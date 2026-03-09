# gstreamer_architecture

In-depth walkthrough of the GStreamer pipeline this app builds, the VVAS
(Vitis Video Analytics SDK) plugins and APIs it touches, the Vitis-AI
models it loads, the JSON descriptors that configure every accelerated
kernel, and the built-in GStreamer facilities used for profiling and
control.

This is about what runs on the Kria KV260 when you click **Start
Pipeline** in the Detection tab. It is not a generic VVAS tutorial;
every path / name / option below matches the production code in
`/home/petalinux/server.py` and the JSON files in
`/home/petalinux/jsons/`.

---

## 1. High-level shape of a detection channel

One channel = one of the four 960×540 (or full-screen, configurable)
quadrants on the 1920×1080 HDMI output. Each channel is an independent
sub-pipeline inside a single `gst-launch-1.0` process; the single
process is what lets all four channels share the DPU's exclusive XRT
lock (see below).

A detection-enabled channel has this dataflow:

```
  ┌─────────────────────────────┐
  │  source: filesrc or v4l2src │   (H.264 byte-stream)
  └────────────────┬────────────┘
                   │
           ┌───────▼───────┐
           │  qtdemux      │   (only for file sources)
           └───────┬───────┘
           ┌───────▼───────┐
           │  h264parse    │
           └───────┬───────┘
           ┌───────▼───────┐
           │  omxh264dec   │   (VCU hardware decoder → NV12)
           └───────┬───────┘
                   │
           ┌───────▼───────┐
           │   tee         │   (broadcast to detection + display branches)
           └──┬────────┬───┘
              │        │
              │        └──────────────────────────────────────┐
              ▼                                               ▼
   ┌──────────────────┐                              ┌───────────────────┐
   │ vvas_xabrscaler  │ (image_processing kernel)    │  queue (1 buffer) │
   │  NV12 → BGR 480× │                              │   synchronises to │
   │  360 for refinedet│                             │   detection meta  │
   └─────────┬────────┘                              └─────────┬─────────┘
             ▼                                                 │
   ┌──────────────────┐                                        │
   │   vvas_xinfer    │ (DPU forward pass + postproc)          │
   │   → refinedet /  │                                        │
   │     densebox     │                                        │
   └────────┬─────────┘                                        │
            ▼                                                  ▼
   ┌────────────────────── vvas_xmetaaffixer ────────────────────┐
   │ master-pad: inferred 480×360 BGR buffer with detections     │
   │ slave-pad:  original NV12 960×540 frame from tee            │
   │ output    : slave frame annotated with SCALED metadata      │
   └──────────────────────────────┬──────────────────────────────┘
                                  ▼
                      ┌────────────────────┐
                      │ vvas_xmetaconvert  │  (struct → overlay hints)
                      └──────────┬─────────┘
                                 ▼
                      ┌────────────────────┐
                      │ vvas_xoverlay      │  (draw boxes on NV12)
                      └──────────┬─────────┘
                                 ▼
                            ┌────────┐
                            │  tee   │  (broadcast to kmssink + fps)
                            └─┬────┬─┘
                              │    │
                              ▼    ▼
                       ┌──────────┐  ┌────────────────┐
                       │ kmssink  │  │ fpsdisplaysink │
                       │ (plane N)│  │  → fakesink    │
                       └──────────┘  └────────────────┘
```

When **Detection OFF** is toggled on a channel the subtree between the
first `tee` and the `vvas_xoverlay` is collapsed — the raw NV12 goes
straight from the decoder to a display `tee` and then to `kmssink` and
`fpsdisplaysink`. No DPU or scaler is touched.

---

## 1b. Layering (GStreamer ↔ VVAS Core ↔ Vitis-AI ↔ XRT)

Every VVAS element in the pipeline is a thin GStreamer adapter on top of
three deeper layers. Understanding the layering helps when debugging:

```
┌──────────────────────────────────────────────────────────────┐
│  GStreamer pipeline (gst-launch-1.0 subprocess in our case)  │
│  - elements: vvas_xinfer, vvas_xabrscaler, vvas_xoverlay, …  │
│  - implement GstBaseTransform / GstElement                   │
├──────────────────────────────────────────────────────────────┤
│  VVAS Core library (libvvascore_*.so)                        │
│  - GstVvasMemory / buffer pools                              │
│  - scaler / overlay primitives (vvas_core_scaler,            │
│    vvas_core_overlay, vvas_core_common)                      │
├──────────────────────────────────────────────────────────────┤
│  Vitis-AI Library (libvitis_ai_library.so)                   │
│  - model-class-specific tasks: REFINEDET / FACEDETECT /      │
│    SSD / YOLO / …                                            │
│  - xir::Graph + vart::Runner                                 │
├──────────────────────────────────────────────────────────────┤
│  XRT (libxrt_core.so, libxrt_coreutil.so)                    │
│  - xrt::device, xrt::xclbin, xrt::kernel, xrt::run           │
│  - DMA buffer management via ZOCL                            │
├──────────────────────────────────────────────────────────────┤
│  Kernel: ZOCL driver (zynqmp_drm), CMA reservation,          │
│          FPGA manager (remaining xclbin bitstream load)      │
├──────────────────────────────────────────────────────────────┤
│  Hardware: DPU CU, image_processing CU, VCU, DDR, AMS, IC    │
└──────────────────────────────────────────────────────────────┘
```

