# Build_Makefiles

Top-level build wrappers and boot-image manifests.

| File | Role |
|---|---|
| `Makefile_platforms`       | Top-level Makefile from `kria-vitis-platforms/kv260/platforms/`. Drives the Vivado, Vitis and PetaLinux builds in sequence. |
| `Makefile_rpu-test`        | Makefile from `rpu-test/`. Wraps the deployment of overlays, xclbin, R5 firmware and Flask server to the board. |
| `linux.bif`                | Boot Image Format manifest used by `bootgen` to assemble `BOOT.BIN`. Defines the boot loader, the FPGA bitstream and the kernel image. |
| `bitstream.bif`            | BIF manifest specifically for the FPGA bitstream conversion to `.bit.bin`. |
| `board_bringup.sh`         | One-shot board-bring-up script. Loads overlay, starts R5, brings up DisplayPort, launches server. |
| `deploy_openamp.sh`        | Deploy script that scp's the R5 firmware to the board and reloads remoteproc. |

## Top-level build sequence

```sh
# 1. Build the Vivado hardware platform
cd ../Vivado_Hardware/kv260_vcuDecode_vmixDP
make

# 2. Build the Vitis platform (xpfm)
cd ../../Vitis_Platform
make -f Makefile_platforms

# 3. Build the multichannel xclbin
cd multichannel_overlay
make

# 4. Build the PetaLinux image
cd ../../PetaLinux_BSP
petalinux-build
petalinux-package --boot --u-boot --force

# 5. Flash and deploy
sudo dd if=build/.../petalinux-sdimage.wic of=/dev/sdX bs=4M
# (or scp deltas to a running board through deploy_openamp.sh)
```
