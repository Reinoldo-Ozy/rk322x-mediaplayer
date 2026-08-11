#!/usr/bin/env python3
"""Espelha a tela do PC na TV via box3 (RK3229).

Pede um no de captura ao portal do Wayland, mantem a sessao viva e
transmite H.264 por RTP. Encoder por hardware na Arc (VAAPI).

Uso:  tv-cast.py [--host IP] [--port N] [--width W] [--height H] [--fps N] [--bitrate K]
"""
import argparse, atexit, json, os, random, signal, subprocess, sys, threading
from pathlib import Path
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS, PATH = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
# bitmask do portal ScreenCast: 1=monitor, 2=janela, 4=virtual
TIPOS = {"monitor": 1, "janela": 2, "ambos": 3}
IFACE = "org.freedesktop.portal.ScreenCast"

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("TV_BOX_HOST", "192.168.10.159"),
                help="IP do box (ou variavel TV_BOX_HOST)")
ap.add_argument("--port", type=int, default=5004)
ap.add_argument("--width", type=int, default=1920)
ap.add_argument("--height", type=int, default=1080)
ap.add_argument("--fps", type=int, default=60)
ap.add_argument("--bitrate", type=int, default=20000, help="kbps")
ap.add_argument("--sw-scale", action="store_true", help="escalar na CPU em vez da GPU")
ap.add_argument("--forget", action="store_true", help="esquecer a tela salva e perguntar de novo")
ap.add_argument("--no-audio", action="store_true", help="nao transmitir o audio do PC")
ap.add_argument("--audio-port", type=int, default=0, help="porta do audio (padrao: video+2)")
ap.add_argument("--source", choices=["monitor", "janela", "ambos"], default="monitor",
                help="o que capturar: uma tela inteira, uma janela, ou deixar escolher")
ap.add_argument("--audio-source", choices=["sistema", "janela"], default="sistema",
                help="'sistema' manda tudo que sai das caixas; 'janela' manda so o som "
                     "do programa dono da janela capturada")
ap.add_argument("--audio-pid", type=int, default=0,
                help="mandar so o som deste processo (e dos filhos dele); dispensa a "
                     "descoberta automatica da janela")
args = ap.parse_args()

TOKEN_FILE = Path(os.path.expanduser(f"~/.cache/tv-cast-restore-token-{args.source}"))
if args.forget and TOKEN_FILE.exists():
    TOKEN_FILE.unlink(); print("tela salva esquecida", file=sys.stderr)
SAVED = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
sender = bus.get_unique_name()[1:].replace(".", "_")
loop = GLib.MainLoop()
state = {"procs": []}
tok = lambda: "t%d" % random.randint(0, 2**31)

def call(m, p):
    return bus.call_sync(BUS, PATH, IFACE, m, p, None, Gio.DBusCallFlags.NONE, -1, None)

def wait(h, cb):
    def on_sig(c, s, pth, i, sig, a):
        code, res = a.unpack()
        if code != 0:
            print(f"portal cancelou (codigo {code})", file=sys.stderr); loop.quit(); return
        cb(res)
    bus.signal_subscribe(BUS, "org.freedesktop.portal.Request", "Response",
                         h, None, Gio.DBusSignalFlags.NONE, on_sig)

def create():
    t, st = tok(), tok()
    wait(f"{PATH}/request/{sender}/{t}", select)
    call("CreateSession", GLib.Variant("(a{sv})", ({
        "handle_token": GLib.Variant("s", t),
        "session_handle_token": GLib.Variant("s", st)},)))

def select(res):
    state["session"] = res["session_handle"]; t = tok()
    wait(f"{PATH}/request/{sender}/{t}", start)
    call("SelectSources", GLib.Variant("(oa{sv})", (state["session"], {
        "handle_token": GLib.Variant("s", t),
        "types": GLib.Variant("u", TIPOS[args.source]), "multiple": GLib.Variant("b", False),
        "cursor_mode": GLib.Variant("u", 2),
        "persist_mode": GLib.Variant("u", 2),
        **({"restore_token": GLib.Variant("s", SAVED)} if SAVED else {})})))

def start(res):
    t = tok()
    wait(f"{PATH}/request/{sender}/{t}", stream)
    call("Start", GLib.Variant("(osa{sv})", (state["session"], "", {
        "handle_token": GLib.Variant("s", t)})))

# ----------------------------------------------------------------- som por janela
# O portal NAO diz qual janela foi escolhida — a resposta traz so o tamanho dela, e o
# no do PipeWire tambem nao tem nada do alvo (conferido com pw-dump: node.name e
# sempre "xdg-desktop-portal-hyprland"). Entao o dono da janela e descoberto casando
# esse tamanho com a lista de janelas do Hyprland.