A single call like `gst_element_set_state(xinfer, PLAYING)` fans out
through every layer: Vitis-AI opens the model, creates a `vart::Runner`,
which asks XRT for an `xrt::kernel` handle against the DPU CU, which
reserves a slice of the DPU's hardware context via ZOCL, which uses
CMA to back the input/output tensors. The moment that chain completes
the scaler CU and DPU are locked to this process.

---

## 1c. Memory: CMA, dmabuf, and zero-copy between plugins

A 1280×720 NV12 buffer is ~1.35 MB. At 60 fps × 4 channels × several
queue stages the pipeline would copy 2+ GB/s through the APU if every
element did a `memcpy` on each buffer. VVAS avoids that by sharing
**Contiguous Memory (CMA)** buffers via **dmabuf** file descriptors.

- Each VVAS element that owns a compute unit (xinfer, xabrscaler,
  metaaffixer) allocates `GstVvasMemory` using VVAS's buffer pool,
  which is backed by ZOCL-allocated physically-contiguous memory.
  That memory is then **exported as a dmabuf fd** that the next
  element imports without copying.
- `omxh264dec` likewise produces NV12 in a `GstOmxPort`-managed CMA
  pool. VVAS recognises the dmabuf and consumes it directly — the
  scaler CU walks the physical addresses via the XRT BO (buffer
  object) API, no CPU read/write involved.
- `kmssink` imports the dmabuf as a DRM prime fd and hands it to the
  DRM scanout engine without a copy. That is how the full pipeline
  ends up using only a few MB/s of DDR bandwidth for the main data
  path instead of hundreds.

This is why `CmaFree` on the Board Stats tab drops sharply when the
pipeline starts — each pipeline slot reserves ~8-12 buffers × ~1 MB
across the scaler / overlay / display pools.

---

## 2. Source selection (file vs UVC webcam)

`_source_element(video, ch)` in `server.py` branches on the prefix of
the path:

- **File** (`.mp4`, `.h264`, etc.):
  ```
  filesrc location=… name=srcN  !  qtdemux name=demuxN  !
  h264parse name=parseN  !  omxh264dec name=decN internal-entropy-buffers=3
  ```
  `qtdemux` separates the mp4 container into tracks; `h264parse`
  normalises `avc` into `byte-stream` and pushes SPS/PPS with every
  key frame; `omxh264dec` is the Xilinx OpenMAX wrapper around the VCU
  hardware codec on ZynqMP.

- **Webcam** (`/dev/videoN`):
  ```
  v4l2src device=… name=srcN do-timestamp=true  !
    video/x-h264,stream-format=byte-stream,alignment=au,
                  width=1280,height=720,framerate=60/1  !
  h264parse name=parseN  !  omxh264dec name=decN internal-entropy-buffers=3
  ```
  The camera (Luminous C50, UVC 1.5) natively emits H.264; we ask for
  720p60 because 1080p showed interframe artifacts on this camera/USB
  link. `do-timestamp=true` makes v4l2src apply running-clock timestamps
  to each buffer so downstream pacing works.

Every important element carries a `name=<prefix><ch>` suffix (e.g.,
`decN`, `scalerN`, `inferN`, `overlayN`, `ksinkN`) so per-channel
tracer output can be routed back to the right channel in the Python
parser.

### 2a. Decoder knobs worth calling out

- `omxh264dec internal-entropy-buffers=3` — the VCU's entropy decoder
  has its own internal buffer pool separate from the output pool.
  Raising the default (2) to 3 reduces the chance of a short stall
  when the output side is slow — on a 4-channel pipeline with a
  single DPU, each decoder occasionally waits 50+ ms for its output
  buffer to be consumed.
- No `config-interval` on `h264parse`: we don't inject SPS/PPS at a
  fixed interval. mp4 files already have a valid parameter-set and
  v4l2src's H.264 stream sends fresh ones on every I-frame.
