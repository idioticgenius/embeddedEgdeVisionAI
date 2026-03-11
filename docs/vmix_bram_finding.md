# Video Mixer BRAM Trade-off

## Problem

The reference v_mix configuration uses 8 layers. On the KV260 (xck26-sfvc784-2LV-c),
synthesis reports BRAM utilisation above the device budget once the rest of the
multichannel platform (DPU at B3136 + the image-processing compute unit) is
included.

## Investigation

Synthesised the platform at three layer counts:

| v_mix layers | BRAM_18K used | BRAM_18K available | Fits  |
|--------------|---------------|--------------------|-------|
| 8            | 161%          | 144                | no    |
| 7            | 118%          | 144                | no    |
| 6            | 92%           | 144                | yes   |

## Decision

Reduce v_mix to 6 layers. Six layers covers a 2x2 grid of the four channel
outputs plus the background and one overlay layer, which is what the dashboard
needs. Recorded in `image_processing_config.h`.

## Source

`hardware/Vitis_Platform/multichannel_overlay/image_processing_config.h`
