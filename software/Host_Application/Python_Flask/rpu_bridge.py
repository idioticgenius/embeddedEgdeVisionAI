"""
rpu_bridge.py — APU-side bridge to the R5 (RPU) over OpenAMP rpmsg.

Design:
  * /dev/rpmsg0 is the char-dev endpoint the APU reads/writes.
  * The endpoint is normally created by running /usr/bin/echo_test once after
    R5 boot (it modprobes rpmsg_char, binds the channel, and opens the ept).
    Once created, the endpoint persists until remoteproc stops, so this module
    just opens /dev/rpmsg0 at startup.
  * If /dev/rpmsg0 is missing we try to recreate it via RPMSG_CREATE_EPT_IOCTL
    on /dev/rpmsg_ctrl1; if that also fails the bridge degrades to disabled
    (alerts still work, just no RPU-confirmed badge).

Protocol (ASCII, newline-terminated, ≤128 B):
  APU → RPU:  "ALERT CH=<n> KIND=<ENTER|CLEAR> REASON=<text>\\n"
  RPU → APU:  echo firmware returns the same payload byte-for-byte.
              Future custom firmware may return "ACK ..." — parser accepts
              any response that contains "CH=<n>" as a positive ack for CH n.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import threading
import time
from collections import deque

RPMSG_DEV       = "/dev/rpmsg0"
# The control-dev index isn't fixed: it depends on how many virtio_rpmsg
# buses the kernel has enumerated. On this kernel build with a single R5
# split-mode remoteproc the node is /dev/rpmsg_ctrl0; older setups saw
# ctrl1. _open_endpoint() tries them in order.
RPMSG_CTRL_DEVS = ("/dev/rpmsg_ctrl0", "/dev/rpmsg_ctrl1")
RPMSG_CTRL_DEV  = RPMSG_CTRL_DEVS[0]   # kept for back-compat log strings
CHANNEL_NAME    = "rpmsg-openamp-demo-channel"

# struct rpmsg_endpoint_info { char name[32]; __u32 src; __u32 dst; };
class _RpmsgEndpointInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * 32),
                ("src",  ctypes.c_uint32),
                ("dst",  ctypes.c_uint32)]

# _IOW(0xb5, 0x1, struct rpmsg_endpoint_info)  — size = 40
#   dir=1 (_IOC_WRITE), type=0xb5, nr=0x1, size=40
#   ioctl nr = (1 << 30) | (40 << 16) | (0xb5 << 8) | 0x01 = 0x4028b501
RPMSG_CREATE_EPT_IOCTL = 0x4028B501


class RpuBridge:
    """Owns /dev/rpmsg0. Sends zone-alert triggers, matches echoes, and notifies
    a callback when the RPU has confirmed a trigger.

    Not a singleton by design — server.py holds one instance. If open() fails
    the bridge is disabled and send_trigger() is a no-op.
    """

    def __init__(self, on_confirm=None, logger=print):
        self._fd          = None
        self._log         = logger
        self._on_confirm  = on_confirm or (lambda ch, kind, rtt_ms: None)
        self._pending     = deque(maxlen=32)   # [(ch, kind, send_ts)]
        self._pending_lk  = threading.Lock()
        self._reader      = None
        self._stop        = threading.Event()
        self.enabled      = False
        self.last_error   = None
        # Stats tracked for the board-stats view + APU↔RPU link visualization.
        self._stats_lk    = threading.Lock()
        self._tx_count    = 0
        self._rx_count    = 0
        self._last_tx_ts  = 0.0
        self._last_rx_ts  = 0.0
        self._last_rtt_ms = None
        self._rtt_window  = deque(maxlen=20)
        self._started_ts  = 0.0
        # Recent events for the wire-animation. Each entry:
        #   {"dir":"tx"|"rx", "ch":int, "kind":str, "ts":float,
        #    "rtt_ms":float|None, "seq":int}
        self._events      = deque(maxlen=40)
        self._event_seq   = 0
        # Heartbeat: channels with an active alert state get a synthetic HB
        # every _hb_interval seconds so the RPU link shows proportional load
        # while alerts are sustained (not just at ENTER/CLEAR edges).
        self._active_channels = set()
        self._hb_interval     = 0.5
        self._hb_thread       = None
        # Reconnect state: when /dev/rpmsg0 is torn down (R5 restart)
        # the bridge disables itself and a single background thread
        # polls for the endpoint to come back.
        self._reconnect_thread = None
        self._reconnect_lk     = threading.Lock()
        self._write_err_logged = False

    # ---- setup ----

    def start(self):
        try:
            self._fd = self._open_endpoint()
        except OSError as e:
            self.last_error = f"open {RPMSG_DEV}: {e}"
            self._log(f"[RPU] bridge disabled: {self.last_error}")
            return False
        self.enabled = True
        self._started_ts = time.time()
        self._reader = threading.Thread(target=self._reader_loop,
                                        name="rpu-bridge-rx", daemon=True)
        self._reader.start()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop,
                                           name="rpu-bridge-hb", daemon=True)
        self._hb_thread.start()
        self._log(f"[RPU] bridge up on {RPMSG_DEV}")
        return True

    def stop(self):
        self._stop.set()
        try:
            if self._fd is not None:
                os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        self.enabled = False

    def _open_endpoint(self):
        if os.path.exists(RPMSG_DEV):
            return os.open(RPMSG_DEV, os.O_RDWR)
        # Fallback: try to re-create the endpoint if someone destroyed it.
        ctrl_path = next((p for p in RPMSG_CTRL_DEVS if os.path.exists(p)), None)
        if ctrl_path is None:
            raise OSError(f"none of {RPMSG_CTRL_DEVS} present — R5 not running "
                          "or rpmsg_chrdev not bound")
        ctl = os.open(ctrl_path, os.O_RDWR)
        try:
            info = _RpmsgEndpointInfo()
            info.name = CHANNEL_NAME.encode("ascii")
            info.src = 0
            info.dst = 0x400    # RPU-side addr seen in dmesg: "addr 0x400"
            fcntl.ioctl(ctl, RPMSG_CREATE_EPT_IOCTL, info)
        finally:
            os.close(ctl)
        # kernel should create /dev/rpmsg0 after the ioctl
        for _ in range(50):
            if os.path.exists(RPMSG_DEV):
                break
            time.sleep(0.02)
        return os.open(RPMSG_DEV, os.O_RDWR)

    # ---- public API ----

    def send_trigger(self, ch: int, kind: str, reason: str = ""):
        """Fire-and-forget. If bridge is disabled or write fails, log and move on."""
        if not self.enabled or self._fd is None:
            return
        reason = (reason or "")[:80]
        payload = f"ALERT CH={ch} KIND={kind} REASON={reason}\n".encode("ascii", "replace")
        now = time.time()
        try:
            with self._pending_lk:
                self._pending.append((ch, kind, now))
            os.write(self._fd, payload)
        except OSError as e:
            # /dev/rpmsg0 went away (R5 stopped / restarted). Log *once* —
            # without this latch the server log fills with hundreds of
            # "Broken pipe" lines per second while every alert re-tries.
            # Mark the bridge disabled and start a reconnect thread that
            # polls for the endpoint to come back.
            self._handle_write_error(e)
            return
        with self._stats_lk:
            self._tx_count += 1
            self._last_tx_ts = now
            self._event_seq += 1
            self._events.append({
                "seq":    self._event_seq,
                "dir":    "tx",
                "ch":     ch,
                "kind":   kind,
                "ts":     now,
                "rtt_ms": None,
            })

    # ---- internal ----

    def _reader_loop(self):
        buf = b""
        # EPIPE means the other side of /dev/rpmsg0 isn't draining (R5 has
        # no platform_poll in the shm-only firmware). We want to tell the
        # user once, then stop spamming the log.
        epipe_once = False
        import errno as _errno
        while not self._stop.is_set():
            try:
                chunk = os.read(self._fd, 256)
            except OSError as e:
                if self._stop.is_set():
                    return
                if e.errno == _errno.EPIPE:
                    if not epipe_once:
                        self._log("[RPU] /dev/rpmsg0 EPIPE — R5 isn't "
                                  "draining messages. Bridge going idle "
                                  "(shm fast path is primary; this is "
                                  "expected with the no-platform_poll "
                                  "firmware).")
                        epipe_once = True
                    # Sleep longer on EPIPE — nothing useful will come.
                    time.sleep(2.0)
                    continue
                self._log(f"[RPU] read error: {e}")
                time.sleep(0.2)
                continue
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle_line(line.decode("ascii", "replace").strip())

    def _handle_line(self, line: str):
        if not line:
            return
        # Match the first pending entry whose ch= matches (FIFO per channel).
        ch = _parse_ch(line)
        if ch is None:
            self._log(f"[RPU] unparseable ack: {line!r}")
            return
        kind = _parse_kind(line)
        with self._pending_lk:
            match = None
            for i, (pch, pkind, ts) in enumerate(self._pending):
                if pch == ch and (kind is None or pkind == kind):
                    match = (i, ts, pkind)
                    break
            if match:
                idx, ts, pkind = match
                # consume that entry
                del self._pending[idx]
            else:
                ts = None
                pkind = kind
        if ts is None:
            self._log(f"[RPU] ack without pending: {line!r}")
            return
        now = time.time()
        rtt_ms = (now - ts) * 1000.0
        with self._stats_lk:
            self._rx_count += 1
            self._last_rx_ts = now
            self._last_rtt_ms = rtt_ms
            self._rtt_window.append(rtt_ms)
            self._event_seq += 1
            self._events.append({
                "seq":    self._event_seq,
                "dir":    "rx",
                "ch":     ch,
                "kind":   pkind,
                "ts":     now,
                "rtt_ms": round(rtt_ms, 2),
            })
        # Heartbeats are bookkeeping only — don't surface them as user-visible
        # alert confirmations. They already counted toward tx/rx stats above.
        if pkind == "HB":
            return
        try:
            self._on_confirm(ch, pkind, rtt_ms)
        except Exception as e:
            self._log(f"[RPU] on_confirm raised: {e}")

    def set_channel_active(self, ch: int, active: bool):
        """Called by server when an alert on `ch` starts/ends. Drives heartbeats."""
        with self._stats_lk:
            if active:
                self._active_channels.add(ch)
            else:
                self._active_channels.discard(ch)

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            time.sleep(self._hb_interval)
            if not self.enabled:
                continue
            with self._stats_lk:
                chans = list(self._active_channels)
            for ch in chans:
                self.send_trigger(ch, "HB", "hb")

    def _handle_write_error(self, err: OSError):
        """Called from send_trigger when os.write fails (typically EPIPE
        after R5 stop/restart tears down the rpmsg channel). Disables
        the bridge, logs the first occurrence only, and arms a
        background reconnector. Idempotent."""
        with self._reconnect_lk:
            if not self._write_err_logged:
                self._log(f"[RPU] write failed: {err}; bridge going "
                          "disabled, will auto-reconnect when "
                          f"{RPMSG_DEV} returns")
                self._write_err_logged = True
            self.enabled = False
            self.last_error = f"write: {err}"
            try:
                if self._fd is not None:
                    os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            # Arm the reconnect thread if it isn't already running.
            if self._reconnect_thread is None or not self._reconnect_thread.is_alive():
                self._reconnect_thread = threading.Thread(
                    target=self._reconnect_loop,
                    name="rpu-bridge-reconnect", daemon=True)
                self._reconnect_thread.start()

    def _reconnect_loop(self):
        """Polls every 2 s for /dev/rpmsg0 to reappear, then reopens
        the endpoint and re-enables send_trigger. One reader thread +
        one heartbeat thread are restarted on success."""
        while not self._stop.is_set():
            time.sleep(2.0)
            # Drop out cleanly if the bridge was re-enabled some other way.
            if self.enabled:
                return
            try:
                fd = self._open_endpoint()
            except OSError:
                continue
            # Restart loops with a fresh state.
            self._fd = fd
            self.enabled = True
            self.last_error = None
            self._write_err_logged = False
            self._reader = threading.Thread(target=self._reader_loop,
                                            name="rpu-bridge-rx", daemon=True)
            self._reader.start()
            # _heartbeat_loop thread from start() is still alive (it
            # skips iterations while enabled=False); no need to re-spawn.
            self._log(f"[RPU] bridge reconnected on {RPMSG_DEV}")
            return

    def get_stats(self, since_seq: int = 0):
        """Return a snapshot of link stats for /api/stats.

        since_seq: client passes the highest seq it's already seen so we
        only ship new events (keeps payload small for the wire animation).
        If 0, return all buffered events.
        """
        with self._stats_lk:
            avg = (sum(self._rtt_window) / len(self._rtt_window)
                   if self._rtt_window else None)
            if since_seq <= 0:
                events = list(self._events)
            else:
                events = [e for e in self._events if e["seq"] > since_seq]
            with self._pending_lk:
                pending = len(self._pending)
            # Synthetic per-core utilization. The echo firmware is bare-metal
            # and does not self-report, so we approximate from recent echo
            # activity: each rx ≈ a small busy slice. Window = 5s.
            now_ts = time.time()
            win = 5.0
            rx_in_win = sum(1 for e in self._events
                            if e["dir"] == "rx" and (now_ts - e["ts"]) <= win)
            # 50 msg / 5s  ≈ 100% (rough proxy for demo; real firmware cost is
            # much lower, but this scales sensibly with load).
            r5_0_util = min(100.0, rx_in_win * 2.0) if self.enabled else 0.0
            # R5F_0 state reflects the kernel remoteproc state — the
            # ground truth for R5 firmware liveness. self.enabled only
            # tracks the rpmsg slow-path control link, which may be in
            # a reconnect loop while the TCM fast-path data plane keeps
            # working. Reporting "offline" based on rpmsg state alone
            # would mislead the operator dashboard.
            try:
                with open("/sys/class/remoteproc/remoteproc0/state") as _rf:
                    _r5_state = _rf.read().strip() or "unknown"
            except Exception:
                _r5_state = "unknown"
            cores = [
                {"name": "R5F_0", "state": _r5_state,
                 "util_pct": round(r5_0_util, 1), "freq_mhz": 533},
                {"name": "R5F_1", "state": "offline", "util_pct": 0.0, "freq_mhz": None},
            ]
            return {
                "enabled":       self.enabled,
                "device":        RPMSG_DEV,
                "last_error":    self.last_error,
                "tx_count":      self._tx_count,
                "rx_count":      self._rx_count,
                "pending":       pending,
                "last_rtt_ms":   round(self._last_rtt_ms, 2) if self._last_rtt_ms is not None else None,
                "avg_rtt_ms":    round(avg, 2) if avg is not None else None,
                "last_tx_ts":    self._last_tx_ts or None,
                "last_rx_ts":    self._last_rx_ts or None,
                "last_seq":      self._event_seq,
                "uptime_s":      (time.time() - self._started_ts) if self._started_ts else 0,
                "events":        events,
                "cores":         cores,
            }


def _parse_ch(line: str):
    # Look for "CH=<int>" token.
    for tok in line.split():
        if tok.startswith("CH="):
            try:
                return int(tok[3:])
            except ValueError:
                return None
    return None


def _parse_kind(line: str):
    for tok in line.split():
        if tok.startswith("KIND="):
            return tok[5:]
    return None
