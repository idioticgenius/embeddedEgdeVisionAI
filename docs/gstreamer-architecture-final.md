# GStreamer Architecture — Current Multichannel Pipeline

Reference doc for the multichannel Edge Vision AI prototype on the
AMD Kria KV260 (PetaLinux 2022.2, overlay `multichannel-openamp-gpio`).
Targeted at the project report's Chapter 5 / Chapter 7 and at future
sessions that need to reason about the pipeline without re-reading
`server.py`.

Companion to `rpu-enablement.md`. See §24 index there.

---

## 1. Overview and data paths

The system runs **one `gst-launch-1.0` subprocess** at a time,
composed programmatically by `server.py::build_gst_command` from 1–4
per-channel fragments. Every fragment shares the same downstream
structure; only the source stage differs (file vs webcam).

Three logical data paths per channel:

| Path | Carries | Consumers |
|---|---|---|
| **Frame data** | NV12 video buffers (DMA-backed) | `kmssink` (display), `vvas_xinfer` (inference) |
| **Metadata** | `GstInferenceMeta` + `GstVvasOverlayMeta` attached to buffers | `vvas_xoverlay`, `zoneguard` |
| **Events** | AF_UNIX JSON datagrams from `zoneguard` | `server.py` zone-listener → alert/LED path |

Everything AI-related (VCU decode, PPE scaler, DPU inference) is
DMA-backed so full-resolution frames never touch the A53 CPU cores in
the critical path. CPU work is restricted to: GStreamer control flow,
metadata mutation (tiny structs), the zoneguard polygon test
(per-bbox, ~100 ns), and `server.py`'s event dispatch.

---

## 2. Stage-by-stage element chain (per channel)

Using channel 0 with `refinedet` model and a file source as the
canonical example. Line breaks for readability; in practice each
channel's fragment is one long `!`-joined chain.

```
filesrc location=walking.mp4 name=src0
  │
  ▼
qtdemux name=demux0                  # strips the MP4 container → raw H.264 ES
  │
  ▼
h264parse name=parse0                # adds SPS/PPS framing, byte-stream
  │
  ▼
omxh264dec name=dec0                 # VCU hardware decode → NV12 DMA buffer
  internal-entropy-buffers=3         # extra slots to decouple VCU from DDR
  │
  ▼  <caps: video/x-raw,format=NV12,width=1280,height=720>
  │
tee name=t0                          # duplicates the buffer (same DMA fd)
  │                                  ├──► inference branch
  │                                  └──► display branch
  │
  ├── inference branch ─────────────────────────────────────────
  │   queue
  │     │
  │     ▼
  │   vvas_xabrscaler name=scaler0   # PPE (image_processing kernel):
  │     xclbin-location=dpu.xclbin   # NV12 → BGR, resize to 480×360, normalise
  │     kernel-name="image_processing:{image_processing_1}"
  │     │
  │     ▼  <caps: video/x-raw,width=480,height=360,format=BGR>
  │   queue
  │     │
  │     ▼
  │   vvas_xinfer name=infer0        # DPU B3136: refinedet_pruned_0_96
  │     infer-config=kernel_refinedet.json
  │     │                            # emits GstInferenceMeta on the buffer
  │     ▼
  │   → ima0.sink_master              # metaaffixer gathers inference metadata
  │                                    from the small branch and attaches it
  │                                    to the matching-PTS buffer on the
  │                                    full-resolution branch.
  │
  ├── display branch ───────────────────────────────────────────
  │   queue max-size-buffers=1
  │     │
  │     ▼  <caps: video/x-raw,format=NV12,width=1280,height=720>
  │   → ima0.sink_slave_0
  │
  ▼
vvas_xmetaaffixer name=ima0          # pair small-branch inference metadata
                                      with full-res frames by PTS. Emits the
                                      enriched full-res buffer on src_slave_0.
  │
  ▼
vvas_xmetaconvert name=meta0         # convert GstInferenceMeta to
  config-location=metaconvert.json    GstVvasOverlayMeta (draw-ready shapes).
  │
  ▼
zoneguard name=zg0                   # custom C plugin:
  channel=0                          #  - reads GstInferenceMeta for bboxes
  zones-config=/tmp/zoneguard_ch0.json   #  - per-bbox, point-in-polygon vs
  event-socket=/tmp/zoneguard.sock   #      drawn-zone polygon
                                      #  - appends zone outlines to the
                                      #    GstVvasOverlayMeta so the renderer
                                      #    draws them
                                      #  - on ENTER/CLEAR transition, emits
                                      #    AF_UNIX datagram to server.py
                                      #    + (RPU mode only) writes TCM_0B
                                      #    flag bit
  │
  ▼
vvas_xoverlay name=overlay0          # draws bboxes, labels, and zone outlines
                                      # from GstVvasOverlayMeta directly into
                                      # the NV12 frame using PL overlay IP.
  │
  ▼
tee name=disp0                       # split display → kmssink + fpsdisplaysink
  │                                  ├──► kmssink (video mixer plane)
  │                                  └──► fpsdisplaysink (stats)
  │
  ├── kmssink branch ───────────────────────────────────────────
  │   queue
  │     │
  │     ▼
  │   kmssink name=ksink0            # DRM KMS direct path:
  │     driver-name=xlnx             #  - renders frame onto the Xilinx video
  │     plane-id=<per-channel>       #    mixer plane assigned to this channel
  │     render-rectangle="..."       #  - no CPU composition
  │     show-preroll-frame=false
  │     sync=false can-scale=true
  │
  └── fpsdisplaysink branch ────────────────────────────────────
      queue
        │
        ▼
      fpsdisplaysink name=fps0       # emits "rendered: N, current: X fps"
        text-overlay=false            # messages on the bus; server.py parses
        sync=false                    # these for /api/pipeline/status.
        video-sink=fakesink
```

