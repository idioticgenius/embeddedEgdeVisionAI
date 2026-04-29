# Firmware packaging — multichannel overlay

How to turn `v++` link outputs into a `firmware/` directory that
`xmutil loadapp kv260-multichannel` can load on the Kria KV260.

Target audience: future-self / subagents picking this up after a
rebuild. Written 2026-04-19 after the xpfm regression (stock platform
lacked `axi_gpio`, so the whole `firmware/` had to be rebuilt against
the local `kv260_vcuDecode_vmixDP` platform).

---

## Inputs — what v++ produces

All paths relative to
`overlays/examples/multichannel/binary_container_1/`.

| File | Location | Purpose |
|---|---|---|
| `dpu.xclbin` | `./` and `sd_card/` | DPU + image_processing kernel binary |
| `dpu.xsa` | `./` | Hardware handoff for DT generation |
| `sd_card/kv260_vcuDecode_vmixDP.hwh` | `sd_card/` | Final hwh — address map source of truth |
| `sd_card/kv260_vcuDecode_vmixDP_wrapper.bit` | `sd_card/` | Raw FPGA bitstream |
| `sd_card/bitstream.bif` | `sd_card/` | `bootgen` manifest |

Sanity check before packaging:
```sh
cd binary_container_1
./dpu.xclbin && xclbinutil --info --input dpu.xclbin | grep -E 'BASE|KERNEL|platform'
```
Expected: `DPUCZDX8G` and `image_processing` kernels, platform
`kv260_vcuDecode_vmixDP` (local, **not** `xilinx_kv260_...`), and no
address collision with `axi_gpio_0 @ 0xA0010000`.

---

## Outputs — what `firmware/` needs

Target directory on board:
`/lib/firmware/xilinx/kv260-multichannel/`

| File | Source | Notes |
|---|---|---|
| `dpu.xclbin` | copy from `binary_container_1/` | |
| `kv260_multichannel.bit.bin` | generated via `bootgen` (step 1) | bitstream for FPGA manager |
| `kv260-multichannel.dtbo` | xsct + dtc (steps 2–3) | device tree overlay |
| `kv260_vcuDecode_vmixDP.hwh` | copy from `sd_card/` | reference map, xlnx tools consult this |
| `shell.json` | reuse previous (static) | `xmutil` manifest |
| `arch.json` | copy from `binary_container_1/` | arch tag for runtime |

---

## Step 1 — bit.bin via bootgen

```sh
cd binary_container_1/sd_card

# bitstream.bif should already exist. If missing:
cat > bitstream.bif <<'EOF'
all:
{
  kv260_vcuDecode_vmixDP_wrapper.bit
}
EOF

bootgen -arch zynqmp -image bitstream.bif -w \
        -o kv260_multichannel.bit.bin
```

`-w` allows overwrite. Output is ~7.8 MB.

---

## Step 2 — generate pl.dtsi from dpu.xsa

Run `xsct` (from Vitis 2022.2). Needs `device-tree-xlnx` repo checked
out at the same branch as Vitis (`xlnx_rel_v2022.2`).

```sh
cd binary_container_1
mkdir -p dt

xsct <<'EOF'
hsi open_hw_design dpu.xsa
hsi set_repo_path /path/to/device-tree-xlnx
hsi create_sw_design -proc psu_cortexa53_0 -os device_tree dt
hsi generate_target -dir dt
hsi close_hw_design [hsi current_hw_design]
EOF
```

Produces:
- `dt/pl.dtsi` — auto-generated PL node tree (DPU, image_processing,
  axi_gpio, v_mix, v_tc, vcu, clk_wiz, axi_vip)
- `dt/pcw.dtsi`, `dt/system-top.dts`, `dt/zynqmp*.dtsi`, `dt/dt.mss`
  (not used for dtbo, only pl.dtsi matters for the overlay)

Verify `pl.dtsi` has every expected node:
```sh
grep -E '^\s+(DPUCZDX8G|image_processing|axi_gpio|v_mix|v_tc)' dt/pl.dtsi
```

---

## Step 3 — splice OpenAMP + compile to dtbo

The auto-generated `pl.dtsi` has no RPU/OpenAMP nodes — those live in
`rpu-test/kv260-multichannel-openamp.dtsi`. Two options:

### Option A — single overlay (recommended)

Concatenate the OpenAMP fragment into a fresh overlay header:
```sh
cd binary_container_1/dt

cat > kv260-multichannel.dtsi <<'EOF'
/dts-v1/;
/plugin/;

/ {
    compatible = "xlnx,zynqmp";
};

EOF

# Append PL nodes (strip the enclosing / { ... } wrapper)
sed -n '/amba_pl:/,/^};$/p' pl.dtsi >> kv260-multichannel.dtsi

# Append OpenAMP fragment (reserved-memory, rpmsg, remoteproc)
cat ../../rpu-test/kv260-multichannel-openamp.dtsi >> kv260-multichannel.dtsi

dtc -@ -I dts -O dtb -o kv260-multichannel.dtbo kv260-multichannel.dtsi
```