def ancestrais(pid):
    """O pid e todos os pais dele, ate o init."""
    fora, atual = [], pid
    for _ in range(32):
        fora.append(atual)
        try:
            # o comm fica entre parenteses e pode ter espacos — cortar pelo ultimo ")"
            ppid = int(open(f"/proc/{atual}/stat").read().rsplit(") ", 1)[1].split()[1])
        except Exception:
            break
        if ppid <= 1:
            break
        atual = ppid
    return fora


def pw_dump():
    return json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True,
                                     check=True).stdout)


def pid_da_janela(w, h):
    """Qual processo e dono da janela de w x h. None se nao der pra ter certeza."""
    try:
        js = json.loads(subprocess.run(["hyprctl", "-j", "clients"], capture_output=True,
                                       text=True, check=True).stdout)
    except Exception as e:
        print(f"nao consegui falar com o Hyprland ({e})", file=sys.stderr)
        return None
    cand = [c for c in js if tuple(c.get("size") or ()) == (w, h) and c.get("pid", 0) > 0]
    if not cand:
        print(f"nenhuma janela de {w}x{h} na lista do Hyprland", file=sys.stderr)
        return None
    if len(cand) > 1:
        # empate no tamanho: fica com a que esta tocando algo agora
        try:
            tocando = set()
            for n in pw_dump():
                p = n.get("info", {}).get("props", {})
                if p.get("media.class") == "Stream/Output/Audio":
                    try:
                        tocando.add(int(p.get("application.process.id")))
                    except (TypeError, ValueError):
                        pass
            # quem toca costuma ser um filho da janela, entao o teste e ao contrario:
            # o pid da janela aparece na linhagem de quem esta tocando
            com_som = [c for c in cand
                       if any(c["pid"] in ancestrais(t) for t in tocando)]
            if len(com_som) == 1:
                cand = com_som
        except Exception:
            pass
    if len(cand) > 1:
        print(f"{len(cand)} janelas tem {w}x{h}; nao da pra saber qual foi escolhida",
              file=sys.stderr)
        return None
    print(f"janela: {cand[0].get('class')} — {cand[0].get('title', '')[:50]} "
          f"(pid {cand[0]['pid']})", flush=True)
    return cand[0]["pid"]


# Para onde cada canal do programa vai no destino estereo.
CANAIS = {"FL": ["FL"], "FR": ["FR"], "MONO": ["FL", "FR"], "FC": ["FL", "FR"],
          "RL": ["FL"], "RR": ["FR"], "SL": ["FL"], "SR": ["FR"], "LFE": []}