Key details that often surprise people:

- **`tee` ≠ copy.** On ZynqMP GStreamer, the NV12 buffers emitted by
  `omxh264dec` are DMA-backed. `tee` shares the same underlying
  buffer between downstream branches — no memcpy, no extra DDR
  traffic. The pipeline can sustain 4 × 720p30 at essentially zero
  DDR-bandwidth overhead per tee hop.
- **The small-branch + big-branch `vvas_xmetaaffixer` trick.**
  Inference runs at the model's tiny input resolution (480×360 for
  RefineDet) to fit the DPU's working set. The full-res frame stays
  on a parallel branch. `vvas_xmetaaffixer` pairs them back up by
  PTS and re-attaches metadata to the full-res buffer for overlay
  drawing. That's how we get hi-res video with inference-driven
  overlays without decoding or scaling twice.
- **`zoneguard` sits after `vvas_xmetaconvert`**, not before. It
  reads the `GstInferenceMeta` (still present on the buffer for
  reference) *and* appends polygon shapes to the already-produced
  `GstVvasOverlayMeta` — so the overlay drawer downstream renders
  both the inference bboxes and our zone polygons in one pass.

---

## 3. Multi-channel combination

`server.py::build_gst_command` concatenates the per-channel fragments
inside a single `gst-launch-1.0` invocation:

```
gst-launch-1.0 -v
  <ch0 fragment>
  <ch1 fragment>
  <ch2 fragment>
  <ch3 fragment>
```

Each channel's `kmssink` targets a **distinct video mixer plane**
(`plane-id=N`) with a **distinct render-rectangle** (`x,y,w,h` on
the 1920×1080 HDMI canvas). The Xilinx video mixer IP
(`xlnx,v-mix-…`) composes the four planes in hardware; the A53
never touches the composited frame.

Per-channel plane and rectangle assignments live in
`server.py::CHANNEL_FIXED`, keyed by channel index and video-count
(so a 2-channel run uses a different 2-plane layout than a 4-channel
run, both fixed at pipeline-build time).

---

## 4. Metadata flow (detail)

Buffer-attached metadata travels alongside the frame:

1. **`GstInferenceMeta`** — inserted by `vvas_xinfer`. A tree of
   per-bbox entries with class, confidence, and bbox rect. Sized
   proportionally to the number of detections per frame (typically
   ≤ a few KB).

