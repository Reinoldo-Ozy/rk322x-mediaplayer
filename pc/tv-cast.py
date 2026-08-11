#!/usr/bin/env python3
"""Espelha a tela do PC na TV via box3 (RK3229).

Pede um no de captura ao portal do Wayland, mantem a sessao viva e
transmite H.264 por RTP. Encoder por hardware na Arc (VAAPI).

Uso:  tv-cast.py [--host IP] [--port N] [--width W] [--height H] [--fps N] [--bitrate K]
"""
import argparse, os, random, signal, subprocess, sys
from pathlib import Path
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS, PATH = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
# bitmask do portal ScreenCast: 1=monitor, 2=janela, 4=virtual
TIPOS = {"monitor": 1, "janela": 2, "ambos": 3}
IFACE = "org.freedesktop.portal.ScreenCast"

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="192.168.10.159")
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
            mon = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                                 text=True, check=True).stdout.strip() + ".monitor"
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