- Source `do-timestamp=true` (v4l2 only): v4l2src by default uses the
  kernel's `V4L2_BUF_FLAG_TIMESTAMP_*` field; `do-timestamp=true`
  replaces that with the element's running-clock time. This matters
  when a UVC driver reports 0 timestamps (some do), otherwise the
  pipeline ends up re-clocking everything to zero and kmssink fires
  a QoS storm.

---

## 3. VVAS plugins used

All VVAS plugins on this board are installed to
`/usr/lib/gstreamer-1.0/` as `libgstvvas_x*.so`. They implement
GStreamer's standard `GstElement`/`GstBaseTransform` interfaces but
internally call the VVAS Core C library, which in turn calls XRT
(`libxrt_core.so`) to schedule work on compute units from the loaded
xclbin.

### 3.1 `vvas_xabrscaler` — hardware scaler / colour-converter

- **Compute unit:** `image_processing:{image_processing_1}` from
  `/lib/firmware/xilinx/multichannel/dpu.xclbin`.
- **Why it's here:** `refinedet_pruned_0_96` takes **480×360 BGR**;
  our source is 960×540 (or 1280×720 webcam) **NV12**. The CU resizes
  and does the colour-space conversion in hardware.
- **Capabilities:** arbitrary input/output resolutions within xclbin
  limits, NV12/I420/BGR/RGB conversion, up to 8 output pads with
  independent sizes (we only use one).
- **Pipeline use:**
  ```
  vvas_xabrscaler name=scalerN
    xclbin-location=/lib/firmware/xilinx/multichannel/dpu.xclbin
    kernel-name="image_processing:{image_processing_1}"
  ! video/x-raw,width=480,height=360,format=BGR
  ```
- **Key properties:**
  - `xclbin-location` — path to the xclbin that contains the CU. Must
    match what was loaded with `xmutil loadapp multichannel`.
  - `kernel-name` — `kernel_name:{instance_name}` — the braces select
    a specific CU instance if the xclbin has several.
  - No `ppc` / `coef-load-type` here — they come from the preprocess
    JSON when this plugin is used inside `vvas_xinfer`.

### 3.2 `vvas_xinfer` — ML inference driver

- **Compute unit:** `DPUCZDX8G:DPUCZDX8G_1` (Vitis-AI DPU).
- **What it does:** reads the JSON pointed at by `infer-config=…`,
  loads the model tree, sets up DPU I/O tensors, hands each incoming
  buffer to the DPU, collects the outputs, runs the model-specific
  post-processing (box decoding, NMS for RefineDet/SSD, heat-map
  thresholding for DenseBox, …), and attaches the results as a
  `GstInferenceMeta` on the output buffer.
- **Pipeline use:**
  ```
  vvas_xinfer name=inferN infer-config=/home/petalinux/jsons/kernel_refinedet.json
  ! <downstream>
  ```
- **Key properties / JSON fields** (see §5 for full JSON):
  - `infer-config` — the kernel-level JSON (below).
  - `preprocess-config` — optional; when set, an in-process scaler
    runs instead of an external `vvas_xabrscaler`. We don't use this
    — we chain `vvas_xabrscaler` manually because that is what the
    multichannel reference design wires up and the scaler init path
    in this build chokes when both elements try to open the same CU.
  - Inside the JSON:
    - `inference-level = 1` — one inference per frame.
    - `inference-max-queue = 30` — how many frames can wait for the
      DPU. Larger = more throughput, more latency.
    - `low-latency = false` — allow the DPU scheduler to batch. With
      `true`, every frame's result comes back immediately at the cost
      of throughput.
    - `batch-size = 1` — DPU batch. The ZCU104 DPU supports up to 3;
      we use 1 to minimise queueing latency.
    - `model-name`, `model-class`, `model-format`, `model-path` — see §4.
    - `vitis-ai-preprocess = false` — means VVAS does **not** invoke
      `xilinx::ai::configurable_dpu_task`-driven preprocessing; we
      have already done scaling/colour conversion with
      `vvas_xabrscaler`, so the DPU only runs the network.
    - `attach-ppe-outbuf = true` (RefineDet only) — keeps the
      pre-processed buffer attached, needed by the metaaffixer to
      map boxes from 480×360 back to the original 960×540.

### 3.3 `vvas_xmetaaffixer` — metadata rescaler / sync

- **What it does:** takes the inferred, preprocessed buffer on its
  **master** sink pad (480×360 BGR with `GstInferenceMeta`) and one or
  more **slave** sink pads carrying the original frame (NV12 960×540
  from the `tee`), matches them by timestamp / order, **scales every
  bounding box from master-pad coordinates to slave-pad coordinates**,
  and emits the slave buffer with the rescaled meta attached on its
  `src_slave_N` pad.
