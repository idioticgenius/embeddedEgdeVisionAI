#!/bin/sh
# Cycle 2: four parallel v4l2src branches mixed into a 2x2 grid via v_mix.
# Each branch: v4l2src -> IPK -> DPU (RefineDet) -> v_mix sinkpad
set -e

DEV0=${DEV0:-/dev/video0}
DEV1=${DEV1:-/dev/video1}
DEV2=${DEV2:-/dev/video2}
DEV3=${DEV3:-/dev/video3}
W=1920
H=1080
QW=960
QH=540

gst-launch-1.0 -v \
    vvas_xvideomixer name=mix sink_0::xpos=0    sink_0::ypos=0    sink_0::width=${QW} sink_0::height=${QH} \
                          sink_1::xpos=${QW} sink_1::ypos=0    sink_1::width=${QW} sink_1::height=${QH} \
                          sink_2::xpos=0    sink_2::ypos=${QH} sink_2::width=${QW} sink_2::height=${QH} \
                          sink_3::xpos=${QW} sink_3::ypos=${QH} sink_3::width=${QW} sink_3::height=${QH} \
                          ! kmssink sync=false \
    v4l2src device=${DEV0} ! video/x-raw,width=${W},height=${H} ! vvas_xabrscaler kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! vvas_xinfer kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! mix.sink_0 \
    v4l2src device=${DEV1} ! video/x-raw,width=${W},height=${H} ! vvas_xabrscaler kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! vvas_xinfer kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! mix.sink_1 \
    v4l2src device=${DEV2} ! video/x-raw,width=${W},height=${H} ! vvas_xabrscaler kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! vvas_xinfer kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! mix.sink_2 \
    v4l2src device=${DEV3} ! video/x-raw,width=${W},height=${H} ! vvas_xabrscaler kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! vvas_xinfer kconfig=/opt/xilinx/share/vvas/kernel_refinedet.json ! mix.sink_3
