#!/bin/sh
# Parallel board-stats logger — runs alongside soak_24h.sh.
# Samples every 60 s for 86400 s (24 h). Captures:
#   - all hwmon rails (voltage/current/power sensors)
#   - all thermal zones
#   - per-core CPU utilisation (4× A53)
#   - /proc/meminfo detailed
#   - /proc/loadavg
#   - /proc/diskstats (for /dev/mmcblk1p2 only)
#   - net rx/tx bytes for eth0
# Reads /sys directly — never calls xlnx_platformstats, so it will
# not trigger the zocl-vs-platformstats SIGBUS race that killed the
# first 2 h attempt.
# Writes to /home/petalinux/soaklog/soak24_stats_<ts>.log.

set -u
LOGDIR=/home/petalinux/soaklog
mkdir -p "$LOGDIR"
TS=$(date +%Y%m%d_%H%M%S)
LOG=${LOGDIR}/soak24_stats_${TS}.log

isots() { date -Is; }

echo "# 24-hour board-stats soak — started $(isots)" > "$LOG"
echo "# host=$(uname -n) kernel=$(uname -r)" >> "$LOG"

# enumerate hwmon sensors once
echo "# === hwmon enumeration ===" >> "$LOG"
for d in /sys/class/hwmon/hwmon*/; do
    name=$(cat "$d/name" 2>/dev/null || echo unknown)
    for f in "$d"*_input "$d"*_label; do
        [ -f "$f" ] || continue
        case "$f" in
            *_label) lbl=$(cat "$f"); echo "# $(basename "$f"): $lbl [hwmon=$name]" >> "$LOG";;
        esac
    done
done

echo "# === thermal zones ===" >> "$LOG"
for z in /sys/class/thermal/thermal_zone*/; do
    t=$(cat "$z/type" 2>/dev/null || echo ?)
    echo "# $(basename "$z"): $t" >> "$LOG"
done

echo "# === sampling begins ===" >> "$LOG"

read_cpu_snapshot() {
    # returns space-separated "user nice sys idle iowait irq softirq" for cpu0..3
    awk '/^cpu[0-3] /{for(i=2;i<=8;i++) printf "%s,",$i; printf " "}' /proc/stat
}

END=$(( $(date +%s) + 86400 ))
PREV_CPU=$(read_cpu_snapshot)
PREV_NET=$(cat /proc/net/dev 2>/dev/null | awk '/eth0:/{print $2" "$10}')

