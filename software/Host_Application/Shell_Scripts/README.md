# Shell_Scripts

Bring-up wrappers, soak runners and statistics-gathering scripts.

| File | Role |
|---|---|
| `run.sh`                     | Canonical bring-up. Loads overlay through `xmutil loadapp`, starts R5 remoteproc, brings up DisplayPort plane, exec-launches Flask server. Invoked by the systemd unit. |
| `display.sh`                 | `modetest` wrapper for the DP/HDMI plane bring-up. |
| `blink_led.sh`               | Manual LED test through the APU sysfs path. Useful for confirming the AXI GPIO core is reachable before R5 is loaded. |
| `soak_2h.sh`                 | Two-hour soak runner. Drives the pipeline through periodic restart cycles for 2 hours. |
| `soak_2h_rpu.sh`             | Two-hour soak runner with the RPU path active. |
| `soak_24h.sh`                | 24-hour soak runner (used by the 12-hour campaign with early termination). |
| `soak_handoff_12h.sh`        | 12-hour soak handoff variant for cross-session continuation. |
| `soak_stats_2h.sh`           | Statistics-gathering daemon paired with `soak_2h.sh`. Samples CPU, RSS, temperature, power every 30 s. |
| `soak_stats_2h_rpu.sh`       | RPU-mode counterpart. |
| `soak_stats_24h.sh`          | 24-hour-mode counterpart. |

## How they fit together

The systemd unit `kv260-multichannel-openamp.service` invokes `run.sh`,
which in turn brings up the system and exec-launches the Flask server.
For the soak campaign, `soak_24h.sh` is invoked manually as root after
the system is already up; it spawns `soak_stats_24h.sh` in parallel and
the two together produce the per-sample log read by §6.10.5 of the report.

The output logs land in `references/soaklog/` after a campaign and are
read by the matplotlib chart generator in
`references/evidence/diagrams/make_soak_charts.py` to produce the
fig_soak12_*.png figures.
