# Hardware H.264 Decode on RK322x — Mainline Kernel 6.6

Hardware-accelerated 720p H.264 video playback on Rockchip RK322x TV boxes using the mainline Linux kernel, GStreamer, and open-source drivers only. No Android, no proprietary blobs, no BSP kernel.

---

## What this is

A technical foundation for running H.264 video at 720p in real-time on cheap RK322x TV boxes (MXQ Pro, TX3 Mini, and similar) under Armbian with kernel 6.6.

**This is not a plug-and-play media center.** There is no graphical interface, no YouTube app, no remote control. What you get is:

- A working GStreamer pipeline that decodes H.264 at 720p using the hardware decoder (rkvdec)
- A command-line script (`yt-play`) for streaming YouTube videos to HDMI
- A proxy server that extracts and muxes video+audio streams

Think of this as the building block. You can build a UI, a kiosk, a simple media tool on top of it — but out of the box, you interact via terminal.

---

## Why this matters

Most RK322x guides use the old Rockchip BSP kernel (4.4), proprietary RKMPP blobs, and the Jock's media framework. This guide achieves hardware decode on **mainline kernel 6.6** using:

- `rkvdec` — the upstream V4L2 stateless decoder driver
- `v4l2slh264dec` — the GStreamer element for stateless decode
- `kmssink` — direct DRM/KMS output to HDMI (no X11, no Wayland)
- `Lima` — open-source Mali-400 GPU driver

This approach is reproducible, upgrade-friendly, and doesn't require replacing the kernel or installing blobs.

---

## Hardware requirements

- Rockchip RK322x TV box (RK3228, RK3229, RK3228A)
- HDMI display connected
- Armbian 24.x with kernel `6.6.x-current-rockchip`
- At least 1 GB RAM
- Internet connection (Ethernet or USB WiFi)

> **Tested on:** MXQ Pro-style box with RK3229, Armbian 24.2.5 Bookworm, kernel 6.6.22-current-rockchip

---

## How it works

```
YouTube URL
    │
    ▼
[Proxy machine: yt-dlp extracts URLs, ffmpeg muxes H.264+AAC → MPEG-TS]
    │  HTTP stream
    ▼
[RK322x box]
souphttpsrc → tsdemux ──► h264parse → v4l2slh264dec → videoconvert → kmssink (HDMI)
                      └──► aacparse → avdec_aac → audioresample → alsasink (HDMI audio)
```

The RK322x hardware decoder (`rkvdec`) handles H.264 decode. The CPU only parses the stream and handles audio decode in software (AAC is lightweight — no issue for Cortex-A7).

---

## Performance

| Resolution | Decoder | Result |
|---|---|---|
| 720p@30fps H.264 | rkvdec (hardware) | ✅ Real-time, zero frame drops |
| 1080p@30fps H.264 | rkvdec (hardware) | ❌ ~13fps — not real-time |
| 1080p@30fps H.264 | avdec_h264 (software) | ❌ Real-time without display, drops frames with kmssink |

**Use 720p or below.** 1080p fails not because of the decoder itself, but because writing 1080p NV12 frames (~90 MB/s) to uncached DRM memory exceeds what the Cortex-A7 can sustain. This is a hardware limitation with no software fix on the mainline kernel.

---

## Setup

### 1. Install GStreamer on the RK322x box

```bash
sudo apt install -y \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-libav \
  gstreamer1.0-alsa
```

### 2. Verify the hardware decoder is available

```bash
ls /dev/video*
# Look for rkvdec — typically /dev/video4
# Confirm with:
gst-inspect-1.0 v4l2slh264dec
```

### 3. Test with a local H.264 file (no proxy needed)

```bash
# Download a test file
wget -O /tmp/test.mp4 "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/720/Big_Buck_Bunny_720_10s_1MB.mp4"

# Play it
gst-launch-1.0 filesrc location=/tmp/test.mp4 ! qtdemux ! h264parse \
  ! v4l2slh264dec ! videoconvert ! kmssink driver-name=rockchip sync=true
```

If video appears on HDMI, hardware decode is working.

### 4. Verify ALSA audio devices

```bash
aplay -l
# You should see:
#   card 0: analog   — 3.5mm jack
#   card 2: hdmisound — HDMI audio
```

### 5. Install yt-play

```bash
git clone https://github.com/Reinoldo-Ozy/rk322x-mediaplayer
cd rk322x-mediaplayer
sudo ./install-box.sh PROXY_IP   # replace with your proxy machine IP
```

The script installs GStreamer packages, copies `yt-play` to `/usr/local/bin/`, and saves the proxy IP to `/etc/profile.d/rk322x-proxy.sh`.

---

## Proxy setup (required for YouTube streaming)

The proxy runs on **a separate machine** on the same network (a Raspberry Pi, another Linux box, or a PC). It handles YouTube URL extraction and audio/video muxing so the RK322x only has to decode.

> The RK322x can run the proxy itself, but it will take longer to start playback (~30s vs ~15s).

### On the proxy machine

```bash
git clone https://github.com/Reinoldo-Ozy/rk322x-mediaplayer
cd rk322x-mediaplayer
sudo ./install-proxy.sh
```

The script installs ffmpeg, Node.js, yt-dlp, copies the proxy to `/opt/rk322x-proxy/`, and starts it as a systemd service on port 8091.

To enable authenticated YouTube access (avoids some throttling), export cookies from your browser and set the `COOKIES` path in `/opt/rk322x-proxy/yt_proxy.py`.

---

## Usage

