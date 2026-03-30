#!/usr/bin/env python3
#
# measure_latency.py
# ==================
#
# PURPOSE
#     Micro-benchmark for the LED action paths on the Kria KV260
#     multichannel Edge Vision AI prototype. Produces the
#     NFR-001 / NFR-002 action-latency numbers for the final report.
#
#     Two independent paths are measured in a single invocation:
#
#         APU-e2e :  UDS datagram (zoneguard-compat) -> server.py ->
#                    gpio-sysfs driver -> axi_gpio_0 DATA register.
#                    What Linux user-space + scheduler contribute.
#
#         RPU-hw  :  script writes TCM_0B flag bit -> R5 baremetal
#                    mirror loop reads TCM, writes axi_gpio_0.
#                    What the R5 tight loop contributes.
#
#     Both are stopwatch-on-the-same-clock measurements: we mmap
#     /dev/mem and busy-spin on the axi_gpio_0 DATA register, so the
#     start and end timestamps come from one `time.monotonic_ns()`
#     on one core. No IPC, no logging, no noise from the pipeline.
#
#
# WHY TWO SEPARATE PHASES IN ONE RUN
#     The two paths cannot coexist — they fight for axi_gpio_0:
#       - APU path:  server calls apu_gpio.set_channel(ch, 1), which
#                    goes through the Linux gpio-sysfs driver and
#                    ends up writing one bit of axi_gpio_0 DATA.
#       - RPU path:  R5's mirror loop reads TCM flags and writes the
#                    whole axi_gpio_0 DATA word every ~µs.
#     If both are active, the R5's continuous writes clobber the
#     APU sysfs writes as fast as they land and the APU bit never
#     stabilises high.
#
#     So this script forces the correct mode for each phase:
#       APU phase  ->  R5 stopped  (echo stop > remoteproc state)
#                      led_mode   = apu
#       RPU phase  ->  R5 running (echo start > remoteproc state)
#                      led_mode   = rpu
#
#     On exit (even on error/interrupt) it returns the system to
#     (R5 running, led_mode=rpu) so normal operation resumes.
#
#
# WHY AUTO-LOGIN
#     server.py's /api/led/mode endpoint is behind the Flask session
#     auth gate added in this project. To flip led_mode from the
#     script we have to POST /login first (admin/admin by default),
#     carry the cookie, then POST the mode change. If you want to
#     avoid auto-login you can set led_mode manually in the web UI
#     before each phase — but the script still needs to stop/start
#     the R5, which it can do directly via /sys (requires sudo).
#
#
# PRECONDITIONS
#     - run as root (sudo)                  - needs /dev/mem + remoteproc write
#     - server.py running                   - provides the UDS listener
#     - no gst-launch pipeline running      - zoneguard would contend for the bit
#     - UI_USER / UI_PASS env if not admin  - defaults are admin / admin
#
#
# USAGE
#     sudo python3 /home/petalinux/measure_latency.py
#     sudo python3 /home/petalinux/measure_latency.py -n 500 --ch 1
#     sudo python3 /home/petalinux/measure_latency.py --skip-apu
#     UI_PASS=mypw sudo -E python3 /home/petalinux/measure_latency.py
#
#     Results are printed to stdout and written as JSON to
#     /home/petalinux/latency_results.json (override with --out).
#
#
# OUTPUT FORMAT (per phase)
#     rise 0->1 : time from "event sent" to "GPIO bit observed high"
#     fall 1->0 : time from "event sent" to "GPIO bit observed low"
#     Each line: n=<count>, mean, p50, p95, p99, max, stdev — all µs.
#
#
# LIMITATIONS
#     - Our time source is Python's monotonic_ns, ~100 ns resolution.
#       Anything < ~500 ns is measurement noise.
#     - The APU path measurement is whole-path (UDS + server + driver),
#       not broken down per stage. If you need a stage-by-stage split
#       you'll need bpftrace/ftrace or add timestamp probes in server.
#     - The RPU path measurement is R5-mirror only; it does not
#       include the zoneguard-writes-TCM step (that step takes a
#       single ~µs store from the GStreamer thread — negligible).
#     - Python GC pauses occasionally show up as max-spike outliers.
#       Tail latency has a long right tail for APU; mean/p50 are the
#       honest numbers to quote.
#
# --------------------------------------------------------------------

