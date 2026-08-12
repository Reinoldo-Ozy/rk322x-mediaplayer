#!/usr/bin/env python3
"""Espelha a tela do PC na TV via box3 (RK3229).

Pede um no de captura ao portal do Wayland, mantem a sessao viva e
transmite H.264 por RTP. Encoder por hardware na Arc (VAAPI).

Uso:  tv-cast.py [--host IP] [--port N] [--width W] [--height H] [--fps N] [--bitrate K]
"""
import argparse, atexit, ctypes, fcntl, json, os, random, signal, subprocess, sys, threading, time
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
ap.add_argument("--escala", choices=["tamanho-real", "preencher"], default="tamanho-real",
                help="janela que cabe na TV: mandar 1:1 com borda preta (mais nitido) ou "
                     "esticar ate preencher. Tela inteira e janela maior que a TV sempre esticam")
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
state = {}
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

def travar():
    """Garante uma transmissao por destino, encerrando a anterior se houver.

    Duas transmissoes na mesma porta viram dois H.264 embaralhados no mesmo fluxo
    RTP — o decodificador do box para e o kmssink fica segurando o ultimo quadro:
    a TV congela. Acontecia de verdade com o painel e o terminal abertos ao mesmo
    tempo, e o sintoma nao parece nada com a causa.

    A trava e um flock: se o dono morrer de qualquer jeito, o sistema solta sozinho.
    """
    d = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    arq = Path(d) / f"tv-cast-{args.host.replace(':', '_')}-{args.port}.lock"
    f = open(arq, "a+")
    for ultima in (False, True):
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            try:
                velho = int(f.read().split()[0])
            except (ValueError, IndexError):
                velho = 0
            if ultima or not velho:
                print(f"ja existe uma transmissao para {args.host}:{args.port}"
                      f"{f' (pid {velho})' if velho else ''}", file=sys.stderr)
                sys.exit(1)
            print(f"encerrando a transmissao anterior (pid {velho})", flush=True)
            # SIGUSR1 e nosso "saia caladinho": no SIGTERM o outro processo ainda
            # desenha a tela de "sem sinal", que iria pro mesmo endereco e sujaria
            # o comeco desta transmissao.
            try:
                os.kill(velho, signal.SIGUSR1)
            except ProcessLookupError:
                pass
            for _ in range(60):
                try:
                    os.kill(velho, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            continue
        f.seek(0); f.truncate()
        f.write(f"{os.getpid()} {args.host}:{args.port}\n"); f.flush()
        state["trava"] = f    # manter aberto: fechar o arquivo solta a trava
        return


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


def morre_com_o_pai():
    """PR_SET_PDEATHSIG: se este processo levar um kill -9, os filhos vao junto.

    Sem isso um `kill -9` no tv-cast.py deixa o ffmpeg do audio orfao, mandando som
    pra TV pra sempre — aconteceu de verdade, e o sintoma (som sem imagem) parece
    problema de video.
    """
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)


def bytes_do_video():
    """Quanto o ffmpeg do video ja leu do encoder. Serve pra saber se ainda ha fluxo."""
    procs = state.get("video") or []
    if len(procs) < 2:
        return None
    try:
        with open(f"/proc/{procs[1].pid}/io") as f:
            for l in f:
                if l.startswith("rchar:"):
                    return int(l.split()[1])
    except OSError:
        return None
    return None


def tamanho_do_node(node):
    """Tamanho que a captura tem AGORA — muda sozinho quando a janela e redimensionada
    ou trocada de monitor. O portal so informa o tamanho inicial, entao a fonte da
    verdade e o formato negociado do no do PipeWire."""
    try:
        for o in pw_dump():
            if o.get("id") == node:
                for f in o.get("info", {}).get("params", {}).get("Format", []):
                    s = f.get("size") or {}
                    if s.get("width") and s.get("height"):
                        return (s["width"], s["height"])
    except Exception:
        pass
    return None


def montar_video(node, w, h, janela):
    """(Re)cria o par gst+ffmpeg do video e devolve os processos."""
    # Esticar e o que mais custa qualidade — muito mais que qualquer ajuste do encoder.
    # Medido no caminho inteiro (escala -> encode -> decode -> volta), contra os pixels
    # originais da janela: ampliada 96,75 de VMAF, em tamanho real 99,60 (e nos piores
    # quadros, 68 contra 88). Por isso, janela que cabe na TV vai 1:1 com borda preta.
    cabe = (janela and 0 < w <= args.width and 0 < h <= args.height and not args.sw_scale)
    if args.escala == "tamanho-real" and cabe:
        # posicao par: em NV12 o croma vem de dois em dois pixels, e offset impar desloca cor
        x, y = ((args.width - w) // 2) & ~1, ((args.height - h) // 2) & ~1
        scale = ["vacompositor", f"sink_0::xpos={x}", f"sink_0::ypos={y}", "!",
                 f"video/x-raw,width={args.width},height={args.height},format=NV12", "!",
                 "videorate", "!", f"video/x-raw,framerate={args.fps}/1", "!"]
        print(f"sem esticar: {w}x{h} centralizado em {x},{y}", flush=True)
    else:
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
           # Medido a 20 Mbps, contra a tela capturada sem compressao: trellis vale
           # +0,16 de VMAF, profile high +0,03, GOP de 120 +0,01 — de graca, sem B-frames
           # e sem nada que precise "olhar o futuro", entao a latencia nao muda. O GOP mais
           # longo tambem espaca a rajada do quadro-chave (481 kB = 41 ms do enlace).
           # target-usage=1 pede o encoder mais caprichoso: sobra folga (244 fps de encode).
           "vah264enc", f"bitrate={args.bitrate}", "key-int-max=120", "target-usage=1",
           "trellis=true", f"cpb-size={args.bitrate}", "ref-frames=1", "b-frames=0", "!",
           "video/x-h264,profile=high", "!", "h264parse", "config-interval=1", "!",
           "fdsink", "fd=1"]
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
          "-f", "h264", "-i", "-", "-c", "copy",
          "-f", "rtp", f"rtp://{args.host}:{args.port}"]

    p1 = subprocess.Popen(gst, stdout=subprocess.PIPE, preexec_fn=morre_com_o_pai)
    p2 = subprocess.Popen(ff, stdin=p1.stdout, preexec_fn=morre_com_o_pai)
    p1.stdout.close()
    return [p1, p2]