class SomDoApp:
    """Um destino de audio so nosso, alimentado apenas pelo programa escolhido.

    Cria um sink nulo e liga nele as saidas do programa — SEM desligar as ligacoes
    que ele ja tem com as caixas de som, entao o som continua saindo no PC. O ffmpeg
    grava o monitor desse sink, que so tem esse programa dentro.

    O sink nulo e o ponto fixo da historia: programas destroem e recriam o no de audio
    o tempo todo (trocar de video no navegador ja faz isso), e gravar direto do no do
    programa morreria junto. O sink nulo continua existindo — e entregando silencio,
    o que mantem o relogio do RTP andando — enquanto a thread religa o que aparecer.
    """

    def __init__(self, pid):
        self.pid = pid
        self.sink = f"tvcast_{os.getpid()}"
        self.modulo = None
        self.parar = threading.Event()
        self.thread = None

    def abrir(self):
        antes = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                               text=True).stdout.strip()
        self.modulo = subprocess.run(
            ["pactl", "load-module", "module-null-sink", f"sink_name={self.sink}",
             "media.class=Audio/Sink", "channel_map=front-left,front-right",
             "sink_properties=device.description=TV (espelhamento)"],
            capture_output=True, text=True, check=True).stdout.strip()
        atexit.register(self.fechar)
        # se o sistema resolver mudar a saida padrao pro sink novo, desfazer
        depois = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                                text=True).stdout.strip()
        if antes and depois != antes:
            subprocess.run(["pactl", "set-default-sink", antes], check=False)
        self.thread = threading.Thread(target=self._laco, daemon=True)
        self.thread.start()
        return f"{self.sink}.monitor"

    def _portas(self, dump):
        """{id do no: {'in': {...}, 'out': {...}}} com as portas de audio de cada no."""
        fora = {}
        for o in dump:
            if o.get("type") != "PipeWire:Interface:Port":
                continue
            p = o.get("info", {}).get("props", {})
            nome = p.get("port.name", "")
            lado = "out" if p.get("port.direction") == "out" else "in"
            canal = nome.rsplit("_", 1)[-1] if "_" in nome else nome
            fora.setdefault(p.get("node.id"), {"in": {}, "out": {}})[lado][canal] = o["id"]
        return fora

    def _laco(self):
        # Rapido no comeco (o programa pode abrir o som logo depois do cast comecar),
        # calmo depois. Cada passada custa ~15 ms: um pw-dump de 250 kB e o parse dele.
        passada = 0
        while not self.parar.is_set():
            try:
                self._religar()
            except Exception as e:
                print(f"religando o audio: {e}", file=sys.stderr)
            passada += 1
            self.parar.wait(0.4 if passada < 15 else 1.5)

    def _religar(self):
        dump = pw_dump()
        portas = self._portas(dump)
        destino = next((o["id"] for o in dump
                        if o.get("type") == "PipeWire:Interface:Node"
                        and o.get("info", {}).get("props", {}).get("node.name") == self.sink),
                       None)
        if destino is None or destino not in portas:
            return
        alvo = portas[destino]["in"]
        ligado = {(l["info"]["output-port-id"], l["info"]["input-port-id"])
                  for l in dump if l.get("type") == "PipeWire:Interface:Link"}
        for o in dump:
            if o.get("type") != "PipeWire:Interface:Node":
                continue
            p = o.get("info", {}).get("props", {})
            if p.get("media.class") != "Stream/Output/Audio":
                continue
            try:
                dono = int(p.get("application.process.id"))
            except (TypeError, ValueError):
                continue
            # o processo que toca costuma ser filho do dono da janela (o navegador
            # toca num processo separado), entao vale qualquer um da mesma linhagem
            if self.pid not in ancestrais(dono) and dono not in ancestrais(self.pid):
                continue
            saidas = portas.get(o["id"], {}).get("out", {})
            for porta, dsts in self._mapa(saidas).items():
                for d in dsts:
                    if d in alvo and (porta, alvo[d]) not in ligado:
                        subprocess.run(["pw-link", str(porta), str(alvo[d])],
                                       check=False, capture_output=True)

    @staticmethod
    def _mapa(saidas):
        """{porta do programa: canais do destino}. Mono vai pros dois lados, surround
        e dobrado nos dois da frente. Nome fora do padrao (output_1) cai pela ordem."""
        if saidas and all(c in CANAIS for c in saidas):
            return {p: CANAIS[c] for c, p in saidas.items()}
        ordem = [p for _, p in sorted(saidas.items())]
        if len(ordem) == 1:
            return {ordem[0]: ["FL", "FR"]}
        return {ordem[0]: ["FL"], ordem[1]: ["FR"]} if ordem else {}

    def fechar(self):
        self.parar.set()
        if self.modulo:
            subprocess.run(["pactl", "unload-module", self.modulo], check=False,
                           capture_output=True)
            self.modulo = None


def monitor_padrao():
    return subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                          text=True, check=True).stdout.strip() + ".monitor"


def fonte_de_audio(props):
    """Nome da fonte do PulseAudio que o ffmpeg vai gravar."""
    pid = args.audio_pid or None
    if pid is None and args.audio_source == "janela":
        if props.get("source_type") == 1:   # 1 = monitor: o tamanho e o da tela inteira
            print("tela inteira nao tem um dono; vai o som do sistema", file=sys.stderr)
            return monitor_padrao()
        w, h = props.get("size", (0, 0))
        pid = pid_da_janela(w, h)
        if pid is None:
            print("nao identifiquei o programa; vai o som do sistema", file=sys.stderr)
    if not pid:
        return monitor_padrao()
    try:
        som = SomDoApp(pid)
        fonte = som.abrir()
    except Exception as e:
        # sem pipewire-utils, por exemplo: melhor mandar tudo do que ficar mudo
        print(f"nao consegui separar o som ({e}); vai o som do sistema", file=sys.stderr)
        return monitor_padrao()
    state["som"] = som
    print(f"som: apenas o programa do pid {pid}", flush=True)
    return fonte


