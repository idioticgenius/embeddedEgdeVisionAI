#!/bin/sh
# Sets and holds the DP-1 mode for the multichannel-openamp overlay.
# Runs in the foreground — modetest keeps the mode active as long as it runs.
# Must be invoked AFTER `xmutil dp_bind` so the mixer CRTC (id=40) exists.
exec modetest -M xlnx -s 52@40:1920x1080@NV16
