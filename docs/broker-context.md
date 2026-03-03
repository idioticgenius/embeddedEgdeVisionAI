# Broker context for dispatched subagents

**Purpose:** any dispatched subagent that runs host-side probes should read this first via `cat ~/Documents/abhidan/Sem_8/productionProject/projectFiles/kria-vitis-platforms/kv260/overlays/examples/multichannel/rpu-test/broker-context.md`. It carries durable facts so prompts can stay terse.

Last updated: 2026-04-19 (probe 4 — xpfm correction, rebuild in progress)

---

## Host identity

- User: `abhidan`
- IP: `192.168.1.77`
- Hostname: `abhidan-G7-7588`
- Key auth is set up from the board (Kria KV260). Subagents running **on the board** ssh with:
  ```
  ssh -i ~/.ssh/id_ed25519 abhidan@192.168.1.77 '<cmd>'
  scp -i ~/.ssh/id_ed25519 <file> abhidan@192.168.1.77:<path>
  ```
  `-i` is mandatory — dropbear's default key name is `id_dropbear`, not `id_ed25519`.

## Anchoring paths on host

- **Project root:** `/home/abhidan/Documents/abhidan/Sem_8/productionProject/projectFiles/kria-vitis-platforms/kv260`
  - `overlays/examples/multichannel/` — active overlay (VVAS 3.0 + 1 DPU)
  - `petalinux/kv260_project/` — PetaLinux 2022.2 tree (51 GB)
  - `platforms/kv260_vcuDecode_vmixDP/` — Vitis platform (local build)
  - `platforms/xilinx_kv260_vcuDecode_vmixDP_202220_1/` — stock Xilinx platform
  - `platforms/vivado/` — 4 Vivado projects (vcuDecode_vmixDP, ispMipiRx_vcu_DP, ispMipiRx_rpiMipiRx_DP, ispMipiRx_vmixDP)

- **Active overlay:** `overlays/examples/multichannel/`
  - `prj_conf/prj_config_1dpu` — v++ link config (single source of truth for kernel clocks)
  - `sample_link.ini` — v++ advanced TCL only, no [clock] section
  - `image_processing.cfg` — color format list only
  - `create_imgproc_config.sh` — generates `image_processing.cfg`
  - `image_processing/src/` — C++ HLS sources
  - `firmware/` — last-build artefacts: `dpu.xclbin`, `kv260-multichannel.dtbo`, `kv260_multichannel.bit.bin`, `kv260_vcuDecode_vmixDP.hwh`, `shell.json`, `arch.json`
  - `binary_container_1/` — active v++ link dir (currently live build)
  - `rpu-test/` — doc mirror between board and host (this file lives here)
  - `scripts/` — TCL: `gen_dpu_xo.tcl`, `gen_sfm_xo.tcl`, `package_dpu_kernel.tcl`, `package_sfm_kernel.tcl`, `bip_proc.tcl`
  - `ml-models/` — 9 models including `densebox_640_360`, `refinedet_pruned_0_96`, `ssd_mobilenet_v2_coco_tf`
  - `test_videos/` — 12 MP4 files (~164 MB)

## Base platform (`kv260_vcuDecode_vmixDP`) — fixed facts

Vivado 2022.2, part `xck26 -2LV`, package `sfvc784`, board `kv260_som + SOM240_1`.

**IP address map (from `firmware/kv260_vcuDecode_vmixDP.hwh`):**
| Instance | Base | Notes |
|---|---|---|
| `display_pipeline_clk_wiz_0` | `0x80000000` | |
| `display_pipeline_v_tc_0`    | `0x80010000` | |
| `vcu_vcu_0`                  | `0x80100000` | 1 MB window |
| `axi_vip_0`                  | `0xA0000000` | |
| **`axi_gpio_0`**             | **`0xA0010000`** | 4-bit `led_4bits` output (phase-2 target) |
| `display_pipeline_v_mix_0`   | `0xB0000000` | |

**Clock taps (`clk_wiz_0`, 5 outputs), available to overlay accelerators:**
| Port | Freq |
|---|---|
| `clk_50M`  | 49.9995 MHz |
| `clk_100M` | 99.999 MHz |
| `clk_200M` | 199.998 MHz |
| `clk_275M` | 274.997 MHz |
| `clk_550M` | 549.994 MHz |

## Current overlay build state (as of 2026-04-19, probe 4)

**Prior build (16:32) discarded — used wrong xpfm.** Stock `xilinx_kv260_vcuDecode_vmixDP_202220_1` lacks `axi_gpio`, so DPU got placed at `0xA0010000` (GPIO's slot), stripping GPIO from the design. Phase-2 LED would have been impossible.

**Rebuild in progress** with correct local platform:
`platforms/kv260_vcuDecode_vmixDP/export/kv260_vcuDecode_vmixDP/kv260_vcuDecode_vmixDP.xpfm`

Local platform hwh confirms `axi_gpio_0 @ 0xA0010000 / 0xA001FFFF` with full IP set. v++ will relocate DPU (likely `0xA0020000`) and `image_processing` (likely `0xA0030000`) — new `dpu.xclbin.info` will show final addresses.

**Kernel clocks (`prj_conf/prj_config_1dpu`, unchanged):**
| Kernel | Port | Freq |
|---|---|---|
| DPU `DPUCZDX8G_1` | `aclk` | 275 MHz |
| DPU `DPUCZDX8G_1` | `ap_clk_2` (DSP) | 550 MHz |
| `image_processing_1` | `ap_clk` | 225 MHz (timing closed previously at this freq; HLS Fmax 376 MHz) |

**Firmware packaging — deferred** until rebuild completes. Steps when done:
1. bit.bin via `bootgen -arch zynqmp -image bitstream.bif -w` in `binary_container_1/sd_card/`
2. DTBO: xsct generates `dt/pl.dtsi` from new xsa; splice OpenAMP fragment from `rpu-test/kv260-multichannel-openamp.dtsi`; `dtc -@` to dtbo
3. Assemble `firmware/`: dpu.xclbin + bit.bin + dtbo + hwh + shell.json + arch.json

## Board side (context only — subagents don't ssh here, they run here)

- Kria KV260, PetaLinux 2022.2, kernel 5.15.36-xilinx-v2022.2
- User: `petalinux`, password `petalinux`
- Currently running overlay: `multichannel-openamp` (slot 0)
- OpenAMP rpmsg echo round-trip working (`/dev/rpmsg0`, RTT ≈ 1.5 ms)
- Zone-alert → RPU → GUI integration live
- Display: DP-1 enabled 1920x1080@60 on crtc 50 after `xmutil dp_bind`
- Key files on board: `/home/petalinux/{server.py, rpu_bridge.py, index.html, zoneguard_plugin.so, rpu-enablement.md}`

## Known issues / guardrails

- **Do NOT `xmutil unloadapp` while openamp overlay is loaded** — triggers `zynqmp_ipi_mailbox` UAF kernel oops. Reboot to switch away from openamp.
- **xmutil app-switch sequence:** `dp_unbind → unloadapp → loadapp → dp_bind`. After `dp_bind` modetest IDs change (`modetest -M xlnx` to re-read).
- **Dropbear ssh ignores** `BatchMode`, `StrictHostKeyChecking` options. Use `-y` for auto-accept new host keys.

## Update protocol

After each host-side probe, the main Claude instance rewrites this file's "Current overlay build state" + "Known issues" + timestamp, and scps it back to host. Subagents should cat this file at the start of every new task, then proceed.
