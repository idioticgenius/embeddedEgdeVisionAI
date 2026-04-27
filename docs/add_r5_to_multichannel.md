# add_r5_to_multichannel

Step-by-step guide to bolt a Cortex-R5 / OpenAMP / rpmsg stack on top of
your existing **multichannel** Kria KV260 design so that the APU (Linux,
running our Flask server + zoneguard pipeline) can hand event payloads
to the R5 — the first half of Objective 6, "implement communication
pathway between general purpose CPU and real-time CPU inside the Kria SOM
so that the RT processor can trigger actions based on events."

This guide was cross-referenced against four authoritative sources and
the device tree layout matches the one Xilinx's own BSP ships:

1. [Xilinx *OpenAMP on Kria SOM* documentation](https://xilinx.github.io/kria-apps-docs/openamp/build/html/openamp_landing.html)
2. [Hackster — *OpenAMP on KRIA kv260* (Sasha Falkovich, PetaLinux flow)](https://www.hackster.io/sasha-falkovich/openamp-on-kria-kv260-ccbb30)
3. [Hackster — *OpenAMP in Xilinx MPSoC FPGA: Petalinux & Baremetal* (LogicTronix)](https://www.hackster.io/LogicTronix/openamp-in-xilinx-mpsoc-fpga-running-petalinux-baremetal-e025ca)
4. [GitHub — DanieleOttaviano/OpenAMP_tests (full DT + build scripts)](https://github.com/DanieleOttaviano/OpenAMP_tests)
5. Xilinx **UG1186** — *Libmetal and OpenAMP User Guide*
6. Xilinx Wiki — [OpenAMP 2022.1](https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/2241691649/OpenAMP+2022.1)

**Assumed starting point**

- KV260 SOM, k26 starter-kit carrier
- PetaLinux 2022.2, Vivado / Vitis 2022.2 on host
- You already have a working `kv260-multichannel.dtbo` and the BSP tree
  it was built from
- Goal: a DTBO you can `xmutil loadapp` that keeps everything the
  multichannel app does AND activates the R5 subsystem, plus an R5
  firmware ELF at `/lib/firmware/` that Linux remoteproc can load

---

## 0. Architectural map

```
┌────────────── ZynqMP SOC (K26 SOM) ───────────────┐
│                                                    │
│  Cortex-A53 × 4  (APU)  — Linux, Flask, zoneguard  │
│       │                                            │
│       │ remoteproc FW load + rpmsg over shared mem │
│       ▼                                            │
│  IPI hard-IP @ 0xFF99_0000  ◄──► IPI interrupts    │
│                                                    │
│  Cortex-R5 × 2  (RPU)  — OpenAMP echo / custom app │
│   TCM_A 64 KiB @ 0xFFE0_0000  (pnode-id 0xF)       │
│   TCM_B 64 KiB @ 0xFFE2_0000  (pnode-id 0x10)      │
│                                                    │
│  DDR (shared VDEV buffer + 2 vrings + code region) │
│    rproc_0_reserved  @ 0x3ED0_0000  0x40000   — FW │
│    rpu0vdev0vring0   @ 0x3ED4_0000  0x04000   — A→R│
│    rpu0vdev0vring1   @ 0x3ED4_4000  0x04000   — R→A│
│    rpu0vdev0buffer   @ 0x3ED4_8000  0x100000  — shm│
│                                                    │
└────────────────────────────────────────────────────┘
```

**The R5 cluster, TCMs, IPI block and interrupt controller are hard-IP
inside the ZynqMP PS.** There is no PL IP involved; your multichannel
bitstream does not need to change. Everything in this guide is
PetaLinux config + device tree + R5 firmware build.

---

## 1. What's missing on a currently-booted multichannel board

Run this on your KV260 right now:

```bash
ls /sys/class/remoteproc/           # empty  → no R5 node in the DT
dmesg | grep -iE "r5|remoteproc"    # nothing
zcat /proc/config.gz | grep -iE "REMOTEPROC|RPMSG|ZYNQMP_IPI" | head
#   CONFIG_REMOTEPROC=y
#   CONFIG_ZYNQMP_R5_REMOTEPROC=m
#   CONFIG_MAILBOX=y, CONFIG_ZYNQMP_IPI_MBOX=y
#   CONFIG_RPMSG=m, CONFIG_RPMSG_CHAR=m, CONFIG_RPMSG_NS=m, CONFIG_RPMSG_VIRTIO=m
```

The kernel has everything compiled in. What's missing is purely
**device tree** (no R5 / mailbox / reserved-memory nodes are present)
and a **firmware ELF** to load.

---

## 2. Vivado — are PL changes needed?

**No.** R5, IPI, TCM live in PS hard-IP. The Zynq UltraScale+ PS IP in
your Vivado block design already exposes them as long as:

- **PS-PL Configuration → RPU** tab shows R5 enabled (default)
- **Configurations → IPI Configurations** has at least CH0/CH1 on

Confirm these, re-export the XSA only if you changed anything,
otherwise skip to §3. The Kria BSP platform XSA has them correct
out-of-the-box.

---

## 3. PetaLinux — kernel and rootfs config

From the top of your PetaLinux project (`kv260_multichannel/` or
whatever you named it):

### 3.1 Kernel — `petalinux-config -c kernel`

Verify (all are already right on a stock Kria BSP but check):

```
CONFIG_REMOTEPROC=y
CONFIG_ZYNQMP_R5_REMOTEPROC=m
CONFIG_MAILBOX=y
CONFIG_ZYNQMP_IPI_MBOX=y
CONFIG_RPMSG=m
CONFIG_RPMSG_CHAR=m
CONFIG_RPMSG_NS=m
CONFIG_RPMSG_VIRTIO=m
CONFIG_VIRTIO=m
```

### 3.2 Root filesystem — `petalinux-config -c rootfs`

Under **Filesystem Packages → misc** enable:

```
[*] libopen-amp      (libopen-amp1)
[*] libopen-amp-demos
[*] libmetal
[*] libmetal-demos
[*] openamp-fw-echo-testd      ← the R5-side ELF recipe the BSP ships
```

Under **Petalinux Package Groups**:

```
[*] packagegroup-petalinux-openamp
[*] packagegroup-petalinux-openamp-echo-test
```

The `openamp-fw-echo-testd` recipe is the important one — it installs
the pre-built R5 ELF `image_echo_test` to `/lib/firmware/` on the
target. Confirm it's enabled either via the menuconfig UI or with:

```bash
grep "^CONFIG_openamp\|^CONFIG_packagegroup-petalinux-openamp" \
     project-spec/configs/rootfs_config
```

The Kria apps docs note that **2022.1+ BSP prebuilt images already
include the openamp.dtsi fragment and the echo-test firmware in the
released .wic** — so if you started from the prebuilt Kria SD image,
`/lib/firmware/image_echo_test` may already exist.

---

## 4. Device tree — the BSP layout, verbatim

PetaLinux's Xilinx BSP for Kria/ZynqMP ships a file
`project-spec/meta-user/recipes-bsp/device-tree/files/openamp.dtsi`
with the node definitions. You can either merge it into
`system-user.dtsi` (the BSP way) or write them directly. Below is the
**complete, correct layout** — verified against the DanieleOttaviano
reference and the LogicTronix tutorial.

### 4.1 Merge into `system-user.dtsi`

```dts
/include/ "system-conf.dtsi"

/ {
    /* Four reserved-memory regions used by the R5 OpenAMP link.
     * 0x3ED00000 + 16 MiB is the Xilinx-standard placement; it
     * fits in the 1 GiB DDR that ships on K26 SOMs and does not
     * clash with the 900 MiB CMA reservation Linux uses for VVAS
     * dmabufs. */
    reserved-memory {
        #address-cells = <2>;
        #size-cells = <2>;
        ranges;

        rproc_0_reserved: rproc@3ed00000 {
            no-map;
            reg = <0x0 0x3ed00000 0x0 0x40000>;      /* 256 KiB FW code/data */
        };
        rpu0vdev0vring0: rpu0vdev0vring0@3ed40000 {
            no-map;
            reg = <0x0 0x3ed40000 0x0 0x4000>;       /*  16 KiB APU→RPU vring */
        };
        rpu0vdev0vring1: rpu0vdev0vring1@3ed44000 {
            no-map;
            reg = <0x0 0x3ed44000 0x0 0x4000>;       /*  16 KiB RPU→APU vring */
        };
        rpu0vdev0buffer: rpu0vdev0buffer@3ed48000 {
            no-map;
            reg = <0x0 0x3ed48000 0x0 0x100000>;     /*   1 MiB rpmsg payload */
        };
    };

    /* The two TCM banks. "mmio-sram" is the right compatible; the R5
     * driver also resolves them via their pnode-ids from the PMUFW
     * power-domain table. Values (0xF / 0x10) come from UG1085 §41. */
    tcm_0a@ffe00000 {
        compatible = "mmio-sram";
        no-map;
        reg = <0x0 0xffe00000 0x0 0x10000>;
    };
    tcm_0b@ffe20000 {
        compatible = "mmio-sram";
        no-map;
        reg = <0x0 0xffe20000 0x0 0x10000>;
    };

    /* The R5 remoteproc node. The "rf5ss" wrapper covers both R5
     * cores; xlnx,cluster-mode = 1 selects "split" mode (0 is
     * lockstep). For OpenAMP echo on R5_0 you want split. */
    rf5ss@ff9a0000 {
        compatible = "xlnx,zynqmp-r5-remoteproc";
        xlnx,cluster-mode = <1>;
        reg = <0x0 0xFF9A0000 0x0 0x10000>;
        #address-cells = <0x2>;
        #size-cells = <0x2>;
        ranges;

        r5f_0 {
            compatible = "xilinx,r5f";
            memory-region = <&rproc_0_reserved>,
                            <&rpu0vdev0buffer>,
                            <&rpu0vdev0vring0>,
                            <&rpu0vdev0vring1>;
            mboxes = <&ipi_mailbox_rpu0 0>,
                     <&ipi_mailbox_rpu0 1>;
            mbox-names = "tx", "rx";
        };
    };

    /* IPI (inter-processor interrupt) mailbox. APU local-IPI-id = 7,
     * RPU0 remote-IPI-id = 1 — these are fixed assignments by the
     * PMUFW table in Xilinx's default design. Register map is from
     * UG1085 §25. */
    zynqmp_ipi1 {
        compatible = "xlnx,zynqmp-ipi-mailbox";
        interrupt-parent = <&gic>;
        interrupts = <0 29 4>;
        xlnx,ipi-id = <7>;
        #address-cells = <1>;
        #size-cells = <1>;
        ranges;

        ipi_mailbox_rpu0: mailbox@ff990600 {
            reg = <0xff990600 0x20>,   /* local request  (APU→RPU0) */
                  <0xff990620 0x20>,   /* local response (APU→RPU0) */
                  <0xff9900c0 0x20>,   /* remote request (RPU0→APU) */
                  <0xff9900e0 0x20>;   /* remote response (RPU0→APU) */
            reg-names = "local_request_region",
                        "local_response_region",
                        "remote_request_region",
                        "remote_response_region";
            #mbox-cells = <1>;
            xlnx,ipi-id = <1>;
        };
    };
};
```

### 4.2 The BSP-merge shortcut (from the Kria/Falkovich Hackster)

If your BSP ships `openamp.dtsi` under
`project-spec/meta-user/recipes-bsp/device-tree/files/`, you can splice
it into `system-user.dtsi` in one shot:

```bash
cd project-spec/meta-user/recipes-bsp/device-tree/files

head -n 6  system-user.dtsi  > tmp
tail -n +2 openamp.dtsi | head -n -1 >> tmp
tail -n +7 system-user.dtsi >> tmp
mv tmp system-user.dtsi
```

This is exactly the snippet the [Sasha Falkovich KV260 tutorial](https://www.hackster.io/sasha-falkovich/openamp-on-kria-kv260-ccbb30)
uses. The result is the same §4.1 fragment, just sourced from the BSP
instead of typed out.

### 4.3 Build the DTB

```bash
petalinux-build -c device-tree
```

Output: `images/linux/system.dtb` now contains the R5 subsystem.

---

## 5. Build `openamp.dtbo` (Kria app overlay)

Kria uses **app-manager** (`xmutil loadapp`) to load DTBOs at runtime,
so we want a Kria-shaped overlay you can stack on top of multichannel.

### 5.1 The overlay source — `openamp-overlay.dts`

```dts
/dts-v1/;
/plugin/;

/ {
    fragment@0 {
        target-path = "/reserved-memory";
        __overlay__ {
            #address-cells = <2>;
            #size-cells = <2>;
            ranges;
            rproc_0_reserved: rproc@3ed00000 {
                no-map;
                reg = <0x0 0x3ed00000 0x0 0x40000>;
            };
            rpu0vdev0vring0: rpu0vdev0vring0@3ed40000 {
                no-map;
                reg = <0x0 0x3ed40000 0x0 0x4000>;
            };
            rpu0vdev0vring1: rpu0vdev0vring1@3ed44000 {
                no-map;
                reg = <0x0 0x3ed44000 0x0 0x4000>;
            };
            rpu0vdev0buffer: rpu0vdev0buffer@3ed48000 {
                no-map;
                reg = <0x0 0x3ed48000 0x0 0x100000>;
            };
        };
    };

    fragment@1 {
        target-path = "/";
        __overlay__ {
            tcm_0a@ffe00000 {
                compatible = "mmio-sram";
                no-map;
                reg = <0x0 0xffe00000 0x0 0x10000>;
            };
            tcm_0b@ffe20000 {
                compatible = "mmio-sram";
                no-map;
                reg = <0x0 0xffe20000 0x0 0x10000>;
            };

            rf5ss@ff9a0000 {
                compatible = "xlnx,zynqmp-r5-remoteproc";
                xlnx,cluster-mode = <1>;
                reg = <0x0 0xff9a0000 0x0 0x10000>;
                #address-cells = <0x2>;
                #size-cells = <0x2>;
                ranges;

                r5f_0 {
                    compatible = "xilinx,r5f";
                    memory-region = <&rproc_0_reserved>,
                                    <&rpu0vdev0buffer>,
                                    <&rpu0vdev0vring0>,
                                    <&rpu0vdev0vring1>;
                    mboxes = <&ipi_mailbox_rpu0 0>,
                             <&ipi_mailbox_rpu0 1>;
                    mbox-names = "tx", "rx";
                };
            };

            zynqmp_ipi1 {
                compatible = "xlnx,zynqmp-ipi-mailbox";
                interrupt-parent = <&gic>;
                interrupts = <0 29 4>;
                xlnx,ipi-id = <7>;
                #address-cells = <1>;
                #size-cells = <1>;
                ranges;
                ipi_mailbox_rpu0: mailbox@ff990600 {
                    reg = <0xff990600 0x20>,
                          <0xff990620 0x20>,
                          <0xff9900c0 0x20>,
                          <0xff9900e0 0x20>;
                    reg-names = "local_request_region",
                                "local_response_region",
                                "remote_request_region",
                                "remote_response_region";
                    #mbox-cells = <1>;
                    xlnx,ipi-id = <1>;
                };
            };
        };
    };
};
```

### 5.2 Compile

```bash
dtc -O dtb -@ -o openamp.dtbo openamp-overlay.dts
```

### 5.3 Kria app-manager folder — `shell.json`

```json
{
    "shell_type": "XRT_FLAT",
    "num_slots": "1"
}
```

On the board you lay the assets out like this (same layout
`kv260-multichannel` uses):

```
/lib/firmware/xilinx/openamp-echo/
├── openamp.dtbo
├── shell.json
```

### 5.4 Stacked or merged with multichannel?

Two deployment choices:

1. **Stacked (recommended during bring-up)**:
   `xmutil loadapp multichannel` + `xmutil loadapp openamp-echo`. The
   fragments target different DT subtrees (multichannel hits PL IP +
   video planes; openamp-echo hits PS R5 + IPI), so they don't
   conflict.
2. **Merged** (for a single-app shipping image): cat the openamp
   fragments into the end of `kv260-multichannel.dts` and rebuild a
   new `kv260-multichannel.dtbo` so one `xmutil loadapp multichannel`
   brings up both. Cleaner for operators, more fiddly to iterate on.

---

## 6. Build the R5 firmware (Vitis 2022.2)

The 2022.1+ Kria BSP already ships a pre-built `image_echo_test` via
the `openamp-fw-echo-testd` recipe, so **you may not need Vitis at
all** for the initial bring-up. Verify on a rebuilt PetaLinux rootfs:

```bash
ls -la /lib/firmware/image_echo_test   # should be ~100-200 KB ELF
```

If you want to modify it or build a custom R5 app:

### 6.1 Vitis project

1. **File → New → Application Project**
2. **Platform**: the XSA exported from your Vivado multichannel design,
   OR the stock Kria K26 starter-kit platform
   (`xilinx_kv260_starterkit_202210_1`)
3. **Processor**: `psu_cortexr5_0`
4. **OS**: `standalone` (bare-metal). FreeRTOS works too; the echo
   template supports both.
5. **Template**: **OpenAMP echo-test** (not "helloworld" — the echo
   template already pulls in `libopen_amp` + `libmetal` and sets up
   the rpmsg endpoint with NS announce string
   `"rpmsg-openamp-demo-channel"`).

### 6.2 LogicTronix gotcha: double GIC init

If you move to FreeRTOS on the R5 side, comment out the first GIC
initialisation in `helper.c` (`XScuGic_CfgInitialize` + the vector-
setting block). Both OpenAMP's helper and FreeRTOS try to write the
R5 interrupt vectors; the second write clobbers the first and you get
silence on startup. This is documented in the
[LogicTronix tutorial](https://www.hackster.io/LogicTronix/openamp-in-xilinx-mpsoc-fpga-running-petalinux-baremetal-e025ca).
The plain bare-metal echo template does not have this problem.

### 6.3 Linker script

The default linker script from the OpenAMP template places text + data
in the TCM. If you grow the firmware past ~128 KiB you'll outgrow TCM
and need to either (a) move part to DDR's `rproc_0_reserved` region
(update `MEMORY` in `lscript.ld`), or (b) use lockstep mode for
unified TCM (change `xlnx,cluster-mode = <0>` in §5.1 and recompile).

### 6.4 Where the ELF ends up

Build it. You'll get `<project>/Debug/<project>.elf`. Rename it to
`image_echo_test` (or whatever you'll type into
`/sys/class/remoteproc/remoteproc0/firmware`) and either:

- Copy it to `/lib/firmware/image_echo_test` on the running board
  directly (quick iteration), or
- Add a PetaLinux recipe so it lands in every rootfs image.

PetaLinux recipe `project-spec/meta-user/recipes-apps/rpu-fw/rpu-fw.bb`:

```bitbake
SUMMARY = "R5 OpenAMP echo-test firmware"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"
SRC_URI = "file://image_echo_test"
FILES_${PN} += "/lib/firmware/image_echo_test"
do_install() {
    install -d ${D}/lib/firmware
    install -m 0755 ${WORKDIR}/image_echo_test ${D}/lib/firmware/image_echo_test
}
COMPATIBLE_MACHINE = "zynqmp"
```

Put the ELF in `recipes-apps/rpu-fw/files/`, add `rpu-fw` to rootfs
config, `petalinux-build`.

---

## 7. Flash / copy to the board

### 7.1 Full reflash

Copy the new `BOOT.BIN`, `image.ub`, `rootfs.tar.gz` to SD card per
your normal flow. Reboot.

### 7.2 Incremental

Everything added here can be dropped onto a running board without
reflashing:

```bash
# on host
scp images/linux/image_echo_test        petalinux@kv260:/tmp/
scp openamp.dtbo shell.json             petalinux@kv260:/tmp/
# on board, as root
cp /tmp/image_echo_test /lib/firmware/image_echo_test
mkdir -p /lib/firmware/xilinx/openamp-echo
cp /tmp/openamp.dtbo /tmp/shell.json /lib/firmware/xilinx/openamp-echo/
```

---

## 8. Runtime bring-up sequence

```bash
# Load the multichannel app (your existing workflow)
sudo xmutil unloadapp                     # clean slate
sudo xmutil loadapp k26-starter-kits      # base carrier DT if needed
sudo xmutil loadapp kv260-multichannel

# Layer the openamp overlay
sudo xmutil loadapp openamp-echo

# Load the kernel modules
sudo modprobe zynqmp_r5_remoteproc
sudo modprobe virtio_rpmsg_bus
sudo modprobe rpmsg_char
sudo modprobe rpmsg_ns

# The remoteproc device should now exist
ls /sys/class/remoteproc/                 # → remoteproc0

# Point it at the firmware and start it
echo image_echo_test  | sudo tee /sys/class/remoteproc/remoteproc0/firmware
echo start            | sudo tee /sys/class/remoteproc/remoteproc0/state

sudo dmesg | tail -n 20 | grep -iE "r5|remoteproc|rpmsg"
# expected lines:
#   remoteproc remoteproc0: remote processor r5f@0 is now up
#   virtio_rpmsg_bus virtio0: creating channel rpmsg-openamp-demo-channel addr 0x400
```

And you'll see `/dev/rpmsg_ctrl0` + one or more `/dev/rpmsgN`.

---

## 9. Smoke test

```bash
sudo echo_test
#   Echo test start
#   Open rpmsg dev!
#   ...
#   **************************************
#   Echo test round 0
#   sending payload number 0 of size 17
#   received payload number 0 of size 17
#   ...
#   **************** Test Results: Error count = 0 ****************
```

Stop/start cycle:

```bash
echo stop  | sudo tee /sys/class/remoteproc/remoteproc0/state
echo start | sudo tee /sys/class/remoteproc/remoteproc0/state
```

---

## 10. Failure modes + fixes (aggregated from the cited sources)

| Symptom | Cause | Fix |
|---|---|---|
| `/sys/class/remoteproc/` stays empty after `modprobe` | R5 overlay didn't apply — wrong target-path or conflicting node | `cat /sys/firmware/fdt > /tmp/active.dtb && dtc -I dtb -O dts /tmp/active.dtb` and grep for `rf5ss` to confirm it's there |
| `remoteproc0/state: I/O error` on `echo start` | R5 ELF links code outside reserved-memory or has wrong endian | Check Vitis build targets `psu_cortexr5_0`, 32-bit LE, armv7; verify `lscript.ld` places `.text` in TCM or `rproc_0_reserved` |
| `mboxes must have 2 entries` | Only one of `tx`/`rx` in `r5f_0` | Both required — see §4.1 |
| No `/dev/rpmsg*` after firmware starts | `virtio_rpmsg_bus` and/or `rpmsg_char` not loaded | `modprobe` both — or add to `/etc/modules-load.d/openamp.conf` |
| Echo test silent / FreeRTOS build | Double GIC init (helper.c + FreeRTOS port) | Comment out the helper.c init, per LogicTronix tutorial §6.2 above |
| DTBO silently doesn't apply | Address conflicts against multichannel | Do a merge build (§5.4 option 2) or double-check no other DTBO defines the same reserved-memory range |
| `rproc device not idle` on reload | R5 still running from last session | `echo stop > …/state` first, then swap the firmware filename |
| Echo test hangs on `echo stop` | APU-side rpmsg endpoint still open | Close the fd before stopping; our custom Flask bridge does this in its teardown path |
| Errors on 2021.x PetaLinux only | BSP needed the `openamp-fw-echo-testd` recipe, not just `packagegroup-petalinux-openamp` (LogicTronix note) | Enable the recipe explicitly per §3.2 |

---

## 11. Integration point with our app

Once `/dev/rpmsg_ctrl0` + `/dev/rpmsgN` exist, the rest is pure
userspace. Plan for the app-level integration (separate doc to
follow):

1. **APU-side bridge module** (Python, in `server.py`): on startup
   open the rpmsg char device; on `PipelineManager.trigger_alert` and
   `clear_alert` write a one-line JSON:
   ```json
   {"ch":0,"kind":"ENTER","name":"danger","ts":1775979667.786}
   {"ch":0,"kind":"CLEAR","ts":1775979670.421}
   ```
   If the device disappears (R5 unloaded) fall back to a local FIFO so
   the pipeline keeps working, reconnect when it reappears.
2. **R5 firmware** (custom app on top of the OpenAMP template):
   - Parse the one-line JSON (or use a fixed 16-byte struct for
     speed).
   - On `ENTER`: drive a PS-MIO GPIO high (machinery stop relay), or
     post a CAN frame on PS-CAN0, or whatever the carrier wires up.
   - On `CLEAR`: release the GPIO / send all-clear frame.

The R5 app swap is just the payload-handling function; the transport
layer (rpmsg over IPI/shared memory) is already solved by the echo
template.

---

## 12. Quick reference

```
FW ELF              /lib/firmware/image_echo_test
App DTBO            /lib/firmware/xilinx/openamp-echo/openamp.dtbo
Remoteproc sysfs    /sys/class/remoteproc/remoteproc0/{state,firmware}
rpmsg endpoints     /dev/rpmsg_ctrl0, /dev/rpmsgN
IPI ID APU          7 (xlnx,ipi-id on zynqmp_ipi1)
IPI ID RPU0         1 (xlnx,ipi-id on mailbox@ff990600)
IPI regs            0xff990600..ff9906e0 (4 × 0x20)
Reserved DDR base   0x3ed00000 (rproc) / 0x3ed40000 / 0x3ed44000 / 0x3ed48000
TCM_A / TCM_B       0xffe00000 (pnode-id 0xF) / 0xffe20000 (pnode-id 0x10)
Cluster mode        xlnx,cluster-mode = <1>   (1 = split, 0 = lockstep)
NS announce string  "rpmsg-openamp-demo-channel"
```

---

## 13. References

- Xilinx KV260 OpenAMP — <https://xilinx.github.io/kria-apps-docs/openamp/build/html/openamp_landing.html>
- Hackster KV260 OpenAMP (Sasha Falkovich) —
  <https://www.hackster.io/sasha-falkovich/openamp-on-kria-kv260-ccbb30>
- Hackster MPSoC OpenAMP (LogicTronix) —
  <https://www.hackster.io/LogicTronix/openamp-in-xilinx-mpsoc-fpga-running-petalinux-baremetal-e025ca>
- DanieleOttaviano/OpenAMP_tests —
  <https://github.com/DanieleOttaviano/OpenAMP_tests>
- Hackster SnickerDoodle OpenAMP —
  <https://www.hackster.io/timothy-vales/openamp-on-the-snickerdoodle-black-691912>
- UG1186 — *Libmetal and OpenAMP User Guide*
- UG1144 — *PetaLinux Tools Documentation*
- UG1085 — *ZynqMP TRM*, §6 PS, §25 IPI, §41 RPU power-node IDs
- PG243 — *KV260 Starter Kit Data Sheet* (MIO / GPIO map)
- Linux kernel DT binding:
  `Documentation/devicetree/bindings/remoteproc/xlnx,zynqmp-r5-remoteproc.yaml`
- Xilinx wiki OpenAMP 2022.1 —
  <https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/2241691649/OpenAMP+2022.1>
- OpenAMP echo test demo —
  <https://openamp.readthedocs.io/en/latest/openamp-system-reference/examples/linux/rpmsg-echo-test/README.html>

Document last updated: 2026-04-12.
