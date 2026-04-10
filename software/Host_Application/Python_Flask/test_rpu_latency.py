#!/usr/bin/env python3
"""
Latency & determinism test for the KV260 zone-alert LED paths.

Measures and compares the APU sysfs write path against the RPU rpmsg+MMIO
path, both idle and under CPU load.

APU path timing: raw write to /sys/class/gpio/gpio504/value, measured
in-process with time.perf_counter_ns(). This is the same write server.py
does in led_mode="apu"; timing it outside the REST layer isolates the
actual sysfs+driver+AXI cost from Flask/socket overhead.

RPU path timing: relies on the rpmsg_bridge echo-RTT that server.py
already exposes. The bridge pumps a heartbeat every 0.5 s whenever a
channel is active, so after triggering ch=0 ENTER the /api/stats rpu
events ring fills with real RX entries whose rtt_ms is measured at the
kernel-write-to-echo-read boundary. We poll stats at 10 Hz and de-dupe
by seq to collect unique samples over a window.

The two measurements are NOT apples-to-apples for median latency (APU
is one-way write, RPU is round-trip), but the distribution shape tells
you what you need — specifically p99/max/max-p50 spread under load.
"""

import argparse, json, mmap, os, statistics, struct, subprocess, sys, time, urllib.request

SERVER = "http://localhost:5000"
GPIO_VAL = "/sys/class/gpio/gpio504/value"

# Shared-memory fast-path page (matches shm_alert.h).
SHM_PA    = 0xFFE20000
SHM_SIZE  = 0x1000
SHM_MAGIC = 0x5A4C4544  # "ZLED"


def http_get(path):
    with urllib.request.urlopen(SERVER + path, timeout=2) as r:
        return json.loads(r.read())

def http_post(path, body):
    req = urllib.request.Request(
        SERVER + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read())


def cpu_load_start(n):
    """Spawn n `yes > /dev/null` processes to saturate CPU."""
    procs = [subprocess.Popen(["yes"], stdout=subprocess.DEVNULL)
             for _ in range(n)]
    # Let the scheduler settle before timing anything.
    time.sleep(1.0)
    return procs

def cpu_load_stop(procs):
    for p in procs:
        p.kill()
    for p in procs:
        p.wait()


def bench_apu(n):
    """Time n raw writes to the gpio sysfs value node."""
    # Ensure APU mode so rpu_bridge isn't also racing on the pin
    http_post("/api/led/mode", {"mode": "apu"})
    # Open once, keep fd hot — same as the driver fast-path
    fd = os.open(GPIO_VAL, os.O_WRONLY)
    try:
        samples = []
        for i in range(n):
            v = b"1" if (i & 1) else b"0"
            t0 = time.perf_counter_ns()
            os.write(fd, v)
            t1 = time.perf_counter_ns()
            samples.append((t1 - t0) / 1000.0)   # µs
    finally:
        os.close(fd)
    # Park pin low
    os.system("echo 0 | sudo tee /sys/class/gpio/gpio504/value > /dev/null")
    return samples


def bench_rpu_shm(n):
    """Measure the APU-shm → R5-poll → GPIO write path end-to-end.

    Approach: mmap the shared flag page read/write, and for each iter
    toggle bit 0 and sample /sys/class/gpio/gpio504/value until it
    reflects the new value — timing the delta. This gives the real
    APU-write-to-LED latency as seen from userspace, with the R5
    poll interval included. The sysfs read is the noise floor.
    """
    # Stamp magic if not already set — first writer takes ownership.
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        m = mmap.mmap(fd, SHM_SIZE, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=SHM_PA)
    finally:
        os.close(fd)

    magic = struct.unpack_from("<I", m, 0)[0]
    if magic != SHM_MAGIC:
        # Initialise magic so R5 starts honouring the page. Flags=0.
        struct.pack_into("<IIII", m, 0, SHM_MAGIC, 0, 0, 0)

    gpio_fd = os.open(GPIO_VAL, os.O_RDONLY)
    samples = []
    try:
        expect = 0
        for _ in range(n):
            expect ^= 1
            t0 = time.perf_counter_ns()
            # Set bit 0 of flags to `expect`. Writes 4 bytes at offset 4.
            struct.pack_into("<I", m, 4, expect & 0x1)
            # Bump seq (offset 8) so R5 has a liveness counter.
            seq = struct.unpack_from("<I", m, 8)[0] + 1
            struct.pack_into("<I", m, 8, seq)
            # Poll gpio value until it matches.
            while True:
                os.lseek(gpio_fd, 0, os.SEEK_SET)
                v = os.read(gpio_fd, 2).strip()
                if v == (b"1" if expect else b"0"):
                    break
            t1 = time.perf_counter_ns()
            samples.append((t1 - t0) / 1000.0)  # µs
    finally:
        os.close(gpio_fd)
        # Park bit 0 low
        struct.pack_into("<I", m, 4, 0)
        m.close()
    return samples


