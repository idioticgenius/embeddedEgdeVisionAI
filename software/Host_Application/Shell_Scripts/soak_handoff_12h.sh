#!/bin/bash
# Wait until +12h boundary (17:35:53 local), then:
#  1. Stop the running 24h soak + stats logger.
#  2. Archive the 24h logs as soak12h_*.
#  3. Launch the 2h RPU-mode soak pair.
TARGET="2026-04-24 17:35:53"
TARGET_S=$(date -d "$TARGET" +%s 2>/dev/null)
[ -z "$TARGET_S" ] && TARGET_S=$(date -d "today 17:35:53" +%s)
until [ "$(date +%s)" -ge "$TARGET_S" ]; do
  sleep 30
done
echo "[handoff] +12h reached at $(date -Is)"

# 1. Stop the running 24h soak + stats logger (kill the bash scripts).
echo petalinux | sudo -S pkill -f 'bash /home/petalinux/soak_24h.sh' 2>/dev/null
echo petalinux | sudo -S pkill -f 'bash /home/petalinux/soak_stats_24h.sh' 2>/dev/null
sleep 2
echo petalinux | sudo -S pkill -f 'python3 /home/petalinux/server.py' 2>/dev/null
echo petalinux | sudo -S pkill -f 'gst-launch-1.0' 2>/dev/null
sleep 4
echo "[handoff] processes stopped at $(date -Is)"

# 2. Archive 24h logs as soak12h_* so they're tagged for the report.
LOGDIR=/home/petalinux/soaklog
for f in "$LOGDIR"/soak24_*.log; do
  [ -f "$f" ] || continue
  base=$(basename "$f")
  new=$(echo "$base" | sed 's/^soak24_/soak12h_/')
  mv "$f" "$LOGDIR/$new"
done
echo "[handoff] logs renamed to soak12h_* at $(date -Is)"
ls -la "$LOGDIR"/soak12h_*.log 2>/dev/null

# 3. Launch the 2h RPU-mode soak pair.
nohup bash /home/petalinux/soak_2h_rpu.sh > /home/petalinux/soaklog/2h_launch.out 2>&1 &
sleep 3
nohup bash /home/petalinux/soak_stats_2h_rpu.sh > /home/petalinux/soaklog/2h_stats_launch.out 2>&1 &
echo "[handoff] 2h soak pair launched at $(date -Is)"
