#!/bin/sh
# 2-hour soak — ST-08 / NFR-003
# 4-channel RefineDet pipeline with auto-restart on file-source EOS.
# File clips are short (35–60 s), so the watcher re-issues
# /api/pipeline/start whenever the server reports running=false,
# emulating a "service-level continuity" soak rather than a single
# uninterrupted gst-launch.
#
# Every 60 s: samples fps per channel, server RSS, server CPU%,
# load, temp. Logs to /tmp/soak_<ts>.log.
# HDMI output from kmssink remains live during pipeline runs
# (brief blank on each restart, 1–2 s).

set -u
TS=$(date +%Y%m%d_%H%M%S)
LOG=/tmp/soak_${TS}.log
AUTH=/tmp/soak_auth_ck
API=http://127.0.0.1:5000

START_PAYLOAD='{"num_ch":4,"ch0":"/home/petalinux/test_videos/walking.mp4","ch1":"/home/petalinux/test_videos/walking.mp4","ch2":"/home/petalinux/test_videos/walking.mp4","ch3":"/home/petalinux/test_videos/walking.mp4","model0":"refinedet","model1":"refinedet","model2":"refinedet","model3":"refinedet"}'

isots()  { date -Is; }
sstamp() { date +%s; }

start_pipeline() {
  curl -s -b "$AUTH" -X POST -H 'Content-Type: application/json' \
       -d "$START_PAYLOAD" "$API/api/pipeline/start"
}

poke_line() {
  curl -s -b "$AUTH" "$API/api/pipeline/status" 2>/dev/null \
    | python3 -c '
import sys, json, subprocess
try:
    d = json.load(sys.stdin)
except Exception:
    print("STATUS_PARSE_FAIL running=UNKNOWN"); sys.exit(0)

running = d.get("running")
fps = d.get("fps", [])
out = []
for i, f in enumerate(fps):
    v = f.get("mean_fps")
    out.append(f"ch{i}={v:.2f}" if isinstance(v,(int,float)) else f"ch{i}=-")
fps_s = " ".join(out)

rss = 0; cpu = 0.0
try:
    for ln in subprocess.check_output(["ps","-eo","pid,%cpu,rss,comm,args"],
                                       text=True).splitlines():
        if "server.py" in ln and "python3" in ln and "grep" not in ln:
            parts = ln.split(None, 4)
            cpu = float(parts[1]); rss = int(parts[2]); break
except Exception: pass

try: la = open("/proc/loadavg").read().split()[0]
except Exception: la = "-"
try:
    for cand in ("/sys/class/thermal/thermal_zone0/temp",
                 "/sys/class/hwmon/hwmon0/temp1_input"):
        try:
            v = int(open(cand).read().strip()); temp_c = f"{v/1000.0:.1f}"; break
        except Exception: continue
    else: temp_c = "-"
except Exception: temp_c = "-"

print(f"running={running} {fps_s} rss_kb={rss} srv_cpu={cpu} loadavg={la} temp_c={temp_c}")
'
}

# ---- boot ---------------------------------------------------------
echo "# 2-hour soak — started $(isots)" > "$LOG"
echo "# host=$(uname -n) kernel=$(uname -r)" >> "$LOG"

echo petalinux | sudo -S pkill -f 'python3 /home/petalinux/server.py' 2>/dev/null
sleep 3
echo petalinux | sudo -S nohup python3 /home/petalinux/server.py \
  > /tmp/soak_server_${TS}.log 2>&1 &
echo "# server.log=/tmp/soak_server_${TS}.log" >> "$LOG"
sleep 10

rm -f "$AUTH"
HTTP=$(curl -s -c "$AUTH" -d 'username=admin&password=admin' \
            -X POST "$API/login" -o /dev/null -w '%{http_code}')
echo "# login=$HTTP" >> "$LOG"

FIRST=$(start_pipeline)
echo "# start#1 $(isots): $FIRST" >> "$LOG"
sleep 15

# ---- 2-hour loop --------------------------------------------------
END=$(( $(sstamp) + 7200 ))
SAMPLE=0
RESTARTS=0
while [ $(sstamp) -lt $END ]; do
  SAMPLE=$((SAMPLE+1))
  LINE="t=$(isots) n=$SAMPLE $(poke_line)"
  echo "$LINE" >> "$LOG"

  # restart if: (a) pipeline running=False, OR (b) any channel has
  # fps=0.00 (indicates file-source EOS'd while others still play).
  # File clips are 34–60 s so expect many restarts over 2 h; each
  # restart is re-logged with an incrementing counter.
  NEED_RESTART=0
  case "$LINE" in
    *running=False*) NEED_RESTART=1 ;;
    *ch0=0.00*|*ch1=0.00*|*ch2=0.00*|*ch3=0.00*) NEED_RESTART=1 ;;
  esac
  if [ $NEED_RESTART -eq 1 ]; then
    curl -s -b "$AUTH" -X POST "$API/api/pipeline/stop" > /dev/null 2>&1
    sleep 3
    RESTARTS=$((RESTARTS+1))
    R=$(start_pipeline)
    echo "# restart#$RESTARTS $(isots): $R" >> "$LOG"
    sleep 12
  fi
  sleep 30
done

# ---- stop ---------------------------------------------------------
curl -s -b "$AUTH" -X POST "$API/api/pipeline/stop" >> "$LOG" 2>&1
echo "" >> "$LOG"
echo "# stopped $(isots) samples=$SAMPLE restarts=$RESTARTS" >> "$LOG"
echo petalinux | sudo -S pkill -f 'python3 /home/petalinux/server.py' 2>/dev/null

# ---- summary ------------------------------------------------------
python3 <<PY >> "$LOG"
import re, statistics
with open("$LOG") as f:
    lines = [ln for ln in f if ln.startswith("t=")]
def get(ln, k):
    m = re.search(k + r"=([^\s]+)", ln); return m.group(1) if m else None

rss = [int(get(l,"rss_kb")) for l in lines
       if get(l,"rss_kb") and get(l,"rss_kb").isdigit()]
cpu = [float(get(l,"srv_cpu")) for l in lines if get(l,"srv_cpu")]
tmp = [float(get(l,"temp_c")) for l in lines
       if get(l,"temp_c") and get(l,"temp_c") not in ("-","None")]
fps_ch = {i: [] for i in range(4)}
for l in lines:
    for i in range(4):
        m = re.search(rf"ch{i}=([0-9.]+)", l)
        if m:
            try: fps_ch[i].append(float(m.group(1)))
            except: pass

print("=== SUMMARY ===")
print(f"samples={len(lines)} restarts=$RESTARTS")
if rss:
    drift = rss[-1]-rss[0]
    pct = (drift/rss[0]*100) if rss[0] else 0
    print(f"RSS KB: first={rss[0]} last={rss[-1]} max={max(rss)} "
          f"min={min(rss)} drift={drift:+d} ({pct:+.2f}%)")
if cpu:
    print(f"server CPU%: mean={statistics.mean(cpu):.2f} max={max(cpu):.2f}")
if tmp:
    print(f"SoC temp C: mean={statistics.mean(tmp):.2f} max={max(tmp):.2f}")
for i in range(4):
    v = fps_ch[i]
    if v:
        print(f"ch{i} fps: n={len(v)} mean={statistics.mean(v):.2f} "
              f"min={min(v):.2f} max={max(v):.2f}")
PY

echo "DONE: $LOG"
