#!/bin/sh
# 2-hour soak (RPU mode) — ST-08 / NFR-003
# Same payload as the 24 h run but END=7200 s instead of 86400 s so
# the report can cite a full day-and-night continuous-operation run
# covering ambient-temperature swing, logrotate/cron interactions,
# and slow RSS drift invisible in a 2 h test.
#
# 4-channel RefineDet pipeline with auto-restart on file-source EOS.
# Every 60 s: samples fps per channel, server RSS, server CPU%,
# load, temp. Logs to /home/petalinux/soaklog/soak24_<ts>.log
# (SD-card, persists across reboots).

set -u
LOGDIR=/home/petalinux/soaklog
mkdir -p "$LOGDIR"
TS=$(date +%Y%m%d_%H%M%S)
LOG=${LOGDIR}/soak2h_${TS}.log
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
  # Pull pipeline/status, led/mode (for pins + rpu_link), shm/stats
  # (for the APU↔R5 alert page seq/flags/magic), and events (for
  # alert counts) in one shot and fold into a single log line.
  STATUS=$(curl -s -b "$AUTH" "$API/api/pipeline/status" 2>/dev/null)
  LEDMODE=$(curl -s -b "$AUTH" "$API/api/led/mode" 2>/dev/null)
  SHMST=$(curl -s -b "$AUTH" "$API/api/shm/stats" 2>/dev/null)
  EVENTS=$(curl -s -b "$AUTH" "$API/api/events" 2>/dev/null)
  python3 - "$STATUS" "$LEDMODE" "$SHMST" "$EVENTS" <<'PY'
import sys, json, subprocess
argv=sys.argv[1:]
def parse(s):
    try: return json.loads(s)
    except Exception: return None
st=parse(argv[0]); lm=parse(argv[1]); sh=parse(argv[2]); ev=parse(argv[3])
if st is None:
    print("STATUS_PARSE_FAIL running=UNKNOWN"); sys.exit(0)

running = st.get("running")
fps = st.get("fps", [])
out=[]
for i,f in enumerate(fps):
    v=f.get("mean_fps")
    out.append(f"ch{i}={v:.2f}" if isinstance(v,(int,float)) else f"ch{i}=-")
fps_s=" ".join(out)

rss=0; cpu=0.0
try:
    for ln in subprocess.check_output(["ps","-eo","pid,%cpu,rss,comm,args"], text=True).splitlines():
        if "server.py" in ln and "python3" in ln and "grep" not in ln:
            p=ln.split(None,4); cpu=float(p[1]); rss=int(p[2]); break
except Exception: pass

try: la=open("/proc/loadavg").read().split()[0]
except Exception: la="-"

# board temp via ams hwmon (sysfs-only, never via xlnx_platformstats)
temp_c="-"
try:
    import glob
    for f in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        try:
            v=int(open(f).read().strip())
            temp_c=f"{v/1000.0:.1f}"; break
        except Exception: continue
except Exception: pass

# RPU/LED pathway fields
mode = (lm or {}).get("mode","-")
rpu_link = (lm or {}).get("rpu_link", None)
pins = (lm or {}).get("pins", {}) or {}
pins_s = ",".join(str(pins.get(str(i), pins.get(i,"-"))) for i in range(4))

shm_ok = (sh or {}).get("ok", False)
shm_seq = (sh or {}).get("seq", "-")
shm_flags = (sh or {}).get("flags", "-")
shm_magic = (sh or {}).get("magic", "-")

# events snapshot — server returns alerts as dict keyed by channel:
#   {"alerts":{"0":{active,since,reason,rpu_confirmed,rpu_rtt_ms}, ...}, "events":[...]}
alerts = (ev or {}).get("alerts", {}) or {}
active = 0
rpu_conf=[0,0,0,0]; rpu_rtts=["-","-","-","-"]
if isinstance(alerts, dict):
    for k,a in alerts.items():
        if not isinstance(a, dict): continue
        try: ch=int(k)
        except Exception: continue
        if not (0 <= ch < 4): continue
        if a.get("active"): active += 1
        if a.get("rpu_confirmed"): rpu_conf[ch]=1
        r=a.get("rpu_rtt_ms")
        if isinstance(r,(int,float)): rpu_rtts[ch]=f"{r:.2f}"
ev_list = (ev or {}).get("events", []) or []
ev_count = len(ev_list)

print(f"running={running} {fps_s} rss_kb={rss} srv_cpu={cpu} loadavg={la} temp_c={temp_c} "
      f"mode={mode} rpu_link={rpu_link} pins={pins_s} "
      f"shm_ok={shm_ok} shm_magic={shm_magic} shm_seq={shm_seq} shm_flags={shm_flags} "
      f"alerts_active={active} events_tail={ev_count} "
      f"rpu_conf={','.join(str(x) for x in rpu_conf)} rpu_rtt_ms={','.join(rpu_rtts)}")
PY
}

# ---- boot ---------------------------------------------------------
echo "# 2-hour soak — started $(isots)" > "$LOG"
echo "# host=$(uname -n) kernel=$(uname -r)" >> "$LOG"

echo petalinux | sudo -S pkill -f 'python3 /home/petalinux/server.py' 2>/dev/null
sleep 3
echo petalinux | sudo -S nohup python3 /home/petalinux/server.py \
  > ${LOGDIR}/soak2h_server_${TS}.log 2>&1 &
echo "# server.log=${LOGDIR}/soak2h_server_${TS}.log" >> "$LOG"
sleep 10

rm -f "$AUTH"
HTTP=$(curl -s -c "$AUTH" -d 'username=admin&password=admin' \
            -X POST "$API/login" -o /dev/null -w '%{http_code}')
echo "# login=$HTTP" >> "$LOG"

# --- zones: central 60%×60% rect per channel (960×540 render rect
# each), so anyone walking near frame centre in walking.mp4 triggers
# an alert. Set once; persists across pipeline restarts.
for CH in 0 1 2 3; do
  ZR=$(curl -s -b "$AUTH" -X POST -H 'Content-Type: application/json' \
       -d "{\"ch\":$CH,\"zones\":[{\"type\":\"rect\",\"name\":\"centre_ch$CH\",\"x\":192,\"y\":108,\"w\":576,\"h\":324}]}" \
       "$API/api/pipeline/zones")
  echo "# zone_set ch$CH: $ZR" >> "$LOG"
done

# --- LED mode: switch to RPU before pipeline start so alerts fire
# over the R5 TCM path. ensure_rpu_running() kicks remoteproc0 start
# inside the server; any failure is logged but the soak continues.
LEDR=$(curl -s -b "$AUTH" -X POST -H 'Content-Type: application/json' \
       -d '{"mode":"rpu"}' "$API/api/led/mode")
echo "# led_mode_set: $LEDR" >> "$LOG"
sleep 3

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