- **Why it exists:** the detection runs on a downscaled frame, but we
  want to draw boxes on the full-resolution frame for display.
- **Pipeline use (slight syntactic quirk — named pad requests):**
  ```
  vvas_xinfer … ! imaN.sink_master
  vvas_xmetaaffixer name=imaN
    imaN.src_master ! fakesink
    teeN. ! queue max-size-buffers=1 ! imaN.sink_slave_0
    imaN.src_slave_0 ! queue ! vvas_xmetaconvert …
  ```
  The `.` syntax tells `gst-parse-launch` to request pads on an
  existing named element; we add one master pair (`sink_master`/
  `src_master`) and one slave pair (`sink_slave_0`/`src_slave_0`) per
  channel. The master output goes to `fakesink` because we don't need
  the downscaled BGR buffer any more — only the scaled metadata.
- **Pad naming conventions:** `sink_master` and `src_master` are
  *static* pads declared by the element. `sink_slave_N` / `src_slave_N`
  are *request* pads — one new slave pair is instantiated on demand
  each time the `.` syntax or `gst_element_request_pad` is invoked.
  VVAS supports up to 8 slave pairs per metaaffixer; we only use one.
- **Metadata math:** let the master buffer's video-info be
  `(Wm, Hm)` and a slave's `(Ws, Hs)`. For each inference prediction
  `(x, y, w, h)` the rescaled box is
  `(x·Ws/Wm, y·Hs/Hm, w·Ws/Wm, h·Hs/Hm)`. Masks and keypoints use
  the same affine transform. There's no interpolation — the master
  always has lower-or-equal resolution than the slave.
- **Buffer pairing:** internally it holds a small ring buffer of
  recent master metadata keyed by PTS; when a slave buffer arrives,
  it pops the master entry with the nearest PTS. This means the
  slave queue (`queue max-size-buffers=1`) is critical — bigger
  values would let slave frames arrive before inference, and the
  metaaffixer would pair them with *old* master metadata, making
  the boxes lag visibly.

### 3.4 `vvas_xmetaconvert` — struct → overlay hints

Translates the generic `GstInferenceMeta` (which is a nested struct
describing classes/boxes/masks/labels) into `GstVvasOverlayMeta` — a
simpler struct that the overlay element renders. The JSON it consumes
defines **per-class colour, label, visibility, masking** (see §5).

### 3.5 `vvas_xoverlay` — draw boxes / labels / masks

Software rasteriser that reads `GstVvasOverlayMeta` from the buffer,
renders the rectangles / text / optional masks onto the NV12 frame in
place. It doesn't touch the DPU or any CU. On NV12 buffers it writes
Y and UV planes directly using the accompanying cairo-like helper.

### 3.6 Plugins we explicitly *don't* use (any more)

- `vvas_xskipframe` — drops every Nth frame pre-inference. We
  measured this costs more than it saves on this workload because of
  extra metadata-tagging overhead (see earlier bench).
- `vvas_xtracker` — IOU/sort tracker that fills boxes between
  inference frames. Only useful if combined with skipframe.
- `vvas_xreorderframe` — re-sorts frames split across skip/infer
  paths. Only relevant with skipframe; broke the pipeline here
  because of a bug in buffer pool negotiation.
- `vvas_xfilter` / `vvas_xfuncreg` — generic hooks to run a
  user-supplied shared library on buffers. Not needed for the two
  models we ship.

---

## 4. ML models

Both models live under `/usr/share/vitis_ai_library/models/` and ship
as a **Vitis-AI model directory**:
```
<model-name>/
    <model-name>.xmodel          # compiled DPU graph (serialised XIR)
    <model-name>.prototxt        # Vitis-AI parameters (anchors, means, std, classes)
    meta.json                    # library-wide manifest (checksum, etc.)
```

### 4.1 `refinedet_pruned_0_96` — person detection

- **Architecture:** RefineDet. Two-stage refinement of SSD-style
  anchors: an ARM (Anchor Refinement Module) produces proposals, an
  ODM (Object Detection Module) classifies and further refines them.
  Backbone is VGG-16 truncated + extra stages. The `pruned_0_96`
  variant is pruned to ~0.96 GOPs; person-only (COCO `person` class).
- **Input:** 480 × 360, BGR, float32 normalised by subtracting
  `[104, 117, 123]` (ImageNet BGR means); `model-format: BGR` and
  `float-feature: 1` in the JSON select this path in the Vitis-AI
  library.
- **Output:** list of `(x, y, w, h, confidence)` persons. NMS is
  handled internally by the Vitis-AI RefineDet postprocessor.
- **Perf on KV260 (1 channel, no skipframe):** ~9 ms infer, ~10 ms
  scaler, ~1 ms overlay (measured from the GStreamer tracer).

