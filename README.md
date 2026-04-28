# embeddedEgdeVisionAI

Real-time multichannel edge-vision AI on the AMD Kria KV260 Vision AI Starter Kit.

The system performs person and face detection on four 1080p video streams in
parallel, evaluates operator-drawn zones in user space and signals a real-time
alert through a Cortex-R5 firmware that drives an LED over PMOD AXI GPIO. The
APU-to-RPU actuation hot path uses a TCM mailbox at `0xFFE20000` with a
magic-value handshake; measured one-way latency is **9.30 us at the median**
(versus 341.76 us for the OpenAMP RPMsg path the project started with).

## Highlights

* Four channels at 1080p, RefineDet + DenseBox cascade on the DPUCZDX8G B3136
  at 275 MHz aclk.
* Per-channel **23.85 fps** sustained, aggregate **95.4 fps**, validated over
  a twelve-hour unattended soak.
* APU dashboard in Python / Flask with login, channel control and a
  Server-Sent Events alert stream.
* Custom GStreamer plug-in (`zoneguard`) with rectangle hit-test and a
  three-frame hysteresis state machine.
* Cortex-R5 standalone firmware with a tight while-loop polling the alert page
  in TCM and writing AXI GPIO. No RTOS, no DDR access on the hot path.

## Repository layout

* `hardware/` - Vivado / Vitis platform, HLS image-processing kernel,
  device-tree overlay sources, build Makefiles.
* `software/` - PetaLinux BSP user customisation, R5 firmware, Flask
  application, GStreamer plug-in, dashboard front-end, shell scripts,
  evaluation logs.
* `ml_models/` - INT8 xmodels for the DPU plus DPU runtime config.
* `docs/` - design notes, latency report, soak results, engineering
  write-ups.

## Build

See `hardware/Build_Makefiles/README.md` for the FPGA + Linux image build
chain. The PetaLinux BSP under `software/OS/PetaLinux_BSP/` rebuilds with
`petalinux-build`. The R5 firmware in `software/OS/C_R5_Firmware/` is built
through Vitis 2022.2 against the Standalone BSP.

## Run

On the KV260 booted from the prepackaged SD image:

    cd software/Host_Application/Shell_Scripts
    sudo ./install-firmware-packages.sh
    ./run.sh

The dashboard is reachable at `http://<board-ip>:5000` (default operator
credentials in `software/Host_Application/Python_Flask/README.md`).

## Toolchain

PetaLinux 2022.2, Vivado 2022.2, Vitis 2022.2, Vitis AI 3.0, GStreamer 1.18
with Xilinx VVAS 3.0 SDK.

## Project context

Final-year (Level 6) Production Project, Spring 2026. Supervised by
Mahesh Maharjan.