### Option B — two separate dtbos

Keep `pl.dtsi` as-is → `kv260-multichannel-base.dtbo`, and OpenAMP as
a second overlay. Only works if firmware manager supports stacked
overlays cleanly; simpler to bundle (Option A).

Verify the dtbo:
```sh
fdtdump kv260-multichannel.dtbo | grep -E 'DPUCZDX8G|image_processing|axi_gpio|rpmsg|reserved-memory'
```

---

## Step 4 — assemble firmware/

```sh
cd overlays/examples/multichannel
rm -rf firmware.new
mkdir firmware.new
cd firmware.new

cp ../binary_container_1/dpu.xclbin                                .
cp ../binary_container_1/sd_card/kv260_multichannel.bit.bin        .
cp ../binary_container_1/dt/kv260-multichannel.dtbo                .
cp ../binary_container_1/sd_card/kv260_vcuDecode_vmixDP.hwh        .
cp ../binary_container_1/arch.json                                 .
cp ../firmware/shell.json                                          .

# sanity
ls -la
```

Expected (6 files):
```
arch.json
dpu.xclbin
kv260-multichannel.dtbo
kv260_multichannel.bit.bin
kv260_vcuDecode_vmixDP.hwh
shell.json
```

Swap in atomically:
```sh
cd ..
mv firmware firmware.old && mv firmware.new firmware
```

---

## Step 5 — deploy to board

From host:
```sh
BOARD=petalinux@<board-ip>
scp firmware/* $BOARD:/tmp/fw/
ssh $BOARD 'sudo mkdir -p /lib/firmware/xilinx/kv260-multichannel && \
            sudo cp /tmp/fw/* /lib/firmware/xilinx/kv260-multichannel/'
```

On board:
```sh
# if another overlay is loaded, unbind DP first (never unloadapp openamp)
xmutil dp_unbind
xmutil unloadapp          # only if previous was NOT openamp
xmutil loadapp kv260-multichannel
xmutil dp_bind
modetest -M xlnx          # refresh crtc IDs
```

Check:
```sh
ls /dev/dri/card*
ls /dev/rpmsg0            # if OpenAMP fragment was spliced in
dmesg | tail -30
```

---

## Gotchas

1. **xpfm must be local `kv260_vcuDecode_vmixDP.xpfm`**, not the stock
   `xilinx_kv260_vcuDecode_vmixDP_202220_1`. Stock lacks `axi_gpio` and
   phase-2 LED becomes impossible. Check: `grep axi_gpio
   platforms/kv260_vcuDecode_vmixDP/hw/kv260_vcuDecode_vmixDP/kv260_vcuDecode_vmixDP.hwh`.

2. **Address map shifts** between builds. The old build had DPU at
   `0xA0010000` (colliding with axi_gpio). A correct build pushes DPU
   to `0xA0020000` and image_processing to `0xA0030000`. Update
   `broker-context.md` "IP address map" after each build.

3. **Do not `xmutil unloadapp`** while an openamp overlay is loaded —
   UAF in `zynqmp_ipi_mailbox`, kernel oops. Reboot to switch away.

4. **Dropbear on board** needs `-i ~/.ssh/id_ed25519` explicitly; default
   key filename is `id_dropbear`.

5. **Kernel clocks** are set in `prj_conf/prj_config_1dpu`, not in
   `sample_link.ini` or `image_processing.cfg`. Current settings:
   DPU aclk 275 MHz / ap_clk_2 550 MHz / image_processing 225 MHz.
   HLS Fmax for image_processing is 376 MHz → 67% margin.

---

## Checklist

- [ ] v++ link completed with local xpfm
- [ ] `dpu.xclbin.info` confirms expected platform + address map
- [ ] `bit.bin` generated from `sd_card/*.bit`
- [ ] `dt/pl.dtsi` generated from `dpu.xsa`
- [ ] `pl.dtsi` contains `axi_gpio_0` node (proves correct platform)
- [ ] OpenAMP fragment spliced (if RPU needed)
- [ ] `kv260-multichannel.dtbo` compiled clean
- [ ] `firmware/` has all 6 files
- [ ] board `/lib/firmware/xilinx/kv260-multichannel/` populated
- [ ] `xmutil loadapp kv260-multichannel` succeeds
- [ ] `/dev/dri/card*` present after `dp_bind`
- [ ] `/dev/rpmsg0` present (if OpenAMP enabled)