def bench_rpu(duration_s):
    """Trigger ch=0 ENTER so rpu_bridge.hb_thread pumps frames at 2 Hz,
    poll /api/stats at 10 Hz, de-dupe by seq, harvest rtt_ms from RX
    events for `duration_s` seconds."""
    http_post("/api/led/mode", {"mode": "rpu"})
    http_post("/api/events/trigger", {"ch": 0, "reason": "bench"})
    seen_seq = set()
    samples = []
    t_end = time.time() + duration_s
    while time.time() < t_end:
        try:
            ev = http_get("/api/stats")["rpu"]["events"]
        except Exception:
            time.sleep(0.1)
            continue
        for e in ev:
            if e["dir"] == "rx" and e["seq"] not in seen_seq \
                    and e["rtt_ms"] is not None:
                seen_seq.add(e["seq"])
                samples.append(float(e["rtt_ms"]) * 1000.0)   # ms→µs
        time.sleep(0.1)
    http_post("/api/events/clear", {"ch": 0, "reason": "bench-done"})
    return samples


def summarise(label, samples):
    if not samples:
        return f"{label:25s}  (no samples)"
    s = sorted(samples)
    p50 = s[len(s)//2]
    p99 = s[min(len(s) - 1, int(len(s) * 0.99))]
    p999 = s[min(len(s) - 1, int(len(s) * 0.999))]
    jitter = max(s) - p50
    return (f"{label:25s}  n={len(s):>5d}  "
            f"p50={p50:7.2f}µs  p99={p99:8.2f}µs  "
            f"p999={p999:9.2f}µs  max={max(s):9.2f}µs  "
            f"max-p50={jitter:8.2f}µs")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apu-n",  type=int, default=1000)
    p.add_argument("--rpu-s",  type=float, default=20.0,
                   help="seconds to collect RPU (rpmsg) samples per phase")
    p.add_argument("--shm-n",  type=int, default=500,
                   help="shmem fast-path iterations per phase (0 = skip)")
    p.add_argument("--load-cpus", type=int, default=4)
    args = p.parse_args()

    print("=" * 78)
    print("KV260 APU-vs-RPU zone-alert LED latency / determinism test")
    print(f"  time={time.strftime('%Y-%m-%d %H:%M:%S')}   "
          f"apu-n={args.apu_n}   rpu-s={args.rpu_s}   "
          f"load-cpus={args.load_cpus}")
    print("=" * 78)

    results = []

    print("\n[1/4] APU path, idle")
    r = bench_apu(args.apu_n);   results.append(summarise("APU  idle", r))
    print("      " + results[-1])

    print(f"\n[2/4] RPU path, idle  ({args.rpu_s:.0f}s)")
    r = bench_rpu(args.rpu_s);   results.append(summarise("RPU  idle", r))
    print("      " + results[-1])

    print(f"\n[3/4] APU path, under CPU load ({args.load_cpus}×yes)")
    load = cpu_load_start(args.load_cpus)
    try:
        r = bench_apu(args.apu_n); results.append(summarise("APU  under load", r))
    finally:
        cpu_load_stop(load)
    print("      " + results[-1])

    print(f"\n[4/4] RPU path, under CPU load ({args.load_cpus}×yes, {args.rpu_s:.0f}s)")
    load = cpu_load_start(args.load_cpus)
    try:
        r = bench_rpu(args.rpu_s); results.append(summarise("RPU  under load", r))
    finally:
        cpu_load_stop(load)
    print("      " + results[-1])

    if args.shm_n > 0:
        print(f"\n[5/6] RPU-shm path, idle  (n={args.shm_n})")
        try:
            r = bench_rpu_shm(args.shm_n)
            results.append(summarise("RPU-shm  idle", r))
            print("      " + results[-1])
        except Exception as e:
            print(f"      SKIPPED: {e}")

        print(f"\n[6/6] RPU-shm path, under CPU load ({args.load_cpus}×yes)")
        load = cpu_load_start(args.load_cpus)
        try:
            r = bench_rpu_shm(args.shm_n)
            results.append(summarise("RPU-shm  under load", r))
        except Exception as e:
            print(f"      SKIPPED: {e}")
        finally:
            cpu_load_stop(load)
        if results:
            print("      " + results[-1])

    print("\n" + "=" * 78)
    print("Summary")
    print("=" * 78)
    for line in results:
        print(" " + line)
    print("=" * 78)
    print("Interpretation:")
    print(" - Compare the max-p50 column (jitter budget). RPU's value")
    print("   should stay roughly flat between idle and under-load rows;")
    print("   APU's should grow — that's the determinism proof.")
    print(" - RPU median (~µs) includes an rpmsg round-trip; APU median")
    print("   is a single sysfs write. They are not apples-to-apples for")
    print("   median latency, only for jitter / tail behaviour.")


if __name__ == "__main__":
    sys.exit(main())