def refazer_sessao():
    """Pede uma sessao nova ao portal, mantendo o audio de pe.

    O token salvo faz o portal restaurar a mesma tela/janela sem perguntar nada.
    """
    print("a captura nao voltou; pedindo uma sessao nova ao portal", file=sys.stderr)
    for p in state.pop("video", []):
        try:
            p.kill()
        except Exception:
            pass
    velha = state.get("session")
    if velha:
        try:
            bus.call_sync(BUS, velha, "org.freedesktop.portal.Session", "Close", None,
                          None, Gio.DBusCallFlags.NONE, -1, None)
        except Exception:
            pass
    state.update(parado=0, bytes=None, refeitos=0)
    create()


def vigia(node, janela):
    """Refaz o video quando a fonte muda de tamanho (ou quando o pipeline morre).

    Mover a janela de monitor a redimensiona, e o pipeline NAO se recupera: com o
    vacompositor a posicao esta fixa no tamanho antigo e o fluxo simplesmente para,
    sem erro nenhum — na TV isso aparece como imagem congelada. Refazer so o par de
    video leva menos de um segundo, mantem o audio intacto e ainda recentraliza a
    janela no tamanho novo.
    """
    if state.get("encerrando"):
        return GLib.SOURCE_REMOVE
    morreu = any(p.poll() is not None for p in state.get("video", []))
    novo = tamanho_do_node(node)
    mudou = novo and novo != state.get("tamanho")

    # Fluxo parado: o pipeline segue vivo e do tamanho certo, mas nada sai. Acontece
    # quando a sessao do portal morre por baixo (xdph reiniciou, por exemplo) — a TV
    # fica com o ultimo quadro e o audio continua, o que engana feio. Escada: primeiro
    # refaz so o pipeline, depois pede sessao nova, depois se aquieta (tela parada
    # tambem nao gera bytes, e ai nao ha nada a consertar).
    agora = bytes_do_video()
    parado = state.get("parado", 0) + 1 if agora is not None and agora == state.get(
        "bytes") else 0
    state["bytes"], state["parado"] = agora, parado
    if not (morreu or mudou):
        state["refeitos"] = 0
        if parado != 5:
            return GLib.SOURCE_CONTINUE
        print("nada sendo transmitido ha 10 s; refazendo o video", file=sys.stderr)

    # Quando a sessao do portal morre por baixo (o xdph reiniciou, por exemplo), o gst
    # morre no mesmo instante em que e recriado — refazer o pipeline nao adianta, so
    # uma sessao nova resolve. Por isso a escada sobe: pipeline, sessao, desistir.
    if morreu and state.get("refeitos", 0) >= 2:
        if state.get("sessoes", 0) >= 2:
            print("nem com sessao nova o video para de morrer; desistindo",
                  file=sys.stderr)
            stop(); return GLib.SOURCE_REMOVE
        state["sessoes"] = state.get("sessoes", 0) + 1
        refazer_sessao(); return GLib.SOURCE_REMOVE
    print(f"fonte agora {novo[0]}x{novo[1]}; refazendo o video" if mudou
          else "o video parou; refazendo", flush=True)
    for p in state.get("video", []):
        try:
            p.kill()
        except Exception:
            pass
    if novo:
        state["tamanho"] = novo
    state["refeitos"] = state.get("refeitos", 0) + 1 if morreu else 0
    state["video"] = montar_video(node, *state["tamanho"], janela)
    return GLib.SOURCE_CONTINUE


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
    janela = props.get("source_type") == 2
    print(f"capturando node {node} ({w}x{h}) -> {args.host}:{args.port} "
          f"@ {args.width}x{args.height}/{args.fps}fps {args.bitrate}kbps", flush=True)
    state["tamanho"] = (w, h)
    state["video"] = montar_video(node, w, h, janela)
    state.update(parado=0, bytes=None)
    GLib.timeout_add_seconds(2, vigia, node, janela)

    if not args.no_audio and not state.get("audio"):   # sessao refeita nao remexe no som
        aport = args.audio_port or (args.port + 2)
        try:
            mon = fonte_de_audio(props)
            # PCM cru (L16): sem latencia de codec. Estereo 48k = ~1.5 Mbps, irrelevante na LAN.
            au = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "pulse", "-i", mon,
                  "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16be", "-payload_type", "96",
                  "-f", "rtp", f"rtp://{args.host}:{aport}"]
            state["audio"] = [subprocess.Popen(au, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL,
                                               preexec_fn=morre_com_o_pai)]
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

    procs = state.pop("video", []) + state.pop("audio", [])
    tinha = bool(procs)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    if state.get("som"):
        state.pop("som").fechar()   # tira o sink nulo da lista de saidas do sistema
    if tinha and not state.get("calado"):
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

def sair_calado(*_):
    """Quem esta assumindo a TV manda SIGUSR1: sai sem desenhar 'sem sinal'."""
    state["calado"] = True
    return stop()


_sinal(signal.SIGINT, stop)
_sinal(signal.SIGTERM, stop)
_sinal(signal.SIGUSR1, sair_calado)
travar()
create()
loop.run()