import argparse, json, mmap, os, socket, statistics, struct, sys, time
import urllib.parse, urllib.request
from http.cookiejar import CookieJar

# --- Hardware addresses (Kria KV260, multichannel-openamp-gpio overlay) ---
TCM     = (0xFFE20000, 0x04)  # TCM_0B base, flags word at offset +4
GPIO    = (0xA0010000, 0x00)  # axi_gpio_0 base, DATA register at +0
ZG_SOCK = "/tmp/zoneguard.sock"
RPROC   = "/sys/class/remoteproc/remoteproc0/state"
URL     = "http://127.0.0.1:5000"
PAGE    = 0x1000              # mmap page granularity


# ============================================================
# Low-level /dev/mem helpers
# ============================================================

def reg(base):
    """mmap a 4K page starting at `base`. Caller reads/writes 32-bit
    words with rd/wr helpers below. Requires root."""
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    return mmap.mmap(fd, PAGE, mmap.MAP_SHARED,
                     mmap.PROT_READ | mmap.PROT_WRITE, offset=base)

def rd(m, off):     return struct.unpack_from("<I", m, off)[0]
def wr(m, off, v):  struct.pack_into("<I", m, off, v)


def spin_until(pred, budget_ns):
    """Busy-wait until pred() returns truthy or budget_ns expires.
    Returns monotonic_ns at the moment pred fired, or None on timeout.
    Busy-waiting (not sleeping) keeps the measurement floor at
    ~Python-call overhead, typically a few hundred ns."""
    end = time.monotonic_ns() + budget_ns
    while time.monotonic_ns() < end:
        if pred():
            return time.monotonic_ns()
    return None


def busy(us):
    """Inter-event settle: don't use time.sleep() for small gaps —
    the scheduler wake adds ~tens of µs jitter which contaminates
    the next sample. Busy-spin a monotonic clock instead."""
    end = time.monotonic_ns() + us * 1000
    while time.monotonic_ns() < end:
        pass


# ============================================================
# System-state control (R5 + server.py)
# ============================================================

def rproc_state():
    with open(RPROC) as f:
        return f.read().strip()


def rproc_set(want):
    """want in {'start','stop'}. No-op if already in target state.
    Polls up to ~2 s for the kernel to settle the transition. Note:
    'running' means the ELF has booted — the mirror loop may need
    further warm-up; see wait_r5_mirror() below."""
    cur = rproc_state()
    if want == "start" and cur.startswith("running"): return
    if want == "stop"  and cur == "offline":          return
    with open(RPROC, "w") as f:
        f.write(want)
    for _ in range(40):
        s = rproc_state()
        if (want == "start" and s.startswith("running")) \
        or (want == "stop"  and s == "offline"):
            return
        time.sleep(0.05)


def wait_r5_mirror(tcm, gpio, ch, timeout_s=2.0):
    """After rproc_set('start') the R5 is loaded but the TCM->GPIO
    mirror loop has not necessarily executed yet. Confirm it's live
    by writing a bit and waiting for the mirror before benchmarking.
    Returns True if the mirror responded within timeout_s."""
    mask = 1 << ch
    wr(tcm, TCM[1], rd(tcm, TCM[1]) & ~mask); time.sleep(0.01)
    wr(tcm, TCM[1], rd(tcm, TCM[1]) | mask)
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if rd(gpio, GPIO[1]) & mask:
            wr(tcm, TCM[1], rd(tcm, TCM[1]) & ~mask)
            return True
        time.sleep(0.02)
    wr(tcm, TCM[1], rd(tcm, TCM[1]) & ~mask)
    return False


