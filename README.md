# RK322x TV box as a video receiver — 1080p60 on mainline Linux

Hardware-accelerated **1080p60 H.264 playback with zero dropped frames** on cheap Rockchip
RK322x TV boxes (RK3229 — MXQ Pro, TX3 Mini and similar), using the **mainline Linux kernel and
open drivers only**. No Android, no proprietary blobs, no BSP 4.4 kernel.

> **This document previously claimed a 720p ceiling** and attributed it to a hardware limitation.
> That was wrong, and the correction is the most useful thing here — see
> [Why 720p was a myth](#why-720p-was-a-myth).

Two ways to use it:

- **Play directly** — the box fetches and plays on its own (YouTube, including live streams).
  Original quality, no re-encoding, **2.8% CPU**. The desktop doesn't even need to be on.
- **Mirror a screen** — the desktop is encoded on its GPU and decoded by the box. Needed for
  DRM services, which only play inside a desktop browser.

---

## Results

| | |
|---|---|
| 1080p60 H.264 | **60.02 fps, 0 dropped frames** (reproduced) |
| Frame pacing | 0.67 fps standard deviation |
| CPU, playing a link directly | **2.8%** |
| CPU, receiving a mirrored screen | 17% |
| Bandwidth, playing directly | 5 Mbit/s |
| Bandwidth, mirroring | 20 Mbit/s |
| Mirroring latency | ~56 ms video, ~60 ms audio |

Hardware: RK3229 (4× Cortex-A7 @ 1.49 GHz), Mali-400 MP2, 2 GB DDR3.

---

## Why 720p was a myth

The 720p ceiling reported by this project (and widely repeated for RK322x on mainline) was a
**measurement artifact**, not a hardware limit.

### The trap

Benchmarks used `fakesink`, which forces the decoded frame to be copied into system RAM. The
VPU's buffers are not cache-coherent, and reading them from a Cortex-A7 is brutally slow:

| Step, 1080p60 | Result |
|---|---|
| Decode only, frame stays in VPU memory | **102 fps** (225 fps with memory scaling on) |
| + copy back to system RAM | **10 fps** |
| + colour conversion | 8 fps |

Measured without the copy, the decoder sustains **231 fps at 1080p** — about four times what
60 fps needs. The silicon was never the problem.

### What actually capped playback at 30 fps

Two GStreamer settings:

```bash
kmssink driver-name=rockchip skip-vsync=true qos=false sync=true
```

1. **`skip-vsync=true`** — `kmssink` waits for vsync internally *and* the atomic DRM driver waits
   again on commit. Double vsync = **exactly half the frame rate**. GStreamer's own property
   documentation says as much: *"should be used for atomic drivers to avoid double vsync"*.
2. **`qos=false`** — QoS was dropping frames it judged late, and the lateness came from the double
   vsync. The two fed each other. Fixing only the vsync gave 50 fps; both together gave 60.

The clue was the number itself: a suspiciously round **30.00 fps**. Resource saturation produces
ragged, drifting values. Exact halving means serialisation.

### Two more things that matter

- **No `videoconvert` anywhere in the chain.** It breaks zero-copy. Confirm the negotiated caps
  say `video/x-raw(memory:DMABuf), format=DMA_DRM, drm-format=NV12`.
- **The CRTC mode must match the stream resolution.** A 720p stream on a 1080p CRTC drops from
  60 fps to 18. Pin it in `/boot/armbianEnv.txt`:
  `extraargs=coherent_pool=2M cma=128M video=HDMI-A-1:1920x1080@60`

---

## Repository layout

```
pc/     runs on the Linux desktop
box/    runs on the RK322x box
docs/   architecture notes and measurements (Portuguese)
```

### `pc/` — desktop

| File | Purpose |
|---|---|
| `tv-cast-gui.py` | GTK4/libadwaita app: play a link on the TV, media controls, mirroring |
| `tv-cast.py` | Mirroring: Wayland portal capture → VA-API encode → RTP, audio of one window or of the system |
| `tv-cast.desktop.in` | Application menu entry template |
| `instalar.sh` | Generates the menu entry with the correct path for this checkout |
| `portal-screencast.py` | Utility: request a PipeWire node from the portal (diagnostics) |

Needs GTK4 + libadwaita, `python3-gi`, `ffmpeg`, and GStreamer with `pipewiresrc`, `vapostproc`
and `vah264enc`, plus a Wayland compositor with a screencast portal.

```bash
./pc/instalar.sh          # adds it to the application menu
python3 pc/tv-cast-gui.py # or just run it
```

The box address defaults to `192.168.10.159`. Override it without editing any source:

```bash
echo 'TV_BOX_HOST=192.168.1.50' >> ~/.config/tv-cast.conf
# or, per run:  TV_BOX_HOST=192.168.1.50 python3 pc/tv-cast-gui.py
```

#### Sending only one window's audio

Mirroring a single window used to send the whole system mix with it — notifications, calls and
every other tab leaked onto the TV. Mirroring a window now sends **only the audio of the program
that owns it** (`--audio-source janela`, the default in the GUI for window capture;
`--audio-source sistema` restores the old behaviour, and `--audio-pid N` picks a process by hand).

Two problems had to be solved.

**Which window was picked?** The portal will not say. The `Start` response carries only `size`,
`position` and `source_type`, and the PipeWire node is anonymous — `node.name` is always
`xdg-desktop-portal-hyprland`, confirmed with `pw-dump`. The owner is therefore found by matching
the size the portal reports against the compositor's window list (`hyprctl clients -j`). A size
tie is broken by whichever candidate is playing audio; if that is still ambiguous, it says so and
falls back to system audio. This part is Hyprland-specific — another compositor needs its own
window query.

**How to capture one program without muting it locally.** A null sink is created and the
program's output ports are linked into it with `pw-link`, *in addition to* the links it already
has to the speakers — never moving the stream. Local playback is untouched; the null sink's
monitor contains that program and nothing else.

```
program ──┬─→ speakers            (its original link, left alone)
          └─→ tvcast_<pid> ──monitor──→ ffmpeg ──RTP L16──→ box
```

Capturing the program's node directly would have been simpler and wrong: applications destroy and
recreate their audio node constantly (changing video in a browser is enough), and the capture
would die with it. The null sink is the stable end of the chain — it keeps delivering silence
while the program is quiet, which keeps the RTP clock advancing (measured: 8.01 s of audio in
8.20 s of wall clock with nothing playing). A thread re-links whatever appears, every 0.4 s for
the first few seconds and every 1.5 s after that.

Separation is **per process**: tabs and windows of the same browser share one audio process and
cannot be told apart. Rejection measured against a 1 kHz tone played by another program: 88.9 in
the system mix versus 0.60 in the isolated capture, ~43 dB.

#### Where mirroring quality actually comes from

Every encoder knob was measured against the desktop captured **uncompressed**, running the whole
path — scale, encode, decode, scale back — and scoring the result with VMAF. At 20 Mbps:

| Setting | VMAF | Worst frame |
|---|---|---|
| Baseline (main profile) | 97.99 | 96.23 |
| `target-usage=1` | 98.01 | 96.25 |
| + `trellis=true` | 98.17 | 96.57 |
| + `profile=high` | 98.20 | 96.61 |
| + GOP 120 | **98.21** | 96.61 |
| 30 Mbps instead of 20 | 98.30 | 97.05 |
| 8 Mbps instead of 20 | 97.12 | 94.28 |

All of it adds up to **+0.22 VMAF** — free, but nearly invisible. Raising the bitrate to 30 Mbps
buys +0.10 for 50% more bandwidth. Geometry is where the quality actually goes:

| Path | VMAF | Worst frame |
|---|---|---|
| Whole 1440p screen → 1080p TV | **76.08** | 33.88 |
| 1280×720 window **stretched** to 1080p | 96.75 | 68.18 |
| Same window sent **1:1**, black borders | **99.60** | 88.09 |

Mirroring a screen larger than the TV throws away 44% of the pixels before the encoder ever sees
them, and nothing recovers that. So a window that fits within the output is now sent unscaled and
centred (`--escala tamanho-real`, the default; `--escala preencher` restores stretching). It uses
`vacompositor`, so the frame is padded on the GPU and the zero-copy path is preserved: 60.01 fps,
zero dropped frames, verified end to end.

Two settings that look like free wins and are **not** — verified rather than assumed on Intel's
VA driver: `scale-method=hq` produces output byte-identical to the default, and `cpb-size` does
not change the keyframe burst (481 KB at every value from 20000 down to 2000).

#### Refresh rate: 60 fps out of a 165 Hz display is not smooth

A high-refresh monitor is the wrong clock to sample. At 165 Hz the compositor delivers frames
12 ms and 18 ms apart (2 or 3 refreshes), and `videorate` has to force that onto a 16.67 ms grid:

| Output cadence | 165 Hz source | 120 Hz source |
|---|---|---|
| Median interval | 18.00 ms | **16.67 ms** |
| Standard deviation | 10.19 ms | **2.65 ms** |
| Worst interval | 115 ms | 34 ms |
| Frames duplicated to fill gaps | **96 of 713** | 0 |

Nothing is *lost* — no counter anywhere reports a drop — but the motion judders in a steady
rhythm. Set the mirrored display to a multiple of the target rate (120 Hz for 60 fps) and it goes
away. Everything else — bigger buffers, higher bitrate, frame interpolation — treats the symptom.

This is why serious mirroring systems never sample an existing display: Sunshine and GameStream
create a virtual display at the client's exact mode, macOS renders a separate AirPlay stream, and
Miracast simply mandates the allowed modes. A mirrored virtual output would be ideal here, and
Hyprland can create one, but its portal only offers real outputs — so the practical fix is the
display mode.

#### One cast at a time, and recovering from a dead capture

Two mirrors pointed at the same box put two H.264 streams on one RTP port; the decoder stops and
the TV holds its last frame, while audio keeps playing — a freeze that looks nothing like its
cause. `tv-cast.py` now takes an exclusive `flock` per destination and asks any previous cast to
step aside (`SIGUSR1`, which exits without painting the "no signal" screen over the new stream).

A watchdog checks every 2 s and rebuilds when the capture stops producing:

- the source **resized** (moving a window to another monitor does this) — the pipeline never
  recovers on its own, since the compositor pad is pinned to the old geometry;
- a pipeline process **died**;
- **nothing was sent for 10 s** — if rebuilding the pipeline does not help, the portal session
  itself is gone (its service restarted, say) and a new one is requested; the saved token means
  no dialog appears.

Children are started with `PR_SET_PDEATHSIG`, so a `kill -9` on the cast cannot leave an orphaned
ffmpeg streaming audio to the TV forever.

### `box/` — the TV box

| File | Purpose |
|---|---|
| `tv-player` | TCP-controllable player: links, queue, pause, live/HLS |
| `tv-receiver` | Receives mirrored video (RTP H.264) |
| `tv-receiver-audio` | Receives mirrored audio (RTP L16) |
| `rk322x-dmc.service` | Enables memory frequency scaling (see below) |
| `instalar.sh` | Installs all of the above |

```bash
sudo ./box/instalar.sh
```

Needs Armbian with a mainline kernel, GStreamer 1.26 with `v4l2codecs` and `kmssink`,
`python3-gi`, `gir1.2-gstreamer-1.0`, `gstreamer1.0-alsa`, and a current `yt-dlp`.

> **`yt-dlp` from apt is likely too old.** YouTube breaks older versions regularly; the Debian
> package failed to list any real formats. Use the current zipapp:
> ```bash
> sudo curl -sL -o /usr/local/lib/ytdlp.pyz \
>   https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp
> printf '#!/bin/sh\nexec python3 /usr/local/lib/ytdlp.pyz "$@"\n' | sudo tee /usr/local/bin/yt-dlp
> sudo chmod 755 /usr/local/bin/yt-dlp
> ```
> This needs periodic updating — that is the nature of yt-dlp.

---

## Player protocol (TCP 5010)

One JSON line per command, one JSON line back:

```json
{"cmd":"play","url":"..."}    {"cmd":"add","url":"..."}
{"cmd":"pause"}  {"cmd":"resume"}  {"cmd":"toggle"}
{"cmd":"next"}   {"cmd":"prev"}    {"cmd":"stop"}
{"cmd":"status"}
```

`status` returns state, position, duration, title, queue index and queue, plus a `vivo` flag for
live streams.

---

## Device node numbering changes between boots

`/dev/videoN` numbering is **not stable**. On one boot `video0` is the decoder; on the next it is
the 2D scaler. Any script with a hardcoded number will break at random. Fix it with a udev rule:

```
# /etc/udev/rules.d/60-rk322x-video.rules
SUBSYSTEM=="video4linux", ATTR{name}=="rkvdec", SYMLINK+="video-rkvdec"
SUBSYSTEM=="video4linux", ATTR{name}=="rockchip-rga", SYMLINK+="video-rga"
```

Then use `/dev/video-rkvdec`. GStreamer's `v4l2slh264dec` finds the decoder on its own and does
not need this, but anything referencing a node explicitly does.

---

## Memory frequency scaling (optional)

The DRAM controller node `dmc@11200000` ships **disabled** in the device tree, so memory runs
locked at whatever the bootloader set. The `rk3228_dmc` driver is already built in Armbian's
kernel — only the node is off. Two overlays enable it and unlock the 786 MHz operating point:

```dts
&{/dmc@11200000} { status = "okay"; };
&{/dmc-opp-table/opp-786000000} { status = "okay"; };
```

Add them to `user_overlays=` in `/boot/armbianEnv.txt`. Memory then scales 330↔786 MHz on demand,
the same trick Android uses.

> **This is not required for 1080p60.** After finding the real cause, 1080p60 was verified at
> 534, 660 and 786 MHz with zero dropped frames in all three. Memory bandwidth was never the
> bottleneck. It is kept because it is free and reversible, and it did fix a marginal 1080p30
> case (27.5 → 30.00 fps) before the vsync issue was understood.

---

## YouTube rate limiting

Anonymous extraction gets throttled: during development the box started answering *"Sign in to
confirm you're not a bot"* after a few dozen extractions in one afternoon. The fix is to give
yt-dlp cookies exported from a browser logged into a throwaway account — a burner, not your main
one, since anything with those cookies acts as that account.

> An earlier version of this project played YouTube through a **proxy machine** running yt-dlp and
> ffmpeg, with the box only consuming MPEG-TS over HTTP. `box/tv-player` replaced it entirely — the
> box resolves links by itself, needs no second machine, and adds a queue and playback controls.
> The proxy code was removed in favour of keeping one working path; it is still in the history if
> anyone wants it.

---

## Limitations

| | |
|---|---|
| **Seeking inside 1080p YouTube video** | ❌ That format is fragmented MP4 (DASH) and `qtdemux` cannot seek it. Verified by comparison: the progressive format seeks fine, the fragmented one does not. Pause, queue and next/previous all work. |
| **DRM services** (Netflix, HBO Max, Disney+) | ❌ Widevine does not exist for 32-bit ARM. Use mirroring — the desktop browser handles the DRM. |
| **AV1** | ❌ Not decodable by this SoC. The format selector prefers H.264. |
| **HEVC** | ⚠️ Decodes, but slower than H.264 here (36 vs 89 fps at 720p). Not worth choosing. |
| **A browser on the box** | ❌ A browser cannot reach the VPU and falls back to software decode. Software decode of 1080p manages 27 fps on a desktop i5 — roughly 10× faster per core than this A7. |
| **Thermals** | ⚠️ 80 °C observed on a long 1080p60 live stream, with no heatsink. Throttling starts near 90 °C. |
| **Mirroring a high-refresh display** | ⚠️ A source whose refresh is not a multiple of 60 judders no matter what. Set the mirrored output to 120 Hz (or 60). |
| **Late frames** | ⚠️ `kmssink` drops buffers it considers late (`A lot of buffers are being dropped` in its log) instead of showing them behind schedule, which reads as a freeze. Giving it a `max-lateness` allowance is untested. |

---

## Measurement traps

These cost the most time, and are the reason the wrong conclusion survived so long:

- **`fakesink` measures the copy, not the decode.** Use `fakevideosink` for raw throughput, or
  `kmssink` for the real path.
- **`strace` and `ftrace` flatten whatever you are comparing.** Their overhead makes both cases
  converge; under `strace`, 1080p and 720p produced identical syscall counts. Only per-call
  durations are meaningful — never rates.
- **`query_seeking` returns `true` even when the seek then fails.**
- **A suspiciously round number is a clue, not a result.** "Exactly 30.00 fps" meant serialisation
  from day one; treating it as saturation led to bandwidth arithmetic that explained nothing.
- **Zero dropped frames does not mean smooth.** What matters is *regularity*: a 16 fps standard
  deviation against a fixed-cadence display is visible stutter without losing a single frame.

---

## Tested environments

| Component | Original guide | 1080p60 results |
|---|---|---|
| Board | Generic RK3229 TV box, 2 GB RAM | same |
| OS | Armbian 24.2.5 Bookworm minimal | Armbian Trixie |
| Kernel | 6.6.22-current-rockchip | **6.18.10-current-rockchip** |
| GStreamer | 1.22.0 | **1.26.2** |
| DTB | `rk322x-box.dtb` | same |
| GPU driver | Lima (Mali-400 MP2, GLES 2.0) | same |
| Hardware decoder | rkvdec (H.264, VP9) | same |

**Kernel 6.6 or newer is required.** The V4L2 stateless decoder API used by `v4l2slh264dec` was
stabilised in 5.18; 5.15 has partial support with differing behaviour. GStreamer 1.18 includes
`v4l2slh264dec` but has known limitations in stateless H.264 decode.

---

## Credits

This work stands on the RK322x platform support built by others.

**[jock (paolosabatino)](https://forum.armbian.com/profile/9843-jock/)** — maintainer of the CSC
Armbian rk322x boards. Three concrete things from his work were used:

- His **[v4l2request ffmpeg APT repository](https://forum.armbian.com/topic/32449-repository-for-v4l2request-hardware-video-decoding-rockchip-allwinner/)**
  (`apt.undo.it`). Installing it is what proved the decoder was never the bottleneck — 102 fps at
  1080p. It is not in the final runtime path, but it is what killed the "the hardware is too slow"
  hypothesis and redirected the whole investigation.
- His note that **Rockchip SoCs don't need large CMA buffers because the hardware decoders have
  their own MMUs**, which corrected a wrong hypothesis: raising CMA from 16 to 128 MB had changed
  nothing, and his explanation said why.
- His documented expectations for the rk3228 — roughly 1080p25/1080p30, outside a compositor —
  which set the bar these measurements were taken against. The result went past it, but knowing
  where the bar sat is what made the anomaly worth chasing.

**The [Armbian rk322x community thread](https://forum.armbian.com/topic/34923-csc-armbian-for-rk322x-tv-box-boards/)**
— for the platform itself, without which none of this exists.

Noted while researching but not used here: **ilmich's** unofficial LibreELEC builds for
RK3228/RK3229, and **Jonas Karlman's** rkvdec HEVC backend series upstreamed in August 2025.

---

## References

- [Armbian for RK322x TV boxes](https://forum.armbian.com/topic/34923-csc-armbian-for-rk322x-tv-box-boards/)
- [GStreamer V4L2 stateless codecs](https://gstreamer.freedesktop.org/documentation/v4l2codecs/)
- [rkvdec kernel driver](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/media/platform/rockchip/rkvdec)
- [Lima GPU driver (Mali-400)](https://docs.mesa3d.org/drivers/lima.html)

---

## AI assistance

This project was developed with the assistance of [Claude](https://claude.ai) (Anthropic).
Debugging, GStreamer pipeline design, kernel driver research and documentation were done
collaboratively between the author and Claude Code.

All results were measured on real hardware. Worth recording honestly: the AI also produced the
original wrong conclusion that 1080p was a hardware limit, and several wrong hypotheses along the
way (memory bandwidth, CMA size, clock slaving). What corrected each of them was measurement —
and, more than once, the author noticing with his own eyes and ears what the counters had missed.

---

## License

MIT.
