# Device_Tree_Overlays

The complete production firmware bundle for the **multichannel-openamp-gpio**
overlay — the main firmware of the project. Every file here is a byte-for-byte
copy of `latest-project-files/multichannel-openamp-gpio/`, which is the
canonical firmware shipped to `/lib/firmware/xilinx/multichannel-openamp-gpio/`
on the running KV260 board.

This is what `xmutil loadapp multichannel-openamp-gpio` consumes.

## File inventory

| File | Size | Role |
|---|---:|---|
| `kv260-multichannel-openamp-gpio.dtsi`     |  11,955 B | Production device-tree source. Combines the multichannel pipeline IPs, the OpenAMP fragment (reserved memory carve-outs, IPI mailbox, R5 remoteproc, TCM nodes) and the AXI GPIO core at `0xA0010000`. |
| `kv260-multichannel-gpio.dtbo`              |  10,477 B | Compiled production overlay loaded by remoteproc and applied at runtime through `xmutil loadapp`. |
| `kv260-multichannel-gpio.dtbo.bak`          |  10,788 B | Previous-revision DTBO, kept as a rollback point. |
| `dpu.xclbin`                                |  7.5 MiB  | The packaged FPGA image. Contains the bitstream, the DPU compute-unit description and the image-processing kernel description. Loaded by XRT at run time. |
| `kv260_vcuDecode_vmixDP_wrapper.bit.bin`    |  7.4 MiB  | The raw FPGA bitstream in `.bit.bin` form (kernel-loadable). Produced by Vivado place-and-route + bootgen. |
| `shell.json`                                |     40 B  | xmutil shell manifest. Tells `xmutil loadapp` which DTBO and which xclbin make up the application. |
| `arch.json`                                 |     36 B  | DPU architecture descriptor consumed by the Vitis AI compiler `vai_c_caffe -a`. |
| `image_processing.cfg`                      |     88 B  | v++ link configuration (clock frequencies, port mappings) for the image-processing kernel. |
| `bitstream.bif`                             |     74 B  | BIF manifest used by `bootgen` to convert the bitstream to `.bit.bin`. |

## md5 verification

Every file matches the canonical board copy byte-for-byte:

```
kv260-multichannel-openamp-gpio.dtsi      7c012d39a521eaef2cf81a9c72c5138a
kv260-multichannel-gpio.dtbo              81b4c78cb73100e97ebc325a1237f414
shell.json                                95bfd10880d6091b8fcfe65c2835441c
arch.json                                 89906f19f73b085913d5e8fd5dc3e878
image_processing.cfg                      846f256f36889b8958ad6d188f4714b5
bitstream.bif                             05cb853cdb2da3a9cfbfa10f7cd93f9c
dpu.xclbin                                f944dd8426bba13c166460471d1a161f
kv260_vcuDecode_vmixDP_wrapper.bit.bin    baaa6c926c76f93d423ae783da69eae3
```

## Memory carve-outs (from `kv260-multichannel-openamp-gpio.dtsi`)

| Region | Address | Size | Used by |
|---|---|---:|---|
| `rproc_0_reserved`   | `0x3ED00000` | 256 KiB | R5 firmware code + data |
| `rpu0vdev0vring0`     | `0x3ED40000` |  16 KiB | rpmsg ring 0 (APU→R5) |
| `rpu0vdev0vring1`     | `0x3ED44000` |  16 KiB | rpmsg ring 1 (R5→APU) |
| `rpu0vdev0buffer`     | `0x3ED48000` |   1 MiB | rpmsg payload buffers |
| `tcm_0a`              | `0xFFE00000` |  64 KiB | R5 instruction TCM |
| `tcm_0b` (shm_alert)  | `0xFFE20000` |  64 KiB | R5 data TCM + APU↔R5 fast-path mailbox |
| AXI GPIO core         | `0xA0010000` |  64 KiB | PMOD J2 LED drive (4 outputs) |

## Deploy to the board

The whole bundle goes to `/lib/firmware/xilinx/multichannel-openamp-gpio/`:

```sh
scp -r 2_Implementation/Source_Code/Device_Tree_Overlays/* \
       petalinux@192.168.1.78:/tmp/multichannel-openamp-gpio/
ssh petalinux@192.168.1.78 << 'EOSSH'
sudo mkdir -p /lib/firmware/xilinx/multichannel-openamp-gpio
sudo cp -r /tmp/multichannel-openamp-gpio/* \
           /lib/firmware/xilinx/multichannel-openamp-gpio/
EOSSH
```

## Load on the board

```sh
sudo xmutil loadapp multichannel-openamp-gpio   # reads shell.json → loads DTBO + xclbin
sudo xmutil listapps                             # confirm Active state
```

Expect dmesg to show:
- `fpga_manager fpga0: writing dpu.xclbin to Xilinx Zynq UltraScale+ MPSoC PL`
- `remoteproc remoteproc0: ff9a0000.rf5ss is available`

After loading, write `start` to remoteproc0/state to bring up the R5
mirror firmware:

```sh
echo start | sudo tee /sys/class/remoteproc/remoteproc0/state
```

## Unload (with caution)

The `xmutil unloadapp` path is currently **broken** by a kernel-side
zynqmp_ipi_mailbox use-after-free defect. Unloading the overlay while
the multichannel-openamp-gpio is active triggers a kernel oops. The
workaround is to reboot the board for any application change. The
defect is documented in
`master-draft/references/architecture-docs/rpu-enablement.md`.

```sh
# DO NOT use xmutil unloadapp on this overlay until the kernel patch is upstream
sudo xmutil unloadapp multichannel-openamp-gpio   # ⚠ triggers kernel UAF
```

## Recompile from source

```sh
dtc -O dtb -o kv260-multichannel-gpio.dtbo \
       -i /path/to/kernel-headers \
       kv260-multichannel-openamp-gpio.dtsi
```

In practice the DTBO is built by the v++ link step in
`../Vitis_Platform/multichannel_overlay/Makefile` and packaged together
with the xclbin into the firmware bundle that lives in this folder.

## Cross-references

- DTSI source walkthrough: `master-draft/build/chapters/06_implementation.md` §6.3
- Memory layout rationale (TCM vs DDR vs OCM): `master-draft/references/architecture-docs/shm_refactor_architecture.md`
- xmutil unloadapp UAF: `master-draft/references/architecture-docs/rpu-enablement.md`
- The R5 firmware that the overlay enables: `../C_R5_Firmware/`
- The AXI GPIO that the overlay exposes: `../C_R5_Firmware/rpmsg-echo.c` (lines 36–75)
