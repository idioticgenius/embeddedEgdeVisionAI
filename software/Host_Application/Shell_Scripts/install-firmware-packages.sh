#!/bin/sh
# install-firmware-packages.sh
#
# Bring a fresh KV260 PetaLinux 2022.2 image up to the package set required
# by the multichannel-openamp-gpio firmware project. Run as root on the
# board with the PetaLinux dnf feeds reachable.
#
#   sudo ./install-firmware-packages.sh
#
# Notes:
#   - The script is idempotent: dnf skips already-installed packages.
#   - "*" is shell-globbed by dnf into the matching RPM set on the feed.
#   - R5 firmware ELF and the zoneguard plugin are *built locally* from
#     code/ — they are NOT installed by this script.

set -e

if [ "$(id -u)" != "0" ]; then
    echo "ERROR: must run as root (use sudo)" >&2
    exit 1
fi

log() { printf '\n=== %s ===\n' "$*"; }

log "Refreshing dnf metadata"
dnf makecache

##############################################################################
# Vitis AI / DPU runtime
##############################################################################
log "Vitis AI + VART + XRT"
dnf install -y \
    vitis-ai-library \
    vitis-ai-library-dev \
    vart \
    vart-dev \
    xrt \
    xrt-dev

##############################################################################
# VVAS 3.0 GStreamer stack
##############################################################################
log "VVAS core / utils / gst / accel-libs"
dnf install -y \
    packagegroup-petalinux-vvas \
    vvas-core \
    vvas-core-dev \
    vvas-utils \
    vvas-utils-dev \
    vvas-gst \
    vvas-gst-dev \
    vvas-accel-libs \
    libjansson4 \
    libjansson-dev

##############################################################################
# GStreamer plugins used by server.py pipeline
##############################################################################
log "GStreamer 1.18.5 plugins"
dnf install -y \
    gstreamer1.0 \
    gstreamer1.0-omx \
    gstreamer1.0-plugins-base-app \
    gstreamer1.0-plugins-base-apps \
    gstreamer1.0-plugins-base-videoconvert \
    gstreamer1.0-plugins-base-videoscale \
    gstreamer1.0-plugins-base-videotestsrc \
    gstreamer1.0-plugins-base-tcp \
    gstreamer1.0-plugins-base-compositor \
    gstreamer1.0-plugins-base-meta \
    gstreamer1.0-plugins-good-video4linux2 \
    gstreamer1.0-plugins-good-isomp4 \
    gstreamer1.0-plugins-good-rtp \
    gstreamer1.0-plugins-good-rtsp \
    gstreamer1.0-plugins-good-rtpmanager \
    gstreamer1.0-plugins-good-udp \
    gstreamer1.0-plugins-good-multifile \
    gstreamer1.0-plugins-good-videocrop \
    gstreamer1.0-plugins-good-videofilter \
    gstreamer1.0-plugins-good-videobox \
    gstreamer1.0-plugins-good-videomixer \
    gstreamer1.0-plugins-good-meta \
    gstreamer1.0-plugins-bad-videoparsersbad \
    gstreamer1.0-plugins-bad-kms \
    gstreamer1.0-plugins-bad-debugutilsbad \
    gstreamer1.0-plugins-bad-mediasrcbin \
    gstreamer1.0-plugins-bad-videofiltersbad \
    gstreamer1.0-plugins-bad-meta

##############################################################################
# OpenAMP / RPU userspace + kernel modules
##############################################################################
log "OpenAMP / libmetal / rpmsg"
dnf install -y \
    packagegroup-petalinux-openamp \
    packagegroup-petalinux-openamp-echo-test \
    packagegroup-petalinux-openamp-rpc-demo \
    packagegroup-petalinux-openamp-matrix-mul \
    libmetal \
    libmetal-demos \
    openamp-demo-notebooks \
    rpmsg-echo-test \
    rpmsg-mat-mul \
    rpmsg-proxy-app \
    kernel-module-rpmsg-core \
    kernel-module-rpmsg-char \
    kernel-module-rpmsg-ns \
    kernel-module-virtio-rpmsg-bus

##############################################################################
# FPGA / accelerator app management (loadapp, dp_bind, dtbo)
##############################################################################
log "xmutil / dfx-mgr / fpga-manager"
dnf install -y \
    xmutil \
    dfx-mgr \
    fpga-manager-script \
    fpga-manager-util \
    fpga-manager-util-base \
    device-tree-xilinx \
    linux-xlnx-udev-rules

##############################################################################
# Video / display stack (VCU, OMX, Mali)
##############################################################################
log "VCU / OMX / Mali"
dnf install -y \
    kernel-module-xlnx-vcu \
    libvcu-xlnx \
    libomxil-xlnx \
    libmali-xlnx

##############################################################################
# Board telemetry
##############################################################################
log "xlnx-platformstats (Board Stats tab)"
dnf install -y \
    xlnx-platformstats \
    xlnx-platformstats-python \
    kria-dashboard

##############################################################################
# Python deps (server.py + helpers)
##############################################################################
log "Python deps via pip3"
# numpy and pyserial usually come from the PetaLinux feed; install via dnf
# first when available, fall back to pip if the feed is missing them.
dnf install -y python3-pip python3-numpy python3-pyserial 2>/dev/null || true

pip3 install --upgrade --no-cache-dir \
    'Flask==3.1.3' \
    'Werkzeug==3.1.8' \
    'pyserial==3.5'

# numpy is pinned to the system version (1.21.2) shipped with the build —
# do not pip-upgrade it; xlnx_platformstats and OpenCV are linked against it.
python3 -c 'import numpy, sys; print("numpy", numpy.__version__)'

##############################################################################
# Sanity checks
##############################################################################
log "Sanity check"
for cmd in xmutil xbutil gst-launch-1.0 gst-inspect-1.0; do
    if command -v "$cmd" >/dev/null 2>&1; then
        printf '  %-20s OK\n' "$cmd"
    else
        printf '  %-20s MISSING\n' "$cmd"
    fi
done

for plugin in vvas_xinfer vvas_xmetaconvert vvas_xoverlay vvas_xmetaaffixer omxh264dec kmssink fpsdisplaysink; do
    if gst-inspect-1.0 "$plugin" >/dev/null 2>&1; then
        printf '  gst:%-16s OK\n' "$plugin"
    else
        printf '  gst:%-16s MISSING\n' "$plugin"
    fi
done

python3 - <<'PY'
mods = ["flask", "werkzeug", "numpy", "serial", "xlnx_platformstats"]
for m in mods:
    try:
        __import__(m)
        print(f"  py:{m:<22} OK")
    except Exception as e:
        print(f"  py:{m:<22} MISSING ({e})")
PY

log "Done"
echo "Next steps (NOT done by this script):"
echo "  1. Copy firmware dir to /lib/firmware/xilinx/multichannel-openamp-gpio/"
echo "  2. Build R5 firmware ELF -> /lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw"
echo "  3. Build zoneguard plugin: cd code/zoneguard && make && make install"
echo "  4. Place VVAS JSONs under /home/petalinux/jsons/"
echo "  5. xmutil dp_unbind && xmutil loadapp kv260-multichannel-openamp-gpio && xmutil dp_bind"
