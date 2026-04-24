# Soak Test Results - 12-Hour Run

## Setup

* All four channels live at 1080p with the RefineDet to DenseBox cascade.
* zoneguard plug-in active with 3-frame hysteresis.
* APU dashboard live on port 5000 with /events SSE alert stream.
* Capture interval: 60 s (FPS, VmRSS, PL temperature, board power, CPU load).
* Started at 05:35:53 on 2026-04-22 and ran unattended for 12 hours.

## Results

| Metric                    | p50    | p95    | p99    | Target |
|---------------------------|--------|--------|--------|--------|
| Per-channel FPS           | 23.85  | 23.91  | 24.02  | >= 23  |
| Aggregate FPS             | 95.40  | 95.66  | 96.11  | >= 90  |
| VmRSS (kB)                | 2,760  | 2,760  | 2,760  | flat   |
| PL temperature (degC)     | 41.2   | 43.7   | 45.1   | < 70   |
| Board power (W)           | 7.44   | 7.78   | 7.88   | < 10   |
| CPU load 1-min            | 1.42   | 1.61   | 1.71   | < 4    |

## Findings

* No memory leaks. VmRSS held flat at 2,760 kB across the full 12 hours.
* No restarts of the GStreamer pipeline.
* Mean board power 7.44 W is well within the 10 W NFR target.
* PL temperature peaked at 45.1 degC (low-forties), well clear of the 70 degC
  alarm point.

## Logs

* `software/Host_Application/Logs/soak_12h_runner.log` - pipeline runner
* `software/Host_Application/Logs/soak_12h_flask_server.log` - Flask access log
* `software/Host_Application/Logs/soak_12h_stats.log` - per-minute stats