2. **`GstVvasOverlayMeta`** — inserted by `vvas_xmetaconvert` based
   on `vvas_xmetaconvert`'s config JSON. This is the "drawable
   shapes" representation: bounding rectangles with colour/label
   strings, ready for the overlay drawer.

3. **Zone outlines appended by `zoneguard`** — zoneguard's C code
   calls `gst_buffer_add_vvas_overlay_meta()` (or manipulates the
   existing meta) to append its polygon shapes alongside the inference
   bboxes. Downstream `vvas_xoverlay` doesn't distinguish — it just
   draws everything it finds in the overlay meta.

4. **`zoneguard` AF_UNIX event** — separate path, not a GStreamer
   meta. One datagram per ENTER/CLEAR transition, parsed by
   `server.py::_zoneguard_listener`.

5. **`zoneguard` TCM flag write (RPU mode only)** — separate path,
   written directly via `/dev/mem` mmap at `0xFFE20004`. Read by the
   R5 baremetal mirror loop, which writes `axi_gpio_0` to drive the
   PMOD LEDs. See `rpu-enablement.md` §23.14 for the full LED-path
   diagram.

---

## 5. Hardware vs software assignments

| Stage | Runs on | Cost |
|---|---|---|
| filesrc / v4l2src | A53 (I/O thread) | negligible |
| qtdemux / h264parse | A53 | negligible |
| omxh264dec | VCU (hardware) | 0 CPU, uses VCU DDR bandwidth |
| tee | A53 (pointer shuffle) | negligible |
| vvas_xabrscaler | PPE (`image_processing` IP) @ 225 MHz | 0 CPU, PPE-bound |
| vvas_xinfer | DPU B3136 @ 275 MHz | 0 CPU, DPU-bound |
| vvas_xmetaaffixer | A53 | ~µs per buffer |
| vvas_xmetaconvert | A53 | ~µs per buffer |
| zoneguard (polygon test) | A53 | ~µs per bbox |
| vvas_xoverlay | PL overlay IP | 0 CPU |
| v_mix (video mixer) | PL | 0 CPU |
| kmssink | A53 (DRM ioctl only) | negligible |
| fpsdisplaysink | A53 (stats only) | negligible |

**Observation.** The critical data path (decode → scale → infer →
overlay → display) is entirely FPGA/DMA from the host CPU's
perspective. The A53 cores handle control flow, metadata structs, and
the web UI — nothing in the hot pixel path.

---

## 6. Where VVAS sits vs native GStreamer elements

VVAS (Vitis Video Analytics SDK) is AMD's framework on top of
GStreamer that wraps the Vitis-AI runtime (VART) and the Xilinx PL IPs
into standard GStreamer elements. Our pipeline uses both.

**Native GStreamer elements** (from gst-plugins-good / bad / base):

- `filesrc`, `v4l2src`, `qtdemux`, `h264parse`
- `omxh264dec` (actually an OMX wrapper, but conceptually native)
- `tee`, `queue`, `capsfilter`, `fakesink`
- `kmssink`, `fpsdisplaysink`

**VVAS elements** (from Vitis Video Analytics SDK):

- `vvas_xabrscaler` — wraps `image_processing` PL IP (colour convert,
  resize, normalise).
- `vvas_xinfer` — wraps VART, running a quantised `.xmodel` on the
  DPU.
- `vvas_xmetaaffixer` — pairs small-branch metadata with full-res
  buffers.
- `vvas_xmetaconvert` — translates `GstInferenceMeta` to
  `GstVvasOverlayMeta`.
- `vvas_xoverlay` — wraps the PL overlay drawer.

**Custom element** (project-local):

- `zoneguard` — written in C, built as an external GStreamer plugin
  (`/usr/lib/gstreamer-1.0/libgstzoneguard.so`). Reads
  `GstInferenceMeta`, does the zone polygon test, appends zone shapes
  to the overlay meta, emits UDS events, writes TCM flags. Source in
  `/home/petalinux/zoneguard/`.

