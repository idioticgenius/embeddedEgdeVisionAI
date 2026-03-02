# embeddedEgdeVisionAI

Real-time multichannel edge-vision AI on the AMD Kria KV260 Vision AI Starter Kit.
The system performs person and face detection on four 1080p video streams in
parallel, evaluates operator-drawn zones in user space and signals a real-time
alert through a Cortex-R5 firmware that drives an LED over PMOD AXI GPIO.

## Repository layout

* `hardware/` - Vivado / Vitis platform sources, device-tree overlays, build
  Makefiles and the HLS image-processing kernel.
* `software/` - PetaLinux BSP, the standalone R5 firmware, the Python Flask
  host application, the C GStreamer zoneguard plug-in, the operator dashboard
  front-end and the shell scripts that drive the bench.
* `ml_models/` - DPU-deployable INT8 xmodels (RefineDet for persons, DenseBox
  for faces) and DPU configuration metadata.
* `docs/` - design notes, latency reports and engineering write-ups produced
  during development.

## Status

Active development. Supervised academic project, Production Project module,
Spring 2026.
