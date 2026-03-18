# Cycle 1 - Single-Camera POC results

Pipeline: `v4l2src -> IPK -> DPU (RefineDet INT8) -> v_mix -> kmssink`.

Measured per-frame inference at ~24 fps on the integrated platform with the
Logitech C270 enumerated on `/dev/video0`. fpsdisplaysink overlay was used
to confirm sustained throughput over a 30-minute run.

Cycle 1 ships on schedule. Next: scale to four channels (Cycle 2).