### 4.2 `densebox_640_360` — face detection

- **Architecture:** DenseBox — anchor-free, fully convolutional.
  Outputs a per-pixel face-presence heat-map and a per-pixel box
  regression; peaks above threshold become face boxes.
- **Input:** 640 × 360, BGR, same mean-subtraction preprocessing.
- **Output:** list of `(x, y, w, h, confidence)` faces.
- **Why `max-objects: 10`:** the DPU postprocessor returns the top-K
  peaks sorted by confidence; we cap at 10 to keep the overlay legible.

### 4.3 Where the model lookup happens

`vvas_xinfer` is a thin wrapper around the Vitis-AI high-level library
(`libvitis_ai_library`). The JSON's `model-class` field picks which
class is instantiated — `REFINEDET`, `FACEDETECT`, `SSD`, `YOLOV3`,
etc. — and that class internally does:

1. Open `<model-path>/<model-name>/<model-name>.prototxt` for
   parameters.
2. Locate the matching `.xmodel` next to it.
3. Use `xir::Subgraph::get_attr("dpu")` to find DPU subgraphs,
   create a `vart::Runner` for each, and hold it open for the life of
   the pipeline.

---

## 5. JSON descriptor catalogue

All JSONs referenced below live in `/home/petalinux/jsons/`. Server
maps `model_key → (infer_json, meta_json)` in the `MODELS` dict in
`server.py`.

### 5.1 `kernel_refinedet.json` — infer config for RefineDet

```json
{
  "attach-ppe-outbuf" : true,
  "inference-level"   : 1,
  "low-latency"       : false,
  "inference-max-queue": 30,
  "kernel" : {
    "config": {
      "batch-size"          : 1,
      "model-name"          : "refinedet_pruned_0_96",
      "model-class"         : "REFINEDET",
      "model-format"        : "BGR",
      "model-path"          : "/usr/share/vitis_ai_library/models/",
      "vitis-ai-preprocess" : false,
      "performance-test"    : false,
      "max-objects"         : 3,
      "float-feature"       : 1,
      "segoutfactor"        : 1.0,
      "seg-out-format"      : "BGR",
      "debug-level"         : 1
    }
  }
}
```

Top-level fields are consumed by `vvas_xinfer`; the `kernel.config`
block is passed verbatim to the Vitis-AI library's
`configurable_dpu_task` initialiser.

### 5.2 `kernel_densebox.json` — infer config for DenseBox

Same shape, different `model-class` / `model-name` / `max-objects`.
No `attach-ppe-outbuf` because densebox's postprocessor does not need
the pre-processed buffer during overlay.

### 5.3 `metaconvert_config.json` — person overlay style

```json
{
  "config": {
    "display-level": 0,
    "font-size": 1, "font": 3, "thickness": 2, "radius": 5,
    "mask-level": 0, "y-offset": 0,
    "label-filter": [ "class", "probability" ],
    "classes": [
      { "name": "person", "blue": 0, "green": 255, "red": 0, "masking": 0 }
    ]
  }
}
```

Consumed by `vvas_xmetaconvert`. Each `classes[]` entry binds a
detector class name → a colour + optional mask. The
`label-filter` controls which fields from `GstInferenceMeta` are
rendered next to each box — here "class" (the name) and
"probability" (the confidence).

### 5.4 `metaconvert_facedetect.json`

Same shape, only class is `"face"` with blue = 255 so face boxes
appear blue on screen.

### 5.5 `preprocess_refinedet.json` — standalone preproc (not used)

```json
{
  "xclbin-location": "/lib/firmware/xilinx/multichannel/dpu.xclbin",
  "device-index"   : 0,
  "kernel": {
    "kernel-name": "image_processing:{image_processing_1}",
    "config": { "ppc": 2, "in-mem-bank": 0, "out-mem-bank": 0 }
  }
}
```

This is the schema `vvas_xinfer`'s `preprocess-config=` property
accepts. We keep the file around as a reference but do not use it —
`vvas_xabrscaler` does the same work explicitly in the pipeline, which
is the pattern the KV260 multichannel reference design uses and which
plays nicely with our scaler-CU build.

### 5.6 `/lib/firmware/xilinx/multichannel/dpu.xclbin` — bitstream

Not a JSON but conceptually the "hardware config file". Contains the
bitstream for the programmable logic region + metadata describing the
compute units inside it. Loaded by `xmutil loadapp multichannel`
(a one-shot action at boot) and referenced by both
`vvas_xabrscaler.xclbin-location` and `vvas_xinfer` via the Vitis-AI
library's XRT calls.

