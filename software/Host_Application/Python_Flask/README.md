# Python_Flask

The Python application layer of the project. Six files implementing the
operator-facing web server, the APU-to-RPU bridge and the latency benchmark.

| File | Role |
|---|---|
| `server.py`            | Principal Flask web server. Owns the `PipelineManager` that supervises the gst-launch-1.0 subprocess, the `AlertState` class, and the `ApuGpioBank` and `RpuShmBank` classes. Exposes the HTTP routes for login, pipeline control, zone configuration, alert events, telemetry and the LED-mode toggle. ~1,551 lines. |
| `rpu_bridge.py`        | APU-side rpmsg bridge. Opens `/dev/rpmsg0`, formats `ALERT CH=<n> KIND=<ENTER\|CLEAR\|HB> REASON=<str>\n` messages, parses the R5's echo, and computes round-trip time for the dashboard's "RPU ✓ Nms" badge. |
| `measure_latency.py`   | NFR-08 latency benchmark harness. Drives 300 events through both the APU sysfs path and the RPU shared-memory path; reports p50/p95/p99/max for each. Output JSON is consumed by §6.10.4 of the report. |
| `test_rpu_latency.py`  | Supplementary RPU latency test variant. Uses a different sample size and reports under-load measurements. |
| `server_combined.py`   | Combined-pipeline variant kept for comparison. Uses one gst-launch-1.0 subprocess for all four channels (the production design). |
| `server_pygst.py`      | PyGObject pipeline variant. Uses Python GStreamer bindings rather than a child subprocess. Used during early development; not the production path. |

## Runtime requirements

- Python 3.10
- Flask 2.2 (`pip install flask`)
- Flask-Login (`pip install flask-login`)
- psutil (`pip install psutil`)
- Root privileges (sysfs and `/dev/mem` access)

## How to run

```sh
sudo python3 server.py        # default port 5000, admin/admin login
```

The systemd unit `kv260-multichannel-openamp.service` invokes
`/home/petalinux/run.sh`, which wraps these scripts.
