#!/bin/sh
# Cycle 1: single-camera launch via v4l2src -> IPK -> DPU -> v_mix
# Multichannel form arrives in Cycle 2.
set -e

DEV=${1:-/dev/video0}
WIDTH=1920
HEIGHT=1080

gst-launch-1.0 -v \
    v4l2src device=${DEV} ! \
    video/x-raw,width=${WIDTH},height=${HEIGHT},framerate=30/1 ! \
    vvas_xabrscaler kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! \
    vvas_xinfer kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! \
    fpsdisplaysink video-sink=kmssink sync=false text-overlay=true