def stream(res):
    streams = res.get("streams", [])
    if not streams:
        print("nenhum stream", file=sys.stderr); loop.quit(); return
    tk = res.get("restore_token")
    if tk:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(tk)
    node, props = streams[0]
    w, h = props.get("size", (0, 0))
    print(f"capturando node {node} ({w}x{h}) -> {args.host}:{args.port} "
          f"@ {args.width}x{args.height}/{args.fps}fps {args.bitrate}kbps", flush=True)

    scale = (["videoconvert", "!", "videoscale", "!",
              f"video/x-raw,width={args.width},height={args.height},format=NV12", "!"]
             if args.sw_scale else
             ["vapostproc", "!",
              f"video/x-raw,width={args.width},height={args.height},format=NV12", "!",
              "videorate", "!", f"video/x-raw,framerate={args.fps}/1", "!"])
    # O videorate fica DENTRO de "scale", depois do vapostproc — e so ali que ele
    # funciona: antes da escala, os caps vindos do portal tem taxa indefinida e a
    # negociacao trava (nada e transmitido). Cadencia constante e o que da fluidez:
    # taxa variavel contra os 60Hz fixos da TV vira micro-travada mesmo sem perder quadro.
    rate = []
    gst = ["gst-launch-1.0", "-q",
           "pipewiresrc", f"path={node}", "do-timestamp=true", "!"] + rate + scale + [
           "queue", "max-size-buffers=3", "max-size-time=0", "max-size-bytes=0", "!",
           "vah264enc", f"bitrate={args.bitrate}", "key-int-max=60",
           f"cpb-size={args.bitrate}", "ref-frames=1", "b-frames=0", "!",
           "video/x-h264,profile=main", "!", "h264parse", "config-interval=1", "!",
           "fdsink", "fd=1"]
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
          "-f", "h264", "-i", "-", "-c", "copy",
          "-f", "rtp", f"rtp://{args.host}:{args.port}"]

    p1 = subprocess.Popen(gst, stdout=subprocess.PIPE)
    p2 = subprocess.Popen(ff, stdin=p1.stdout)
    p1.stdout.close()
    state["procs"] = [p1, p2]

    if not args.no_audio:
        aport = args.audio_port or (args.port + 2)
        try:
            mon = fonte_de_audio(props)
            # PCM cru (L16): sem latencia de codec. Estereo 48k = ~1.5 Mbps, irrelevante na LAN.
            au = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "pulse", "-i", mon,
                  "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16be", "-payload_type", "96",
                  "-f", "rtp", f"rtp://{args.host}:{aport}"]
            state["procs"].append(subprocess.Popen(au, stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL))
            print(f"audio: {mon} -> {args.host}:{aport}", flush=True)
        except Exception as e:
            print(f"audio nao iniciado: {e}", file=sys.stderr)
    print("transmitindo (Ctrl+C para parar)", flush=True)

FONTE = "/usr/share/fonts/noto/NotoSans-Bold.ttf"


def tela_sem_sinal():
    """Manda alguns segundos de barras coloridas com 'SEM SINAL'.

    O kmssink mantem o ULTIMO quadro recebido no framebuffer, entao esta tela
    fica na TV depois que a transmissao acaba — no lugar da imagem congelada
    do desktop. Tambem deixa o decodificador do box em estado limpo.
    """
    w, h = args.width, args.height
    txt = (f"drawtext=fontfile={FONTE}:text='SEM SINAL'"
           f":fontcolor=white:fontsize={h // 9}"
           f":box=1:boxcolor=black@0.72:boxborderw={h // 36}"
           f":x=(w-text_w)/2:y=(h-text_h)/2-{h // 14},"
           f"drawtext=fontfile={FONTE}:text='a transmissão foi encerrada'"
           f":fontcolor=white@0.85:fontsize={h // 26}"
           f":box=1:boxcolor=black@0.72:boxborderw={h // 90}"
           f":x=(w-text_w)/2:y=(h-text_h)/2+{h // 11}")
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-re",
             "-f", "lavfi", "-i", f"smptebars=size={w}x{h}:rate=15:duration=2",
             "-vaapi_device", "/dev/dri/renderD128",
             "-vf", f"{txt},format=nv12,hwupload",
             "-c:v", "h264_vaapi", "-b:v", "4M", "-g", "5", "-bf", "0",
             "-f", "rtp", f"rtp://{args.host}:{args.port}"],
            timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def stop(*_):
    """Encerra tudo e deixa a tela de 'sem sinal' na TV.

    ATENCAO: precisa ser ligado via GLib.unix_signal_add, NAO signal.signal().
    Com o programa parado dentro de GLib.MainLoop.run() (codigo C), handlers do
    modulo signal do Python so rodam quando o controle volta ao interpretador —
    ou seja, nunca. Foi por isso que o audio continuava tocando e a tela ficava
    congelada quando a transmissao era encerrada.
    """
    if state.get("encerrando"):
        return GLib.SOURCE_REMOVE
    state["encerrando"] = True

    tinha = bool(state["procs"])
    for p in state["procs"]:
        try:
            p.terminate()
        except Exception:
            pass
    for p in state["procs"]:
        try:
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    state["procs"] = []
    if state.get("som"):
        state.pop("som").fechar()   # tira o sink nulo da lista de saidas do sistema
    if tinha:
        tela_sem_sinal()
    loop.quit()
    return GLib.SOURCE_REMOVE


# GLibUnix.signal_add e a API atual; GLib.unix_signal_add funciona mas avisa depreciacao.
try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
    _sinal = lambda s_, f: GLibUnix.signal_add(GLib.PRIORITY_HIGH, s_, f)
except Exception:
    _sinal = lambda s_, f: GLib.unix_signal_add(GLib.PRIORITY_HIGH, s_, f)

_sinal(signal.SIGINT, stop)
_sinal(signal.SIGTERM, stop)
create()
loop.run()