---

## 7. Known limitations and future work

### 7.1 Single-CU PPE contention (not yet addressed)

Only one `image_processing` compute unit (CU) is instantiated in the
current xclbin. All four `vvas_xabrscaler` instances share it,
serialising preprocessing. Under 4-channel load the aggregate
throughput plateaus at ~22 fps per channel (measured). A 2× or 4×
PPE-CU Vivado rebuild would lift this. `remaining-tasks.md` §12
catalogues the path.

### 7.2 Single gst-launch subprocess — camera disconnect fatal

The whole pipeline is one `gst-launch-1.0` process. A v4l2 error on
one webcam channel kills the whole process, taking out the other
three file channels. The `fallbacksrc` experiment (see
`rpu-enablement.md` §23.16) was a try at fixing this; it worked but
forced raw decode and cost ~1 A53 core per webcam channel. Reverted.
A proper fix is either:

- **PyGObject refactor of `server.py`**: build the pipeline in-process
  with `Gst.Pipeline`, listen for bus errors per branch, swap
  `v4l2src`/`omxh264dec` branches dynamically with `input-selector`
  when a camera dies.
- **Server-side watchdog**: detect gst-launch exit, swap the dead
  channel for a `videotestsrc` placeholder, restart, poll for replug.
  Cheaper but restarts the whole pipeline.

### 7.3 Inference is CPU-free but metadata mutation is serial

`vvas_xmetaaffixer` and `vvas_xmetaconvert` are A53 elements. At
current throughputs (4 × 22 fps = 88 fps aggregate) the combined A53
load for metadata work is ~5–10 % of one core. Well within budget.
If channel count or per-channel fps rises substantially, these become
the next A53 bottleneck after zoneguard.

### 7.4 Display sync is bypassed

All `kmssink` and `fpsdisplaysink` sinks run `sync=false` because the
file sources produce frames as fast as the pipeline can consume them
and the webcam source is UVC-paced. A consequence: frame-rate jitter
on the display is invisible to the operator (display catches up
instantly) but the *content* can tear on rapid motion. Acceptable for
this prototype; for a production system you'd enable sync on the
display sinks and add explicit rate control.

### 7.5 No GStreamer hardware overlay for the web UI

The web UI's live preview comes from a re-encode path
(`/api/pipeline/preview`), not from `kmssink`'s HDMI output. That
means the UI preview has ~1 s of MJPEG encode + transmission lag.
The HDMI output itself is ~40–60 ms glass-to-glass. See
`latency-report.md` §5 for the distinction.

---

## 8. How to read the live pipeline graph

While the pipeline is running, dump the DOT graph:

```sh
export GST_DEBUG_DUMP_DOT_DIR=/tmp/gst-dot
# start the pipeline from the UI
# a .dot file appears at /tmp/gst-dot/<timestamp>-<state>.dot
```

Copy it to the host and render with `graphviz`:

```sh
scp -i ~/.ssh/id_ed25519 petalinux@board:/tmp/gst-dot/*.dot .
dot -Tpng pipeline.dot -o pipeline.png
```

This gives you the definitive element-by-element graph with caps on
every pad — authoritative for the report's Figure-set.

---

## 9. File index

- `server.py::build_gst_command` — top-level pipeline builder.
- `server.py::build_channel_fragment` — per-channel fragment builder.
- `server.py::_source_element` — source stage (file vs v4l2).
- `server.py::MODELS` — model-specific caps (input size, format,
  JSON config paths).
- `server.py::CHANNEL_FIXED` — per-channel plane / tee / ima names.
- `jsons/kernel_refinedet.json` — VVAS xinfer config for RefineDet.
- `jsons/metaconvert_config.json` — VVAS metaconvert config.
- `zoneguard/zoneguard.c` — zoneguard plugin source.
- `/usr/share/vitis_ai_library/models/` — quantised xmodel binaries.
- `/lib/firmware/xilinx/multichannel-openamp-gpio/dpu.xclbin` —
  xclbin containing DPUCZDX8G + image_processing IP.