Run `xbutil examine -d 0 --report dynamic-regions` to see which CUs
it exposes. On this board:
```
image_processing:image_processing_1   0xa0020000   (HW scaler)
DPUCZDX8G:DPUCZDX8G_1                 0xa0010000   (Vitis-AI DPU)
```

---

## 6. Built-in GStreamer facilities we lean on

These are all core or plugins-good/-bad — nothing custom.

### 6.1 Multi-branch topology

- `tee`: one input, N outputs, every buffer ref-counted and pushed to
  all branches. We use it twice per channel — once after the decoder
  to split into detection + display, once after the overlay to split
  into `kmssink` + `fpsdisplaysink`.
- `queue` in front of every branch: tee is push-based; each branch
  runs in its own streaming thread, and `queue` is the thing that
  provides a thread boundary and buffering.
- Named pad requests (`elemN.pad_name`): used for `tee` src pads
  (`t0.`), `vvas_xmetaaffixer`'s master/slave pad pairs, and
  `input-selector` (in the python-gst variant).

### 6.2 Clock & sync

- `kmssink sync=false` + `fpsdisplaysink sync=false`: frames are
  rendered as they arrive, not at PTS time. We keep this because
  DPU throughput can't match 30 fps when multiple channels run
  detection — `sync=true` causes kmssink to drop late buffers and
  stutter. The downside is small PTS-vs-clock drift, but nobody's
  watching the mp4 as a movie — they're watching detection run.
- `kmssink can-scale=true`: lets the xlnx DRM plane scale the buffer
  into the render rectangle. Needed because source size (1280×720 or
  960×540) almost never matches the quadrant.

### 6.3 GST tracers (for the Performance panel)

When the subprocess is launched, we set:

```bash
GST_TRACERS="latency(flags=pipeline+element)"
GST_DEBUG="GST_TRACER:7"
GST_DEBUG_NO_COLOR=1
```

- `latency` is a core tracer. With `flags=pipeline` it emits a line
  **per buffer** at every sink, with fields `src-element`,
  `sink-element`, `time` (ns source→sink). With `flags=element` it
  also emits per-element push times.
- The server's `_stream_logs` parses two regexes:
  ```python
  PIPELINE_LAT_RE = r'latency,\s+.*?src-element=…sink-element=…time=\(guint64\)(\d+)'
  ELEMENT_LAT_RE  = r'element-latency,\s+.*?element=\(string\)(\w+)…time=\(guint64\)(\d+)'
  ```
- It attributes each sample to a channel by looking at the trailing
  digit of the element name (`infer2` → CH 2).
- Rolling windows of 30 samples per (channel, stage) are kept in
  memory; the API returns the mean of each window. This is published
  as `latency` under `/api/pipeline/status` and rendered in the
  Detection tab.

### 6.4 `fpsdisplaysink`

- A `GstBin` that wraps a sink (here `fakesink sync=false`) and
  periodically writes a `last-message` property of the form:
  `rendered: N, dropped: M, current: X.XX, average: Y.YY`.
- We parse that with `FPS_RE` and publish per-channel `fps` and
  `mean_fps`.

### 6.5 Subprocess lifecycle

- A single `gst-launch-1.0 -v …` is spawned from `server.py` via
  `subprocess.Popen(..., start_new_session=True)`. That fresh session
  is what lets `os.killpg(getpgid(pid), SIGTERM)` cleanly take the
  whole pipeline down on Stop/Reconfigure.
- `stdout` and `stderr` are merged and piped into a Python thread
  that does three things: logs a capped deque for the UI,
  pattern-matches fps, and pattern-matches tracer lines.

### 6.6 Control flow: detect ON/OFF

A channel's "Detect ON" and "Detect OFF" are two different
sub-pipelines (with vs. without the VVAS chain). Because `gst-launch`
can't be reconfigured live, toggling detection **rebuilds the whole
command** and does a stop→start cycle — ~1 s display blank, then
running. The other pipeline variant (`server_pygst.py`) keeps both
branches wired through an `input-selector` for truly seamless switch;
it's kept for future experiments but currently has a buffer-flow
issue where the passthrough branch freezes on the switch.

---

## 7. XRT / compute-unit ownership

Everything VVAS-accelerated ends up calling XRT:

- `xrt::device` → opened once per process.
- `xrt::xclbin` → `xrt::device::load_xclbin(…)` is skipped because
  `xmutil loadapp multichannel` has already done it at boot; we just
  open the pre-loaded context.
- `xrt::ip` / `xrt::kernel` / `xrt::run` → per CU. The
  `multichannel` platform grants the DPU CU **exclusively** to a
  single process. That is the reason `server.py` runs a single
  combined `gst-launch` even though per-channel subprocesses would
  otherwise let us toggle each one independently — you'd immediately
  hit `waiting for process to release the resource: DPU_0` on
  channels 2-4.

