#!/usr/bin/env python3
"""
Media proxy for RK322x hardware decode setup.
Supports YouTube and 1000+ sites via yt-dlp.

Endpoints:
  /play?url=FULL_URL&q=720        -> fMP4 H.264+AAC stream (VLC/browser)
  /play?url=FULL_URL&q=720&fmt=ts -> MPEG-TS H.264+AAC stream (GStreamer / yt-play)
  /url?url=FULL_URL&q=720         -> 302 redirect to video-only URL (no audio)
  Shorthand: ?v=YOUTUBE_ID works for YouTube URLs

Startup time: ~12-15s (yt-dlp resolves URLs before ffmpeg starts streaming).
"""
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

PYTHON  = "/usr/bin/python3"   # or full path to your yt-dlp python, e.g. pyenv
COOKIES = ""                   # optional: path to yt-dlp cookies.txt for auth
FFMPEG  = "/usr/bin/ffmpeg"
PORT    = 8091

YT_VIDEO_FMT = {"720": "136", "480": "135", "360": "134", "240": "133", "144": "160"}
YT_AUDIO_FMT = "140"
GENERIC_FMT  = {
    "720": "bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720]",
    "480": "bestvideo[height<=480][ext=mp4]+bestaudio/best[height<=480]",
    "360": "bestvideo[height<=360][ext=mp4]+bestaudio/best[height<=360]",
}

def is_youtube(url):
    return "youtube.com" in url or "youtu.be" in url

def get_urls(media_url, quality):
    cookie_args = ["--cookies", COOKIES] if COOKIES else []
    if is_youtube(media_url):
        vfmt = YT_VIDEO_FMT.get(quality, "136")
        r = subprocess.run(
            [PYTHON, "-m", "yt_dlp"] + cookie_args +
            ["--js-runtimes", "node", "-f", f"{vfmt},{YT_AUDIO_FMT}",
             "--get-url", media_url],
            capture_output=True, text=True, timeout=45)
        urls = [u for u in r.stdout.strip().splitlines() if u.startswith("http")]
        if len(urls) >= 2:
            return urls[0], urls[1]
        if urls:
            return urls[0], None
        raise ValueError(f"YouTube: no URLs — {r.stderr[-300:]}")
    else:
        fmt = GENERIC_FMT.get(quality, GENERIC_FMT["720"])
        r = subprocess.run(
            [PYTHON, "-m", "yt_dlp"] + cookie_args +
            ["-f", fmt, "--get-url", media_url],
            capture_output=True, text=True, timeout=45)
        urls = [u for u in r.stdout.strip().splitlines() if u.startswith("http")]
        if not urls:
            raise ValueError(f"no URLs — {r.stderr[-300:]}")
        if len(urls) >= 2:
            return urls[0], urls[1]
        return urls[0], None

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.client_address[0]}] {fmt % args}")

    def _e(self, code, msg):
        self.send_response(code)
        self.end_headers()
        self.wfile.write(str(msg).encode())

    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        qual = q.get("q", ["720"])[0]
        fmt  = q.get("fmt", ["mp4"])[0]

        raw = q.get("url", [None])[0] or q.get("v", [None])[0]
        if not raw:
            self._e(400, "Missing ?url=URL or ?v=VIDEO_ID\n")
            return

        media_url = raw if raw.startswith("http") else f"https://www.youtube.com/watch?v={raw}"
        media_url = unquote(media_url)
        print(f"[{p.path}] {media_url[:80]} q={qual} fmt={fmt}")

        if p.path == "/url":
            try:
                vurl, _ = get_urls(media_url, qual)
            except Exception as e:
                self._e(500, str(e))
                return
            self.send_response(302)
            self.send_header("Location", vurl)
            self.end_headers()

        elif p.path == "/play":
            try:
                vurl, aurl = get_urls(media_url, qual)
            except Exception as e:
                self._e(500, str(e))
                return

            if fmt == "ts":
                # MPEG-TS: streamable by design, compatible with GStreamer tsdemux
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()
                inputs = ["-i", vurl] + (["-i", aurl] if aurl else [])
                cmd = [FFMPEG, "-loglevel", "error"] + inputs + \
                      ["-c", "copy", "-f", "mpegts", "pipe:1"]
            else:
                # fragmented MP4: compatible with VLC and browsers
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.end_headers()
                inputs = ["-i", vurl] + (["-i", aurl] if aurl else [])
                cmd = [FFMPEG, "-loglevel", "error"] + inputs + \
                      ["-c", "copy", "-f", "mp4",
                       "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                       "pipe:1"]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                while chunk := proc.stdout.read(65536):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.terminate()
        else:
            self._e(404, "Use /play?url=URL or /url?url=URL\n")

if __name__ == "__main__":
    print(f"Media proxy listening on port {PORT}")
    print("  /play?url=URL&q=720        -> fMP4 video+audio (VLC/browser)")
    print("  /play?url=URL&q=720&fmt=ts -> MPEG-TS video+audio (GStreamer)")
    print("  /url?url=URL&q=720         -> redirect video-only")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
