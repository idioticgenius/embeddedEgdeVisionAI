# C_GStreamer_Plugin

The custom GStreamer plug-in that performs the per-stream zone-crossing
decision and dispatches alerts through the fast TCM mailbox path and the
slow Unix-domain socket path.

## Source

The canonical location of the latest source on the board is
`latest-project-files/code/zoneguard/`. The files here are byte-identical
copies of that directory, with `shm_alert.h` added from
`latest-project-files/code/`.

| File | Source location | Role |
|---|---|---|
| `zoneguard.c`         | `code/zoneguard/zoneguard.c`        | Full source. GstBaseTransform subclass, 834 lines. Function-by-function walk in `master-draft/build/chapters/15_appendix_zoneguard_walk.md`. |
| `Makefile`            | `code/zoneguard/Makefile`           | Build rules. Invokes `pkg-config` for GStreamer 1.0 and VVAS. Cross-compiles with the AMD aarch64 toolchain. Output: `libgstzoneguard.so`. |
| `libgstzoneguard.so`  | `code/zoneguard/libgstzoneguard.so` | Pre-built shared library captured from the running board. ARM64 ELF, 35,904 bytes. Installed at `/usr/lib/gstreamer-1.0/` on the board. |
| `shm_alert.h`         | `code/shm_alert.h`                   | Shared-memory contract header. Identical copy lives in `C_R5_Firmware/`; both processors include the same definitions. |

The `libgstzoneguard.so` shipped here is the production binary running on
the board. A reader who wants to deploy to a fresh KV260 without rebuilding
can scp this file directly. A reader who wants to modify the plug-in
should rebuild from `zoneguard.c` and the supplied `Makefile` (see below).

## Build

```sh
cd C_GStreamer_Plugin
make CC=aarch64-linux-gnu-gcc CROSS_COMPILE=aarch64-linux-gnu-
scp libgstzoneguard.so petalinux@192.168.1.78:/tmp/
ssh petalinux@192.168.1.78 'sudo cp /tmp/libgstzoneguard.so /usr/lib/gstreamer-1.0/'
```

## Verify

```sh
gst-inspect-1.0 zoneguard      # confirm the plug-in is registered
```

## Pipeline usage

The plug-in is instantiated once per channel:

```
... ! ima0.src_slave_0 ! queue !
zoneguard channel=0 zones-config=/tmp/zoneguard_ch0.json
          event-socket=/tmp/zoneguard.sock
! vvas_xmetaconvert ! vvas_xoverlay ! ...
```

## md5 verification

Each file in this folder matches the canonical board copy byte-for-byte:

```
zoneguard.c          f3da3c1063e74686fd34b0d34cd32afe
Makefile             af9c85f65f57e0b1ac0dd3f4ea57c618
libgstzoneguard.so   88da96dd6fd8ff3802e4848d732917fc
shm_alert.h          8537afdc15c06df24574f97a6bd4f4ed
```