### Playing a YouTube video

```bash
# Basic — 720p, HDMI audio
yt-play dQw4w9WgXcQ

# Choose quality
yt-play dQw4w9WgXcQ 480

# Use analog audio output (3.5mm jack) instead of HDMI
yt-play dQw4w9WgXcQ 720 analog
```

You can also pass full URLs to the proxy directly:

```
http://<proxy-ip>:8091/play?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&q=720&fmt=ts
```

### Playing a local file

```bash
gst-launch-1.0 filesrc location=/path/to/video.mp4 ! qtdemux ! h264parse \
  ! v4l2slh264dec ! videoconvert ! kmssink driver-name=rockchip sync=true
```

### Playing with audio from a local file (if the file has AAC audio)

```bash
gst-launch-1.0 -e filesrc location=/path/to/video.mp4 ! qtdemux name=demux \
  demux. ! queue ! h264parse ! v4l2slh264dec ! videoconvert ! kmssink driver-name=rockchip sync=true \
  demux. ! queue ! aacparse ! avdec_aac ! audioconvert ! audioresample \
  ! "audio/x-raw,rate=44100,channels=2" ! alsasink device=hw:2
```

---

## Limitations — read before deploying

**This is a command-line tool, not a media center.**

| What you might expect | Reality |
|---|---|
| Open a browser and watch YouTube | ❌ Browsers do software decode — unusable frame rate |
| Install XFCE and use it like a PC | ❌ Desktop + browser overhead kills performance; no VA-API bridge for rkvdec |
| Play 1080p video | ❌ Hardware limitation (DRM memory bandwidth) |
| Use a remote control | ❌ Not implemented — use SSH or build a UI on top |
| Works with any H.264 file | ⚠️ H.264 Main/High profile ≤720p only; HEVC and AV1 not supported by rkvdec on this SoC |
| Audio from any format | ⚠️ Only AAC tested; MP3/Opus needs different decoder element |

**Why not install a desktop environment?**

The Cortex-A7 at 1.2GHz cannot sustain the memory bandwidth needed for 1080p video through the DRM framebuffer. At 720p this works because the frame size (~1.4MB) fits within what the hardware can push. A desktop environment (XFCE, etc.) would run — but video in a browser or media player with a GUI adds overhead that puts even 720p at risk. If you want a GUI, consider a lightweight web interface that triggers `yt-play` via HTTP rather than running a full desktop.

**Why does the proxy need to be on a separate machine?**

yt-dlp takes 10–15 seconds to resolve YouTube URLs. During that time, GStreamer is waiting for the first byte of the stream. Running yt-dlp on the RK322x itself (slow ARM core) can push this to 30+ seconds. A proxy on a faster machine keeps startup time reasonable.

---

## Files in this repo

```
├── install-box.sh        # Installer for the RK322x box
├── install-proxy.sh      # Installer for the proxy machine
├── yt-play               # Playback script (installed by install-box.sh)
└── proxy/
    ├── yt_proxy.py       # Proxy server (installed by install-proxy.sh)
    └── yt-proxy.service  # systemd unit template
```

---

## Tested environment

| Component | Version |
|---|---|
| Board | Generic RK3229 TV box (MXQ Pro style), 2 GB RAM |
| OS | Armbian 24.2.5 Bookworm minimal |
| Kernel | 6.6.22-current-rockchip |
| DTB | `rk322x-box.dtb` (Armbian default for generic RK322x boxes) |
| GStreamer | 1.22.0 (Debian Bookworm apt packages) |
| GPU driver | Lima (Mali-400 MP2, OpenGL ES 2.0) |
| Hardware decoder | rkvdec — `/dev/video4` (H.264, VP9) |

### Kernel and distribution requirements

**Kernel 6.6 (Armbian 24.x current-rockchip) is strongly recommended.**

- The V4L2 stateless decoder API used by `v4l2slh264dec` was stabilized in kernel 5.18. Kernel 5.15 has partial support but behavior may differ.
- GStreamer 1.22 (Debian Bookworm) was used for all tests. GStreamer 1.18 (Debian Bullseye) includes `v4l2slh264dec` but has known limitations in stateless H.264 decode — **not tested with this setup**.
- If your box runs an older Armbian (22.x, kernel 5.15, Debian Bullseye), upgrade to Armbian 24.x before following this guide.

---

## What's next / possible extensions

- Simple Flask web UI to submit URLs and trigger playback remotely
- Playlist support via a queue file
- Hardware VP9 decode (rkvdec supports VP9 on RK3229 — untested)
- HEVC support requires a different decoder node on some RK322x variants

---

## References

- [Armbian for RK322x TV boxes](https://forum.armbian.com/topic/12656-csc-armbian-for-rk322x-tv-box-boards/)
- [GStreamer V4L2 stateless codecs](https://gstreamer.freedesktop.org/documentation/v4l2codecs/)
- [rkvdec kernel driver](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/media/platform/rockchip/rkvdec)
- [Lima GPU driver (Mali-400)](https://docs.mesa3d.org/drivers/lima.html)

---

## AI assistance

This project was developed with the assistance of [Claude](https://claude.ai) (Anthropic). The debugging sessions, GStreamer pipeline design, kernel driver research, and documentation were done collaboratively between the author and Claude Code.

All code was tested on real hardware. The AI assisted in reasoning through kernel internals (V4L2 stateless API, Rockchip EPHY driver, DRM memory bandwidth constraints) and iterating on the GStreamer pipeline until it worked correctly on the actual device.