This is also why the "Compute Units" panel in the Board Stats tab
only shows two CUs (`image_processing`, `DPUCZDX8G`): the xclbin
exposes only these two. When the pipeline is running, their
"Usage" counter increments on every frame kick and you can see them
flip from `IDLE` to `BUSY`.

---

## 8. Startup sequence, end-to-end

1. Boot. Systemd brings up the platform: `xmutil loadapp multichannel`
   loads `dpu.xclbin` onto the FPGA and creates the device tree for
   the CUs.
2. User visits `http://<board-ip>:5000/`, the Flask server returns
   `index.html`.
3. User clicks **Start**; the browser `POST`s the channel config.
4. `PipelineManager.start()` calls `build_gst_command()` which
   stringifies 1-4 channel fragments into one `gst-launch-1.0 -v`
   command with `GST_TRACERS=…`.
5. The subprocess forks. Inside:
   - `gst-parse-launch` builds the element graph, requesting pads.
   - `vvas_xinfer` reads its JSON, loads the model via Vitis-AI,
     opens the DPU CU.
   - `vvas_xabrscaler` opens the scaler CU.
   - `kmssink` opens `/dev/dri/card0`, requests DRM plane `N`.
   - Pipeline transitions NULL → READY → PAUSED → PLAYING.
6. First decoded frame arrives ~20-30 ms later; first inference
   result ~10 ms after that (one extra `vvas_xinfer.inference-max-queue`
   slot is filled per frame before the first pops out).
7. Every buffer pushed into a sink pad triggers the `latency` tracer;
   the tracer writes a line to stdout; Python parser records it.
   `fpsdisplaysink` reports every 500 ms.
8. Browser polls `/api/pipeline/status` every 2 s and updates the
   Performance panel (FPS + Latency Breakdown). Browser polls
   `/api/stats` every 1 s while on the Board Stats tab.
9. On **Stop** or on EOS, the subprocess is killed, DPU lock is
   released, display goes back to the VT's background.

---

## 9. Quick reference table

| Stage            | GStreamer element                  | Named in pipeline as | JSON that configures it         | Latency field in UI |
|------------------|-----------------------------------|----------------------|---------------------------------|---------------------|
| File demux       | `qtdemux`                         | `demuxN`             | —                                | (not measured)      |
| H.264 parse      | `h264parse`                       | `parseN`             | —                                | (not measured)      |
| VCU decode       | `omxh264dec`                      | `decN`               | —                                | `vcu_ms`            |
| Preproc / scale  | `vvas_xabrscaler`                 | `scalerN`            | `dpu.xclbin` CU params           | `preproc_ms`        |
| ML inference     | `vvas_xinfer`                     | `inferN`             | `kernel_<model>.json`            | `infer_ms`          |
| Meta sync        | `vvas_xmetaaffixer`               | `imaN`               | (no JSON)                        | (not measured)      |
| Meta → overlay   | `vvas_xmetaconvert`               | `metaN`              | `metaconvert_<style>.json`       | `meta_ms`           |
| Draw             | `vvas_xoverlay`                   | `overlayN`           | (reads overlay meta)             | `overlay_ms`        |
| Display          | `kmssink`                         | `ksinkN`             | (plane-id, render-rectangle)     | (end-point)         |
| Rate measurement | `fpsdisplaysink`                  | `fpsN`               | `fps-update-interval`            | FPS panel           |

---

## 9a. Flask server endpoints (contract with the browser)

| Route                               | Method | Purpose                                                    |
|-------------------------------------|--------|-----------------------------------------------------------|
| `/`                                 | GET    | Serves `index.html`.                                      |
| `/api/models`                       | GET    | Lists available models (id → label).                      |
| `/api/files`                        | GET    | Lists `*.mp4/.h264/.mov` under `--video-dir`.             |
| `/api/webcams`                      | GET    | Lists capture devices via `gst-device-monitor Video/Source`. |
| `/api/display`                      | GET    | Returns `{width, height}` of the HDMI output.             |
| `/api/pipeline/start`               | POST   | Builds and launches a combined pipeline.                  |
| `/api/pipeline/stop`                | POST   | SIGTERMs the pipeline process-group, then SIGKILLs.       |
| `/api/pipeline/reconfigure`         | POST   | Stop-then-start with new per-channel detect flags etc.    |
| `/api/pipeline/status`              | GET    | Running state, PID, uptime, config, fps[], latency{}.     |
| `/api/pipeline/logs`                | GET    | Recent lines from the pipeline's captured stdout.         |
| `/api/pipeline/preview`             | GET    | Pretty-prints the gst-launch that would be built.         |
| `/api/stats`                        | GET    | Board stats: power, CPU, temps, voltages, DPU, mem.       |

