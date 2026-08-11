# Hardware H.264 Decode on RK322x — Mainline Kernel

Hardware-accelerated **1080p60** H.264 video playback on Rockchip RK322x TV boxes using the mainline Linux kernel, GStreamer, and open-source drivers only. No Android, no proprietary blobs, no BSP kernel.

> **Update:** this project originally documented a 720p ceiling and attributed it to a hardware
> limitation. That was wrong. **1080p60 runs with zero dropped frames** on the same hardware and
> the same mainline kernel — see [Full HD, corrected](#full-hd-corrected) below.

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
| 1080p@60fps H.264 | rkvdec, zero-copy to kmssink | ✅ 60.02 fps, zero frame drops |
| 1080p@30/25fps H.264 | rkvdec, zero-copy to kmssink | ✅ Zero frame drops |
| 720p@60fps H.264 | rkvdec, zero-copy to kmssink | ✅ 60.00 fps, zero frame drops |
| 1080p, decode throughput alone | rkvdec, no display | 231 fps — 4× what 60 fps needs |
| 1080p, decoded then copied to RAM | rkvdec + copy | 10 fps — the copy costs ~90% |

Earlier versions of this document reported ~13 fps at 1080p. That measurement used `fakesink`,
which forces the copy shown in the last row. See [Full HD, corrected](#full-hd-corrected).

**Superseded — 1080p60 works.** The reasoning above identified the right mechanism (copying
decoded frames out of uncached VPU memory is brutally expensive on a Cortex-A7) but drew the
wrong conclusion. The fix is to **never copy at all**: keep the frame as a DMA-BUF from the
decoder straight to the display plane, and disable two GStreamer behaviours that were halving
the frame rate. See [Full HD, corrected](#full-hd-corrected).

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

### YouTube cookies (optional but recommended)

Without cookies, yt-dlp works anonymously. YouTube may throttle anonymous requests or block certain videos. Adding your browser cookies avoids most of these issues.

**Step 1** — Install a browser extension to export cookies:
- Chrome/Edge: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
- Firefox: [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)

**Step 2** — Open YouTube while logged in, click the extension, and export `cookies.txt`.

**Step 3** — Copy the file to the proxy machine:
```bash
scp cookies.txt user@proxy-machine:/opt/rk322x-proxy/cookies.txt
```

**Step 4** — Edit `/opt/rk322x-proxy/yt_proxy.py` and set:
```python
COOKIES = "/opt/rk322x-proxy/cookies.txt"
```

**Step 5** — Restart the proxy:
```bash
sudo systemctl restart yt-proxy
```

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
| Play 1080p video | ✅ Works at 60 fps — see [Full HD, corrected](#full-hd-corrected) |
| Use a remote control | ❌ Not implemented, but `/dev/cec0` and `/dev/lirc0` are exposed on the box |
| Works with any H.264 file | ⚠️ H.264 Main/High profile up to 1080p; HEVC decodes but slower than H.264; AV1 not supported by rkvdec on this SoC |
| Audio from any format | ⚠️ Only AAC tested; MP3/Opus needs different decoder element |

**Why not install a desktop environment?**

Not because of memory bandwidth — that explanation was wrong, and 1080p60 runs fine with a
zero-copy pipeline. The real reason is that **a browser or GUI player cannot reach the VPU**:
they fall back to software decode, and software decode of 1080p manages 27 fps on a desktop
i5 — roughly 10× faster per core than this Cortex-A7. The performance here comes entirely from
the manual `rkvdec → DMA-BUF → kmssink` path. If you want a UI, drive the box remotely (this
repo ships a desktop app that does exactly that) rather than running one on it.

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


---

## Full HD, corrected

The 720p ceiling documented above was a **measurement artifact**, not a hardware limit.

### The measurement that misled us

Benchmarks used `fakesink`, which forces the decoded frame to be copied into system RAM.
On this SoC the VPU's buffers are not cache-coherent, so that copy costs ~90% of throughput:

| Step (1080p60) | Result |
|---|---|
| Decode only, frame stays in VPU memory | **102 fps** (225 fps with memory scaling enabled) |
| + copy back to system RAM | **10 fps** |

Measured without the copy, the decoder sustains **231 fps at 1080p** — roughly four times what
60 fps needs. The silicon was never the problem.

### What actually capped it at 30 fps

Two GStreamer settings, not hardware:

```bash
kmssink driver-name=rockchip skip-vsync=true qos=false sync=true
```

1. **`skip-vsync=true`** — `kmssink` waits for vsync internally *and* the atomic DRM driver waits
   again on commit. Double vsync = **exactly half the frame rate**. This is why the result was a
   suspiciously round "30.00 fps" instead of a ragged number.
2. **`qos=false`** — GStreamer's QoS was pre-emptively dropping frames it judged late, and the
   lateness came from the double vsync. The two fed each other.

Also required: no `videoconvert` anywhere in the chain (it breaks zero-copy), and the CRTC mode
must match the stream resolution — a 720p stream on a 1080p CRTC drops from 60 fps to 18.

### Verified results

| Mode | Result |
|---|---|
| 1080p60 | **60.02 fps, 0 dropped frames** (reproduced) |
| 1080p30 / 1080p25 / 720p60 | 0 dropped frames |
| CPU while playing a link directly | **2.8%** |
| CPU while mirroring the PC screen | 17% |

Confirm zero-copy is active in the negotiated caps:

```
video/x-raw(memory:DMABuf), format=DMA_DRM, drm-format=NV12
```

### A complete system

Beyond the pipeline fix, this repository now carries a working setup:

- **`box/`** — a TCP-controllable player for the box (links, queue, pause, live/HLS streams)
  plus receivers for screen mirroring, with systemd units and an installer.
- **`pc/`** — a GTK4/libadwaita app for the desktop: paste a link to play it on the TV, or
  mirror a screen or single window (Wayland portal capture, VA-API encoding, RTP).
- **`docs/`** — architecture notes, measurements, and the diagnostic traps that cost the most
  time (written in Portuguese).

The original `yt-play` and proxy tooling documented above still works and remains in the repo.

---

## Credits

This work stands on the RK322x platform support built by others. Specifically:

**[jock (paolosabatino)](https://forum.armbian.com/profile/9843-jock/)** — maintainer of the CSC
Armbian rk322x boards. Three concrete things from his work were used here:

- His **[v4l2request ffmpeg APT repository](https://forum.armbian.com/topic/32449-repository-for-v4l2request-hardware-video-decoding-rockchip-allwinner/)**
  (`apt.undo.it`), an ffmpeg built with the v4l2request and v4l2drmprime patches. Installing it is
  what proved the decoder was never the bottleneck — 102 fps at 1080p, 225 fps with memory
  frequency scaling enabled. It is not in the final runtime path (the player uses GStreamer), but
  it is what killed the "the hardware is too slow" hypothesis and redirected the whole
  investigation.
- His note that **Rockchip SoCs don't need large CMA buffers because the hardware decoders have
  their own MMUs**. This corrected a wrong hypothesis: raising CMA from 16 MB to 128 MB had made
  no difference, and his explanation said why.
- His documented expectation for the rk3228 — roughly 1080p25/1080p30, and only outside a
  compositor — which set the bar these tests were measured against. The final result went past it,
  but knowing where the bar was is what made the anomaly worth chasing.

**The Armbian rk322x community thread**
([CSC Armbian for RK322x TV box boards](https://forum.armbian.com/topic/34923-csc-armbian-for-rk322x-tv-box-boards/))
— for the platform itself, without which none of this exists.

Also noted while researching, though not used here: **ilmich's** unofficial LibreELEC builds for
RK3228/RK3229 and **Jonas Karlman's** rkvdec HEVC backend series upstreamed in August 2025.
