# Vitis_Platform

The Vitis platform definition (XPFM), the XSCT scripts, and the multichannel
overlay sources that v++ links into `dpu.xclbin`.

```
Vitis_Platform/
├── kv260_vcuDecode_vmixDP/   # XPFM platform metadata
│   └── *.xpfm                 # platform descriptor consumed by v++ link
├── xsct/                     # XSCT (Xilinx SDK CLI) scripts and platform data
├── scripts/                  # platform build scripts
│   ├── pfm.tcl                # platform definition Tcl
│   └── boot/, image/          # boot-image and rootfs scripts
├── multichannel_overlay/     # the v++ link sources for the production overlay
│   ├── Makefile               # top-level build (v++ link + dtc + bif)
│   ├── dpu_conf.vh            # DPU configuration: B3136, low-RAM, URAM enabled
│   ├── image_processing.cfg   # v++ link config (clock freqs, port maps)
│   ├── image_processing/      # HLS source for the image-processing kernel (NV12->BGR + resize + mean-subtract)
│   ├── kernel_xml/            # XRT kernel descriptors
│   ├── firmware/              # arch.json, shell.json, image_processing.cfg, bitstream.bif
│   ├── multichannel-gpio/     # extras for the GPIO-enabled overlay variant
│   └── create_imgproc_config.sh  # generates image_processing_config.h from the .cfg
```

## Build the production xclbin

```sh
cd multichannel_overlay
make    # synthesises the image-processing HLS, runs v++ link, packs xclbin
```

Output: `dpu.xclbin` (~11 MB) — installed at
`/lib/firmware/xilinx/multichannel-openamp-gpio/dpu.xclbin` on the board.

## Per-stage timing of the build

- HLS synthesis of `image_processing` ~5 min
- v++ link ~25 min
- DTC compile of overlays <10 s
- BIF/bootgen <5 s

The PG338 (DPU product guide), UG1393 (Vitis), and the Vivado hardware-handoff
file `kv260_vcuDecode_vmixDP.hwh` from `Device_Tree_Overlays/` are required
inputs.