### 9b. Alternative server variants kept in-tree

| File                 | Mode                                                         | When you want it                                 |
|----------------------|--------------------------------------------------------------|--------------------------------------------------|
| `server.py`          | Combined `gst-launch-1.0` subprocess. Stop→start on toggle.  | Default. Stable; ~1 s detection-toggle blank.    |
| `server_combined.py` | Identical to `server.py`. Kept as labelled backup.           | Safety net if you want to revert experiments.    |
| `server_pygst.py`    | One python-gst `Gst.parse_launch` with per-channel `input-selector` between a detection branch and a passthrough branch. Toggle = `active-pad` live flip. | Prototype for zero-gap detection toggle. Currently the passthrough branch stalls after the switch (GStreamer 1.18 selector corner case). Kept for further work. |

All three speak the same `/api/*` contract, so swapping them does not
require any change to the browser code.

---

## 9c. Observed performance (this board, 2026-04-11)

Rough numbers, measured via the latency tracer and fpsdisplaysink on
`walking.mp4` (960×540, 30 fps source):

| Scenario                          | FPS / channel  | End-to-end latency |
|-----------------------------------|----------------|---------------------|
| 1 channel, RefineDet, detect ON   | 60 (decoder-limited) | ~100 ms        |
| 4 channels, RefineDet, detect ON  | ~23 each             | ~200 ms        |
| 4 channels, RefineDet, detect OFF | ~60 each (no DPU)    | ~40 ms         |
| 1 channel, DenseBox, detect ON    | ~50                   | ~80 ms         |

Per-stage timing for 1-ch RefineDet:
- VCU decode (`omxh264dec`): ~5-8 ms (one I-frame can spike to 50+ ms)
- Scaler (`vvas_xabrscaler`): ~10 ms
- Infer (`vvas_xinfer` RefineDet): ~9 ms
- Overlay (`vvas_xoverlay`): ~1 ms
- Metaconvert: <1 ms

4-channel numbers roughly quadruple on the scaler/infer stages because
both the image_processing and DPU CUs are time-multiplexed.

---

## 9d. Known issues & gotchas encountered

1. **UVC H.264 1080p shows macroblock artifacts on this camera** —
   USB bandwidth or camera encoder limits. Capped webcam at 720p.
2. **Multiple `gst-launch` processes all running detection fail.**
   XRT grants the DPU CU exclusively per process. Attempting
   per-channel subprocesses yields
   `waiting for process to release the resource: DPU_0` for channels
   2-4. Hence the single combined gst-launch design.
3. **`vvas_xreorderframe` in our xclbin config** breaks scaler pool
   negotiation — hitting "Couldn't get scaler props" in gstreamer
   debug. Not used.
4. **`kmssink sync=true`** with 4-ch detection drops heavily because
   DPU output rate (~23 fps/ch) can't match wall-clock 30 fps. We use
   `sync=false` to keep rendering smooth at the cost of small drift.
5. **`can-scale=true`** is required when source resolution doesn't
   match the render rectangle (e.g. webcam 1280×720 → 960×540
   quadrant); the xlnx DRM plane scaler handles it cleanly.
6. **EOS on file source.** Walking.mp4 is ~8 s; `_handle_eos_stop`
   transitions the pipeline to NULL when EOS arrives. Restart to
   play again (loop support was removed on request).
7. **`fps` element on live `v4l2src`** sometimes reports `current:
   1.5, average: 1.5` for the first measurement right after state
   changes. We ignore the first two samples in the UI.

---

## 10. References

- VVAS 3.0 plugins:
  <https://xilinx.github.io/VVAS/main/build/html/docs/common/gstreamer_plugins/>
- Vitis-AI Library (model containers & configurable DPU tasks):
  <https://github.com/Xilinx/Vitis-AI>
- XRT programming model (xrt::device / xrt::kernel / xrt::run):
  <https://xilinx.github.io/XRT/master/html/xrt_native_apis.html>
- GStreamer core tracers (`latency`, `stats`, `rusage`):
  <https://gstreamer.freedesktop.org/documentation/additional/design/tracing.html>
- `fpsdisplaysink` element reference:
  <https://gstreamer.freedesktop.org/documentation/debug/fpsdisplaysink.html>
- KV260 multichannel app (the `dpu.xclbin` we load):
  <https://xilinx.github.io/kria-apps-docs/kv260/2022.1/build/html/docs/multichannel/docs/multichannel_ml_landing.html>

Document last updated: 2026-04-11.
