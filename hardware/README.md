# Hardware

FPGA hardware sources for the KV260 multichannel platform.

* `Build_Makefiles/` - top-level Makefiles plus the bring-up and OpenAMP deploy
  scripts.
* `Vivado_Hardware/` - Vivado block-design TCL and constraint sources for the
  base platform.
* `Vitis_Platform/` - Vitis platform definition, the HLS image-processing
  kernel and the multichannel overlay (DPU, image-processing, video mixer).
* `Device_Tree_Overlays/` - the runtime overlay (`.dtbo`) plus its DTSI source
  for the multichannel + OpenAMP + AXI GPIO design.
* `Prepackaged_SD_Image/` - reproducible flash image (large binary, see the
  release artifacts on GitHub - not committed inline).
