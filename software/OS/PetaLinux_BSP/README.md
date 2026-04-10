# PetaLinux_BSP

The PetaLinux 2022.2 project tree, minus the build/ directory. Contains
the project-spec configurations, the kernel-config fragments, the device-tree
sources, the meta-user recipes, and selected components.

```
PetaLinux_BSP/
├── README, README.hw, config.project       # project metadata
├── project-spec/
│   ├── attributes/                         # PetaLinux attributes file
│   ├── configs/                            # config, rootfs_config, bsp.cfg, plnxtool.conf
│   ├── decoupling-dtsi/                    # decoupled DTSI files for build
│   ├── dts_dir/                            # custom DTS source
│   ├── hw-description/                     # the .xsa hardware handoff
│   └── meta-user/                          # custom Yocto recipes
│       └── recipes-*/                      # per-component .bb / .bbappend recipes
├── components/                             # selected Yocto layers (subset of upstream)
└── pre-built/                              # boot image manifests (.its, .dts, .cfg)
```

## Build the rootfs and boot image

```sh
cd PetaLinux_BSP
petalinux-build           # ~35 min on first build, <5 min incremental
petalinux-package --boot --u-boot \
    --fpga ../Vitis_Platform/kv260_vcuDecode_vmixDP/.../*.bit \
    --force
```

Outputs (under the build/ directory, not committed here):
- `images/linux/BOOT.BIN`
- `images/linux/image.ub`
- `images/linux/boot.scr`
- `images/linux/petalinux-sdimage.wic`

## Flash to SD card

```sh
sudo dd if=images/linux/petalinux-sdimage.wic of=/dev/sdX bs=4M conv=fsync
```

(Or use Balena Etcher.)

## Adding the missing recipes

The 22 packages required by the multichannel-openamp-gpio firmware
(Vitis AI Library, VART, VVAS, OpenAMP demo packagegroups, libvcu/libomxil,
xlnx-platformstats, xmutil, kria-dashboard, python3-pyserial,
fpga-manager-util-xilinx, etc.) have been added through the canonical
PetaLinux 2022.2 procedure documented in **AMD UG1144 §"Appending Root
File System Packages"**.

### Canonical procedure (per UG1144)

The 2020.1 release onwards mandates this three-step flow:

1. **Edit `project-spec/meta-user/conf/user-rootfsconfig`** — add one
   `CONFIG_<packagename>` line per package, **without `=y`**. This
   registers the entries in the rootfs Kconfig menu under "user
   packages".
2. **Run `petalinux-config -c rootfs`** — open the menu, navigate to
   "user packages", toggle each registered entry on, and save. The
   Kconfig system writes the corresponding `CONFIG_<name>=y` lines into
   `project-spec/configs/rootfs_config`.
3. **Run `petalinux-build`** — Yocto resolves the recipes from the
   layers listed in `components/yocto/conf/bblayers.conf` and bundles
   the packages into the rootfs.

This BSP has been pre-configured for steps 1 and 2: the 22 packages are
already listed in `user-rootfsconfig` and the corresponding
`CONFIG_*=y` lines are already present at the end of `rootfs_config`.
Running `petalinux-config -c rootfs` once and saving (without changing
anything) will refresh the menu and confirm the configuration. Step 3
is the only command that needs to be invoked from a clean checkout.

### Files modified for this configuration

| File | What it provides |
|---|---|
| `project-spec/meta-user/conf/user-rootfsconfig` | `CONFIG_<pkg>` registrations under "user packages" — the canonical 2020.1+ entry point. |
| `project-spec/configs/rootfs_config` | `CONFIG_<pkg>=y` selections (auto-written by `petalinux-config -c rootfs` after `user-rootfsconfig` is updated). |
| `project-spec/meta-user/conf/petalinuxbsp.conf` | `IMAGE_INSTALL:append` lines as a defensive fallback for the case where a package is provided by a layer that does not yet expose a Kconfig entry. Per UG1144, this is the legacy method (pre-2020.1); in modern flows it is supplementary, not primary. |
| `project-spec/meta-user/conf/bblayers.conf.append` | Documentation of which Yocto layer provides each recipe. The audit confirms that all required layers (`meta-vitis`, `meta-xilinx/meta-xilinx-core`, `meta-som`, `meta-petalinux`, `meta-openamp`, `meta-openembedded/meta-python`) are already present in the upstream `bblayers.conf`. No layer additions are needed. |

### Run-time fallback (dnf)

For a board that has already been flashed without these packages in the
image, `Shell_Scripts/install-firmware-packages.sh` is provided. It uses
`dnf install` against the PetaLinux feed to add all 22 packages at run
time. This script is the recommended path for upgrading an existing
image without reflashing.

## Build the rootfs and boot image

```sh
cd PetaLinux_BSP

# 1. Apply the bblayers.conf.append manually
$EDITOR components/yocto/conf/bblayers.conf
# (paste the contents of project-spec/meta-user/conf/bblayers.conf.append
# before the closing quote of BBLAYERS)

# 2. Build the image
petalinux-build           # ~35 min on first build, <5 min incremental
petalinux-package --boot --u-boot \
    --fpga ../Vitis_Platform/kv260_vcuDecode_vmixDP/.../*.bit \
    --force
```

Outputs (under the build/ directory, not committed here):
- `images/linux/BOOT.BIN`
- `images/linux/image.ub`
- `images/linux/boot.scr`
- `images/linux/petalinux-sdimage.wic`

## Flash to SD card

```sh
sudo dd if=images/linux/petalinux-sdimage.wic of=/dev/sdX bs=4M conv=fsync
```

(Or use Balena Etcher.)

## Verify the package set

After flashing and booting the resulting image, the 22 packages should be
visible through `rpm -qa`. The reference output is captured in
`../../latest-project-files/package-snapshot/rpm-installed.txt`.

```sh
ssh petalinux@192.168.1.78 'rpm -qa | grep -E "vitis-ai|vart|vvas|openamp|xmutil|platformstats|kria-dashboard"'
```

## Notes

- The `build/` directory is excluded from this snapshot because it contains
  approximately 50 GB of Yocto build artefacts that are reproducible from
  the configs included here.
- The kernel version is 5.15.36-xilinx-v2022.2.
- The userspace base is BusyBox + systemd + dropbear.
- The root password is `petalinux`.
- Flask 3.1 and Werkzeug 3.1 (used by `server.py`) are installed
  post-build through `pip install --user flask flask-login` on first boot.
  They are not packaged through Yocto and are therefore not added by this
  configuration. The `run.sh` wrapper performs the pip install if the
  packages are absent.
