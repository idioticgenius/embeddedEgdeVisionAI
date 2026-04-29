# multichannel-openamp-gpio — Required Packages

Package inventory for the **multichannel-openamp-gpio** firmware project as
installed in the current PetaLinux build on the KV260. Versions are exact
(`rpm -qa`) so the same build can be reproduced.

## Build base

| Item | Value |
|---|---|
| Distro | PetaLinux 2022.2 update-5 (honister), 2023-04-01 |
| Kernel | `5.15.36-xilinx-v2022.2` |
| Target | Kria K26 / KV260 (`xilinx_k26_kv`, cortexa72/cortexa53) |
| Vitis AI | 3.0 |
| VVAS | 3.0 (core 1.0+git0+542935a020) |
| XRT | 202220.2.14.0 |
| GStreamer | 1.18.5 |

## Vitis AI / DPU runtime

- `vitis-ai-library-3.0-r0.0.cortexa72_cortexa53`
- `vitis-ai-library-dev-3.0-r0.0.cortexa72_cortexa53`
- `vart-3.0-r0.0.cortexa72_cortexa53`
- `vart-dev-3.0-r0.0.cortexa72_cortexa53`
- `xrt-202220.2.14.0-r0.1.cortexa72_cortexa53`
- `xrt-dev-202220.2.14.0-r0.1.cortexa72_cortexa53`

Models on disk (from `vitis-ai-library`):
- `refinedet_pruned_0_96` — 480×360, REFINEDET, 5.08 GOPs (person detect)
- `densebox_640_360` — 640×360, DENSE_BOX/FACEDETECT, 1.11 GOPs (face detect)

DPU target: `DPUCZDX8G_ISA1_B3136` (matches the xclbin in this firmware).

## VVAS GStreamer stack

- `vvas-core-1.0+git0+542935a020-r0.0.cortexa72_cortexa53`
- `vvas-core-dev-1.0+git0+542935a020-r0.0.cortexa72_cortexa53`
- `vvas-utils-3.0-r0.0.cortexa72_cortexa53`
- `vvas-utils-dev-3.0-r0.0.cortexa72_cortexa53`
- `vvas-gst-3.0-r0.0.cortexa72_cortexa53`
- `vvas-gst-dev-3.0-r0.0.cortexa72_cortexa53`
- `vvas-accel-libs-3.0-r0.0.cortexa72_cortexa53`
- `packagegroup-petalinux-vvas-1.0-r0.0.noarch`
- `libjansson4-2.14.0+git0+684e18c927-r0.0.cortexa72_cortexa53` (JSON parser used by VVAS)
- `libjansson-dev-2.14.0+git0+684e18c927-r0.0.cortexa72_cortexa53`

GStreamer elements actually used in the pipeline (`server.py`):
`v4l2src`, `filesrc`, `qtdemux`, `h264parse`, `omxh264dec`, `tee`, `queue`,
`vvas_xinfer`, `vvas_xmetaconvert`, `vvas_xoverlay`, `vvas_xmetaaffixer` (`ima`),
`videoconvert`, `videoscale`, `capsfilter`, `kmssink`, `fpsdisplaysink`,
`fakesink`, plus the locally built `zoneguard` plugin.

Backing GStreamer packages:
- `gstreamer1.0-1.18.5+git0+e483cd3a08-r0.0.cortexa72_cortexa53`
- `gstreamer1.0-plugins-base-*-1.18.5+git0+ce156424eb-r0.0.zynqmp_ev`
  (`app`, `videoconvert`, `videoscale`, `videotestsrc`, `tcp`, `compositor`,
  `meta`, `apps`)
- `gstreamer1.0-plugins-good-*-1.18.5+git0+adc0e0329d-r0.0.cortexa72_cortexa53`
  (`video4linux2`, `isomp4`, `rtp`, `rtsp`, `rtpmanager`, `udp`, `multifile`,
  `videocrop`, `videofilter`, `videobox`, `videomixer`, `meta`)
- `gstreamer1.0-plugins-bad-*-1.18.5+git0+cadd034743-r0.0.cortexa72_cortexa53`
  (`videoparsersbad` for h264parse, `kms` for kmssink, `debugutilsbad` for
  fpsdisplaysink, `mediasrcbin`, `videofiltersbad`, `meta`)
- `gstreamer1.0-omx-1.18.5+git1+e0508d33db-r0.0.zynqmp` (omxh264dec)

## OpenAMP / RPU