def server_login():
    """Returns a urllib opener carrying a valid session cookie.
    Raises on failure. Uses admin/admin unless UI_USER/UI_PASS set."""
    cj = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj))
    body = urllib.parse.urlencode({
        "username": os.environ.get("UI_USER", "admin"),
        "password": os.environ.get("UI_PASS", "admin"),
    }).encode()
    opener.open(URL + "/login", data=body, timeout=2).read()
    return opener


def server_mode(opener, mode):
    """POST /api/led/mode via a logged-in opener."""
    req = urllib.request.Request(
        URL + "/api/led/mode",
        data=json.dumps({"mode": mode}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    opener.open(req, timeout=2).read()


# ============================================================
# Measurement cores (one per path)
# ============================================================

def bench_rpu(ch, n, settle_us):
    """Synthetic TCM->GPIO loop. Script plays both 'zoneguard' and
    'observer'. Measures R5 poll period + AXI-store propagation to
    axi_gpio_0 DATA. Requires R5 running (wait_r5_mirror already
    confirmed the mirror is live)."""
    tcm, gpio = reg(TCM[0]), reg(GPIO[0])
    mask = 1 << ch
    wr(tcm, TCM[1], rd(tcm, TCM[1]) & ~mask)   # start clean
    # Wait for GPIO to drop before we start the first rise.
    spin_until(lambda: not (rd(gpio, GPIO[1]) & mask), 10_000_000)

    rise, fall = [], []
    for _ in range(n):
        # RISE: write 1, time until GPIO goes high.
        wr(tcm, TCM[1], rd(tcm, TCM[1]) | mask)
        t0 = time.monotonic_ns()
        h = spin_until(lambda: rd(gpio, GPIO[1]) & mask, 50_000_000)
        if h is not None:
            rise.append(h - t0)

        # FALL: write 0, time until GPIO goes low.
        wr(tcm, TCM[1], rd(tcm, TCM[1]) & ~mask)
        t0 = time.monotonic_ns()
        h = spin_until(lambda: not (rd(gpio, GPIO[1]) & mask), 50_000_000)
        if h is not None:
            fall.append(h - t0)

        busy(settle_us)
    return rise, fall


def bench_apu(ch, n, settle_us):
    """End-to-end APU loop. Script writes a zoneguard-format JSON
    datagram onto /tmp/zoneguard.sock (same protocol that the
    GStreamer zoneguard plugin uses). The server.py listener picks
    it up, dispatches into PipelineManager.trigger_alert(), which
    in led_mode=apu calls apu_gpio.set_channel() -> gpio-sysfs write
    -> AXI. We observe the AXI register directly. This measures the
    entire Linux user-space + kernel path."""
    gpio = reg(GPIO[0])
    mask = 1 << ch
    c = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    c.connect(ZG_SOCK)

    def emit(kind):
        c.send(json.dumps({"ch": ch, "kind": kind,
                            "reason": "bench"}).encode() + b"\n")

    emit("CLEAR"); time.sleep(0.05)  # start clean (server may clamp)

    rise, fall = [], []
    for _ in range(n):
        # RISE: emit ENTER, time until GPIO goes high.
        t0 = time.monotonic_ns(); emit("ENTER")
        h = spin_until(lambda: rd(gpio, GPIO[1]) & mask, 200_000_000)
        if h is not None:
            rise.append(h - t0)

        # FALL: emit CLEAR, time until GPIO goes low.
        t0 = time.monotonic_ns(); emit("CLEAR")
        h = spin_until(lambda: not (rd(gpio, GPIO[1]) & mask), 200_000_000)
        if h is not None:
            fall.append(h - t0)

        busy(settle_us)
    return rise, fall


# ============================================================
# Stats + presentation
# ============================================================

def stats(ds):
    if not ds:
        return None
    d = sorted(ds); n = len(d)
    q = lambda p: d[min(int(n * p), n - 1)] / 1e3
    return {"n": n, "mean": sum(d) / n / 1e3,
            "p50": q(.5), "p95": q(.95), "p99": q(.99),
            "max": d[-1] / 1e3,
            "stdev": statistics.stdev(d) / 1e3 if n > 1 else 0.0}


def fmt(label, s):
    if s is None:
        return f"  {label:<14s}  (no samples — see prerequisites)"
    return (f"  {label:<14s}  n={s['n']:<4d}  mean={s['mean']:8.2f} µs  "
            f"p50={s['p50']:7.2f}  p95={s['p95']:7.2f}  "
            f"p99={s['p99']:7.2f}  max={s['max']:8.2f}  "
            f"stdev={s['stdev']:6.2f}")


# ============================================================
# Entry
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="APU + RPU LED-path latency benchmark.")
    ap.add_argument("--ch", type=int, default=0, choices=range(4),
                    help="channel bit to exercise (0..3)")
    ap.add_argument("-n", "--n", type=int, default=300,
                    help="events per phase")
    ap.add_argument("--settle", type=int, default=500,
                    help="busy-wait between events (µs)")
    ap.add_argument("--skip-apu", action="store_true")
    ap.add_argument("--skip-rpu", action="store_true")
    ap.add_argument("--out", default="/home/petalinux/latency_results.json")
    args = ap.parse_args()

    # Hard prereqs — fail fast with clear reason.
    if os.geteuid() != 0:
        sys.exit("ERROR: run with sudo (needs /dev/mem and remoteproc)")
    if not os.path.exists(ZG_SOCK):
        sys.exit(f"ERROR: {ZG_SOCK} missing — start server.py first")

    print(f"measure_latency.py  ch={args.ch}  n={args.n}  "
          f"settle={args.settle}µs  {time.strftime('%Y-%m-%d %H:%M:%S')}",
          flush=True)
    print("=" * 72, flush=True)

    opener = server_login()
    out = {"meta": {"ts": time.strftime('%Y-%m-%d %H:%M:%S'),
                    "ch": args.ch, "n": args.n,
                    "settle_us": args.settle}}

    try:
        # --------- APU phase ---------
        if not args.skip_apu:
            print("\n[APU] stopping R5, setting led_mode=apu ...", flush=True)
            server_mode(opener, "apu")
            rproc_set("stop")
            print(f"      remoteproc state = {rproc_state()}", flush=True)
            rise, fall = bench_apu(args.ch, args.n, args.settle)
            out["apu_rise"] = stats(rise); out["apu_fall"] = stats(fall)
            print("APU-e2e (UDS -> server -> sysfs -> axi_gpio_0):")
            print(fmt("rise 0->1", out["apu_rise"]))
            print(fmt("fall 1->0", out["apu_fall"]), flush=True)

        # --------- RPU phase ---------
        if not args.skip_rpu:
            print("\n[RPU] starting R5, setting led_mode=rpu ...", flush=True)
            rproc_set("start")
            server_mode(opener, "rpu")
            print(f"      remoteproc state = {rproc_state()}", flush=True)
            # Confirm mirror is actually running before benchmarking.
            tcm_probe, gpio_probe = reg(TCM[0]), reg(GPIO[0])
            if not wait_r5_mirror(tcm_probe, gpio_probe, args.ch):
                print("      WARN: R5 mirror did not respond within 2s — "
                      "benchmarking anyway, rise numbers may be invalid")
            else:
                print("      R5 mirror confirmed alive", flush=True)
            rise, fall = bench_rpu(args.ch, args.n, args.settle)
            out["rpu_rise"] = stats(rise); out["rpu_fall"] = stats(fall)
            print("RPU-hw (TCM -> R5 mirror -> axi_gpio_0):")
            print(fmt("rise 0->1", out["rpu_rise"]))
            print(fmt("fall 1->0", out["rpu_fall"]), flush=True)

    finally:
        # Restore default operating state regardless of outcome.
        try:
            rproc_set("start")
            server_mode(opener, "rpu")
        except Exception:
            pass

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
