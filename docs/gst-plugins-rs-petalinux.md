# Adding `gst-plugins-rs` to a PetaLinux 2022.2 Image

Companion to `rpu-enablement.md` §23.16. This is the production path
for getting Rust-based GStreamer plugins (e.g. `fallbacksrc`,
`fallbackswitch`) onto the Kria KV260, built into the image rather
than compiled on-board.

All references below point to official, canonical sources:
Yocto Project, OpenEmbedded, PetaLinux, and the upstream GStreamer
project.

---

## 1. Why this instead of on-board Rust build?

We verified on-board builds work (`rpu-enablement.md` §23.16) but:

- They require ~1.5 GB of free rootfs for the Rust toolchain and the
  crate `target/` tree.
- They need internet access from the board and ~8 minutes of A53
  compile time for one plugin.
- The toolchain should be removed afterwards, which means rebuilding
  from scratch every time you want to update or add another plugin.
- No reproducibility — the next image flash starts empty again.

Building the plugin into the PetaLinux image solves all of these:
the plugin is part of the `.wic` / rootfs, appears in every board
that boots that image, is under Yocto's dependency tracking, and
builds once on the host.

---

## 2. Prerequisites on the host

Canonical PetaLinux 2022.2 build host:

- Ubuntu 20.04 LTS (AMD's officially supported host for 2022.2 — see
  [UG1144 PetaLinux Tools Documentation](https://docs.amd.com/r/2022.2-English/ug1144-petalinux-tools-reference-guide/Installation-Requirements)).
- PetaLinux 2022.2 tools installed and sourced
  (`source /opt/petalinux/2022.2/settings.sh`).
- A working PetaLinux project for Kria KV260 (`petalinux-create
  -t project …` with the `xilinx-kv260-starterkit-v2022.2-final.bsp`).
- `~30 GB` free disk for the build cache + sstate.

---

## 3. Upstream packaging: `gstreamer1.0-rs`

The Rust GStreamer plugin family is packaged in the
**meta-openembedded / meta-multimedia** layer as the recipe
`recipes-multimedia/gstreamer/gstreamer1.0-rs.bb`. Sources:

- OpenEmbedded layer index:
  <https://layers.openembedded.org/layerindex/recipe/103030/>
- meta-openembedded repo (honister branch, matches PetaLinux 2022.2):
  <https://git.openembedded.org/meta-openembedded/tree/meta-multimedia/recipes-multimedia/gstreamer?h=honister>
- Upstream plugin source (gst-plugins-rs) the recipe fetches from:
  <https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs>
- Plugin documentation (fallbackswitch / fallbacksrc):
  <https://gstreamer.freedesktop.org/documentation/fallbackswitch/>

The recipe in `honister` provides `gstreamer-rs 0.18.x` which matches
the GStreamer 1.18.5 runtime that ships with PetaLinux 2022.2.

---

## 4. Step-by-step integration

### Step 4.1 — Verify meta-openembedded is already available

PetaLinux 2022.2 bundles `meta-openembedded` with every BSP. Confirm:

```sh
cd <petalinux-project>
petalinux-config
# Top-level → Yocto Settings → User Layers
#   If meta-multimedia is not listed, continue with Step 4.2.
```

Alternatively inspect:

```sh
cat components/yocto/layers/meta-openembedded/meta-multimedia/conf/layer.conf
```

If that file exists, the layer is present but may not be activated.

### Step 4.2 — Enable the `meta-multimedia` sub-layer

PetaLinux ships the meta-openembedded layer but by default only
activates `meta-oe` and `meta-python`. Activate `meta-multimedia`:

```sh
cd <petalinux-project>
petalinux-config
# → Yocto Settings
# → User Layers
# → add: ${proot}/components/yocto/layers/meta-openembedded/meta-multimedia
```

Or edit `project-spec/meta-user/conf/bblayers.conf` manually:

```
BBLAYERS += " ${PROOT}/components/yocto/layers/meta-openembedded/meta-multimedia"
```

Relevant reference:
[Yocto Project Layer Model documentation](https://docs.yoctoproject.org/3.4/overview-manual/yp-intro.html#the-yocto-project-layer-model).

### Step 4.3 — Add the recipe to the rootfs

Two equivalent options:

**Option A — `petalinux-config -c rootfs` menuconfig (interactive).**

```sh
petalinux-config -c rootfs
# → user packages → (search) gstreamer1.0-rs → toggle on
```

This writes to `project-spec/configs/rootfs_config`.

**Option B — `IMAGE_INSTALL:append` (scriptable, preferred for CI).**

Edit `project-spec/meta-user/recipes-core/images/petalinux-image-minimal.bbappend`
(create if missing):

```
IMAGE_INSTALL:append = " gstreamer1.0-rs"
```

`IMAGE_INSTALL:append` is the Yocto 3.4+ override-syntax form of the
older `IMAGE_INSTALL_append`. Reference:
[Yocto Project Overrides Syntax documentation](https://docs.yoctoproject.org/3.4/bitbake/bitbake-user-manual/bitbake-user-manual-metadata.html#appending-and-prepending-override-style-syntax).

### Step 4.4 — Build

```sh
petalinux-build
```

First build picks up `gst-plugins-rs` sources, pulls its Rust
dependency tree, compiles against the Kria sysroot, and places
`libgstfallbackswitch.so` (and the other gst-plugin-rs plugins
available in that recipe) into the target rootfs. Expect ~30 min
extra on a first clean build; subsequent builds hit sstate and
incremental-rebuild in seconds.

### Step 4.5 — Package and flash

```sh
petalinux-package --wic
# → writes images/linux/petalinux-sdimage.wic
```

Flash to the SD card with Balena Etcher / `bmaptool copy`. Boot.

### Step 4.6 — Verify on the board

```sh
gst-inspect-1.0 fallbackswitch
gst-inspect-1.0 fallbacksrc
ls -l /usr/lib/gstreamer-1.0/libgstfallbackswitch.so
```

If `gst-inspect-1.0` reports "No such element or plugin" after a
successful build, clear the plugin cache:

```sh
rm -rf ~/.cache/gstreamer-1.0/
gst-inspect-1.0 fallbackswitch
```

---

## 5. PetaLinux 2022.2 / honister-specific caveats

- **The recipe is in meta-openembedded, not in AMD's `oe-remote-repo`
  RPM feed.** `dnf install` on the running board will not find
  `gst-plugins-rs`. We confirmed this on 2026-04-22:
  `dnf search fallbacksrc fallbackswitch` → *No matches found*.
  This is expected — AMD's vendor feed only carries the packages
  they've QA'd for Kria, not the full meta-openembedded catalogue.
  The only way to ship Rust-based plugins on this release is to
  rebuild the image.

- **Rust cross-toolchain.** The `gstreamer1.0-rs.bb` recipe depends
  on `rust-native` and `rust-cross-aarch64`, which meta-openembedded
  pulls in automatically when the recipe is built. No extra layer
  needed.

- **Disk budget on host.** First build with Rust cross-toolchain adds
  ~3–5 GB to the Yocto sstate cache. Subsequent incremental builds
  are cheap.

- **Which plugins come along?** The `gstreamer1.0-rs` recipe
  (honister version) packages the whole gst-plugins-rs set available
  at that tag: `fallbackswitch`, `threadshare`, `rtpav1`, `rtpgcc`,
  `webrtchttp`, `awstranscriber`, `awss3`, `hlssink3`, `rsonvif`,
  etc. If you only want one, you can override
  `PACKAGES` / `FILES:<pkg>` in a local bbappend — but for a Kria
  prototype the size (~10 MB total) is usually irrelevant and the
  default split-package behaviour is fine.

- **GStreamer 1.18 vs newer gst-plugins-rs.** The recipe on honister
  pins `gst-plugins-rs` to the `0.18.x` branch to match the
  `gstreamer-rs` 0.18 bindings, which in turn targets GStreamer 1.18.
  If you later upgrade to PetaLinux 2023.2 (GStreamer 1.20) or
  2024.1 (GStreamer 1.22), the honister recipe will not work and
  you must switch to `kirkstone`/`scarthgap` meta-openembedded and
  the matching gst-plugins-rs version.

---

## 6. Reproducible quick-reference

For the report / demo reproducibility section:

```sh
# host, inside an Ubuntu 20.04 build VM:
source /opt/petalinux/2022.2/settings.sh
petalinux-create -t project -s xilinx-kv260-starterkit-v2022.2-final.bsp
cd xilinx-kv260-starterkit-2022.2

# activate meta-multimedia
echo 'BBLAYERS += " ${PROOT}/components/yocto/layers/meta-openembedded/meta-multimedia"' \
    >> project-spec/meta-user/conf/bblayers.conf

# install the plugin family
mkdir -p project-spec/meta-user/recipes-core/images
cat >> project-spec/meta-user/recipes-core/images/petalinux-image-minimal.bbappend <<'EOF'
IMAGE_INSTALL:append = " gstreamer1.0-rs"
EOF

petalinux-build
petalinux-package --wic
# flash images/linux/petalinux-sdimage.wic
```

Total effort: ~2–4 hours on a first build (dominated by the Rust
cross-toolchain bootstrap), < 10 min on incremental rebuilds.

---

## 7. When to prefer on-board build instead

The on-board Rust build (documented in `rpu-enablement.md` §23.16)
is the right choice when:

- The host PetaLinux project isn't available / isn't yours.
- You need to prototype a plugin change quickly and don't want to
  re-flash.
- The board has spare rootfs capacity (≥ 2 GB free) and internet
  access.

The image-rebuild path documented here is the right choice when:

- The plugin will ship with the product, not just a prototype.
- You want reproducible builds tracked by Yocto.
- You're deploying to multiple boards.
- You want the board rootfs to stay minimal (no Rust toolchain at
  runtime).

---

## 8. Sources cited (all official)

- [AMD/Xilinx PetaLinux 2022.2 Reference Guide (UG1144)](https://docs.amd.com/r/2022.2-English/ug1144-petalinux-tools-reference-guide)
- [Yocto Project 3.4 (honister) documentation](https://docs.yoctoproject.org/3.4/)
- [Yocto Overrides Syntax](https://docs.yoctoproject.org/3.4/bitbake/bitbake-user-manual/bitbake-user-manual-metadata.html#appending-and-prepending-override-style-syntax)
- [OpenEmbedded meta-openembedded (honister)](https://git.openembedded.org/meta-openembedded/log/?h=honister)
- [OpenEmbedded Layer Index — gstreamer1.0-rs](https://layers.openembedded.org/layerindex/recipe/103030/)
- [GStreamer fallbackswitch plugin documentation](https://gstreamer.freedesktop.org/documentation/fallbackswitch/)
- [GStreamer gst-plugins-rs repository](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs)