- `packagegroup-petalinux-openamp-1.0-r0.0.noarch`
- `packagegroup-petalinux-openamp-echo-test-1.0-r0.0.noarch`
- `packagegroup-petalinux-openamp-rpc-demo-1.0-r0.0.noarch`
- `packagegroup-petalinux-openamp-matrix-mul-1.0-r0.0.noarch`
- `libmetal-2022.1+git0+bee059dfed-r0.0.zynqmp`
- `libmetal-demos-2022.1+git0+bee059dfed-r0.0.zynqmp`
- `openamp-demo-notebooks-0.1-r0.0.zynqmp`
- `rpmsg-echo-test-1.0-r0.0.cortexa72_cortexa53`
- `rpmsg-mat-mul-1.0-r0.1.cortexa72_cortexa53`
- `rpmsg-proxy-app-1.0-r0.0.cortexa72_cortexa53`

Kernel modules (rpmsg / remoteproc plumbing):
- `kernel-module-rpmsg-core-5.15.36-xilinx-v2022.2`
- `kernel-module-rpmsg-char-5.15.36-xilinx-v2022.2`
- `kernel-module-rpmsg-ns-5.15.36-xilinx-v2022.2`
- `kernel-module-virtio-rpmsg-bus-5.15.36-xilinx-v2022.2`

R5 firmware: `/lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw` (built from
`rpu-test/code/` sources; not packaged via RPM).

## FPGA / accelerator app management

- `xmutil-1.0-r0.1.cortexa72_cortexa53` (loadapp / unloadapp / dp_bind)
- `dfx-mgr-1.0-r0.2.cortexa72_cortexa53`
- `fpga-manager-script-1.0-r0.0.cortexa72_cortexa53`
- `fpga-manager-util-xilinx+git0+24d29888d0-r0.0.xilinx_k26_kv`
- `fpga-manager-util-base-xilinx+git0+24d29888d0-r0.0.xilinx_k26_kv`
- `device-tree-xilinx+v2022.2+git0+device+tree-r0.0.xilinx_k26_kv`
- `linux-xlnx-udev-rules-1.0-r0.2.cortexa72_cortexa53`

## Video / display stack

- `kernel-module-xlnx-vcu-5.15.36-xilinx-v2022.2` (VCU H.264 decoder)
- `libvcu-xlnx-1.0.0+xilinx+v2022.2+git0+3c59dede19-r0.0.zynqmp`
- `libomxil-xlnx-1.0.0+xilinx+v2022.2+git0+6752f5da88-r0.0.zynqmp` (OMX plugin)
- `libmali-xlnx-r9p0+01rel0-r0.0.zynqmp_ev` (Mali GPU; used for DP/v_mix path)

KMS plane IDs 34–37 are the four DP overlay planes targeted by `kmssink`.

## Board telemetry (Board Stats tab)

- `xlnx-platformstats-1.0-r0.0.cortexa72_cortexa53`
- `xlnx-platformstats-python-1.0-r0.0.cortexa72_cortexa53`
- `kria-dashboard-1.0-r0.0.cortexa72_cortexa53` (reference dashboard; not used,
  but the per-rail labels in `server.py` mirror its source)

## Python (system site-packages)

Used by `server.py`, `rpu_bridge.py`, `measure_latency.py`,
`test_rpu_latency.py`:

- `Flask 3.1.3`
- `Werkzeug 3.1.8`
- `numpy 1.21.2`
- `pyserial 3.5`
- `xlnx_platformstats` (provided by `xlnx-platformstats-python` above)
- stdlib only otherwise (`mmap`, `struct`, `socket`, `subprocess`,
  `threading`, `faulthandler`)

## Firmware artefacts shipped in this app dir

`/lib/firmware/xilinx/multichannel-openamp-gpio/`:
- `dpu.xclbin` — DPU + image-processing CUs
- `kv260_vcuDecode_vmixDP_wrapper.bit.bin` — PL bitstream
- `kv260-multichannel-gpio.dtbo` — overlay (gpio + openamp/RPU bindings)
- `arch.json`, `bitstream.bif`, `image_processing.cfg`, `shell.json`

## Custom build artefacts (not from RPM)

- `zoneguard` GStreamer plugin — `/usr/lib/gstreamer-1.0/libgstzoneguard.so`,
  built locally from `code/zoneguard/`
- R5 firmware ELF — `/lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw`, built
  from the openamp R5 sources
- VVAS kernel JSONs in `latest-project-files/jsons/`:
  `kernel_refinedet.json`, `kernel_densebox.json`,
  `metaconvert_config.json`, `metaconvert_facedetect.json`