N=0
while [ $(date +%s) -lt $END ]; do
    N=$((N+1))
    TS_S=$(isots)
    {
        echo "--- t=$TS_S n=$N ---"

        # loadavg + uptime
        echo "loadavg=$(cat /proc/loadavg)"

        # memory (selected lines)
        awk '/^MemTotal:|^MemFree:|^MemAvailable:|^Buffers:|^Cached:|^Active:|^Inactive:|^Slab:|^SReclaimable:|^SUnreclaim:|^AnonPages:|^Mapped:|^Shmem:|^KReclaimable:|^KernelStack:|^PageTables:|^CommitLimit:|^Committed_AS:|^Dirty:|^Writeback:/' /proc/meminfo

        # thermal zones
        for z in /sys/class/thermal/thermal_zone*/; do
            t=$(cat "$z/type" 2>/dev/null || echo ?)
            v=$(cat "$z/temp" 2>/dev/null || echo -)
            printf "thermal[%s]=%s milliC\n" "$t" "$v"
        done

        # hwmon rails (print *_input values)
        for f in /sys/class/hwmon/hwmon*/*_input; do
            [ -f "$f" ] || continue
            dir=$(dirname "$f")
            hm=$(cat "$dir/name" 2>/dev/null || echo ?)
            base=$(basename "$f")
            # find matching _label if present
            lblfile="${f%_input}_label"
            if [ -f "$lblfile" ]; then lbl=$(cat "$lblfile"); else lbl="$base"; fi
            v=$(cat "$f" 2>/dev/null || echo -)
            printf "hwmon[%s:%s]=%s\n" "$hm" "$lbl" "$v"
        done

        # per-core CPU delta
        NOW_CPU=$(read_cpu_snapshot)
        echo "$PREV_CPU" | python3 -c '
import sys
prev = sys.stdin.read().strip().split()
now = """'"$NOW_CPU"'""".strip().split()
for i,(a,b) in enumerate(zip(prev, now)):
    pa = [int(x) for x in a.rstrip(",").split(",") if x]
    pb = [int(x) for x in b.rstrip(",").split(",") if x]
    total = sum(pb) - sum(pa)
    idle  = pb[3] - pa[3]
    used = total - idle if total>0 else 0
    pct = 100.0 * used / total if total>0 else 0.0
    print(f"cpu{i}_util_pct={pct:.1f}")
'
        PREV_CPU=$NOW_CPU

        # network bytes delta (eth0)
        NOW_NET=$(cat /proc/net/dev 2>/dev/null | awk '/eth0:/{print $2" "$10}')
        if [ -n "${NOW_NET:-}" ] && [ -n "${PREV_NET:-}" ]; then
            echo "$PREV_NET $NOW_NET" | awk '{
                rx=$3-$1; tx=$4-$2
                print "eth0_rx_bytes_60s="rx
                print "eth0_tx_bytes_60s="tx
            }'
        fi
        PREV_NET=$NOW_NET

        # mmcblk1p2 diskstats
        awk '/mmcblk1p2$/ {
            print "mmcblk1p2_reads_completed="$4
            print "mmcblk1p2_writes_completed="$8
            print "mmcblk1p2_io_ticks_ms="$13
        }' /proc/diskstats

        # --- stability signals (added for 24 h) ---

        # server.py process-level metrics (resident mem, vm size, thread
        # count, FD count). If the server dies, these all print "-".
        SRV_PID=$(pgrep -f 'python3 /home/petalinux/server.py' 2>/dev/null | head -1)
        if [ -n "$SRV_PID" ] && [ -d /proc/$SRV_PID ]; then
            awk '/^VmRSS:|^VmSize:|^VmData:|^VmStk:|^VmExe:|^VmLib:|^VmPTE:|^Threads:|^voluntary_ctxt_switches:|^nonvoluntary_ctxt_switches:/{gsub(":",""); print "srv_"$1"="$2$3}' /proc/$SRV_PID/status 2>/dev/null
            srv_fd=$(ls /proc/$SRV_PID/fd 2>/dev/null | wc -l)
            echo "srv_fd_count=$srv_fd"
            srv_cpu_total=$(awk '{print $14+$15+$16+$17}' /proc/$SRV_PID/stat 2>/dev/null)
            echo "srv_cpu_jiffies_total=${srv_cpu_total:-0}"
        else
            echo "srv_pid=DEAD"
        fi

        # gst-launch process metrics (present only while a pipeline run
        # is active; restarts mean this pid changes every ~60 s).
        GST_PID=$(pgrep -f 'gst-launch-1.0' 2>/dev/null | head -1)
        if [ -n "$GST_PID" ] && [ -d /proc/$GST_PID ]; then
            awk '/^VmRSS:|^VmSize:|^Threads:/{gsub(":",""); print "gst_"$1"="$2$3}' /proc/$GST_PID/status 2>/dev/null
            gst_fd=$(ls /proc/$GST_PID/fd 2>/dev/null | wc -l)
            echo "gst_fd_count=$gst_fd"
        else
            echo "gst_pid=ABSENT"
        fi

        # dmesg tail checkpoint — total lines so far and count of any
        # new WARN/BUG/OOM/error entries since the last sample. Stored
        # as cumulative so summary can compute per-interval deltas.
        DMESG_TOTAL=$(echo petalinux | sudo -S dmesg 2>/dev/null | wc -l)
        DMESG_BAD=$(echo petalinux | sudo -S dmesg 2>/dev/null | grep -cEi 'BUG:|WARNING:|Call trace:|Oops|Out of memory|segfault|audit.*sig=|UBSAN|kernel BUG')
        echo "dmesg_total_lines=$DMESG_TOTAL"
        echo "dmesg_bad_lines=$DMESG_BAD"

        # zocl / DRM bitstream reference count — a growing ref without
        # corresponding 'unlocked' is a context leak.
        DMESG_LOCK=$(echo petalinux | sudo -S dmesg 2>/dev/null | grep -c 'bitstream.*locked, ref=')
        DMESG_UNLK=$(echo petalinux | sudo -S dmesg 2>/dev/null | grep -c 'bitstream.*unlocked, ref=')
        echo "zocl_lock_events=$DMESG_LOCK"
        echo "zocl_unlock_events=$DMESG_UNLK"

        # pipeline error counters from the most recent server log.
        # Non-zero here is the earliest signal of decoder distress.
        SRV_LOG=$(ls -t /home/petalinux/soaklog/soak24_server_*.log 2>/dev/null | head -1)
        if [ -n "$SRV_LOG" ] && [ -f "$SRV_LOG" ]; then
            errs=$(grep -cE 'Error|ERROR|Traceback|CRITICAL|Segmentation|Assertion' "$SRV_LOG" 2>/dev/null || echo 0)
            dec_errs=$(grep -c 'module_dec.cpp' "$SRV_LOG" 2>/dev/null || echo 0)
            echo "server_log_errors_total=$errs"
            echo "server_log_decoder_errors_total=$dec_errs"
        fi

    } >> "$LOG"

    sleep 60
done

echo "--- stopped $(isots) samples=$N ---" >> "$LOG"

# summary
python3 <<PY >> "$LOG"
import re, statistics, collections
L = open("$LOG").read()
blocks = [b for b in L.split("--- t=") if b.strip()]
print()
print("=== BOARD-STATS SUMMARY ===")
print(f"blocks_seen={len(blocks)}")

# collect per-metric series
series = collections.defaultdict(list)
for b in blocks:
    for line in b.splitlines():
        m = re.match(r"^([A-Za-z0-9_\[\]\-:]+)=(-?[0-9.]+)", line.strip())
        if m:
            try:
                series[m.group(1)].append(float(m.group(2)))
            except ValueError:
                pass

# print aggregate for the interesting metrics
interesting = sorted([k for k in series if any(tok in k for tok in
    ("MemAvailable","temp","cpu","util","hwmon","eth0","mmcblk"))])

def stat(name, vals):
    if not vals: return
    if all(v == vals[0] for v in vals):
        print(f"{name}: const={vals[0]}")
    else:
        print(f"{name}: n={len(vals)} mean={statistics.mean(vals):.2f} "
              f"min={min(vals):.2f} max={max(vals):.2f} first={vals[0]:.2f} last={vals[-1]:.2f}")

for k in interesting:
    stat(k, series[k])
PY

echo "DONE: $LOG"
