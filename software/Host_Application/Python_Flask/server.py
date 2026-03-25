#!/usr/bin/env python3
"""
server_combined.py — Fallback variant that runs all channels in ONE combined
gst-launch subprocess. Detection toggle restarts the entire pipeline (~1s gap
on ALL channels), but this variant is stable because XRT's exclusive DPU lock
is held by a single process.

Usage:
    sudo python3 server_combined.py [--port 5000]
"""

import argparse
import faulthandler
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

faulthandler.enable(file=sys.stderr, all_threads=True)
from collections import deque
from flask import Flask, jsonify, request, send_from_directory, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash

from rpu_bridge import RpuBridge

ZONEGUARD_SOCKET = "/tmp/zoneguard.sock"
ZONEGUARD_ZONES_FMT = "/tmp/zoneguard_ch{ch}.json"

UI_USER = os.environ.get("UI_USER", "admin")
UI_PASS_HASH = os.environ.get(
    "UI_PASS_HASH",
    generate_password_hash(os.environ.get("UI_PASS", "admin"),
                           method="pbkdf2:sha256")
)

SECRET_PATH = "/home/petalinux/.flask_secret"

def _load_or_create_secret():
    try:
        with open(SECRET_PATH, "rb") as f:
            s = f.read()
            if len(s) >= 32:
                return s
    except Exception:
        pass
    s = os.urandom(48)
    try:
        with open(SECRET_PATH, "wb") as f:
            f.write(s)
        os.chmod(SECRET_PATH, 0o600)
    except Exception:
        pass
    return s

RPROC_STATE_PATH = "/sys/class/remoteproc/remoteproc0/state"


def ensure_rpu_running(timeout_s=4.0):
    """Ensure the R5 remoteproc is booted so the RPU LED path has a sink.

    Returns (ok, message). Safe to call repeatedly — if the R5 is already
    running this is a single sysfs read. If it's offline we write 'start'
    (requires root; server.py already runs under sudo) and wait for the
    state file to flip to 'running'. Nothing here touches TCM — the
    remoteproc driver brings the RPU island up before it returns 'running'
    so subsequent TCM access is safe."""
    try:
        with open(RPROC_STATE_PATH) as f:
            state = f.read().strip()
    except Exception as e:
        return False, f"rproc read failed: {e}"
    if state.startswith("running"):
        return True, "already running"
    if state != "offline":
        return False, f"unexpected state {state!r}"
    try:
        with open(RPROC_STATE_PATH, "w") as f:
            f.write("start")
    except Exception as e:
        return False, f"rproc start failed: {e}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with open(RPROC_STATE_PATH) as f:
                if f.read().strip().startswith("running"):
                    return True, "started"
        except Exception:
            pass
        time.sleep(0.1)
    return False, "start timed out"

# APU-driven LEDs on axi_gpio_0 lines 504-507 (gpiochip504 base=504):
#   bit 0 / line 504 → PMOD J2 pin 1  / K26 H12  (top row)
#   bit 1 / line 505 → PMOD J2 pin 7  / K26 B10  (bottom row)
#   bit 2 / line 506 → PMOD J2 pin 8  / K26 E12  (bottom row)
#   bit 3 / line 507 → PMOD J2 pin 9  / K26 D11  (bottom row)
# Each channel (0..3) drives its own LED via its corresponding bit.
# No R5 involvement — APU writes per-bit sysfs value files.
APU_GPIO_LINES = [504, 505, 506, 507]


class ApuGpioBank:
    """Per-channel control of a contiguous bank of sysfs GPIO lines."""
    def __init__(self, lines):
        self.lines = list(lines)
        self.paths = {ch: f"/sys/class/gpio/gpio{n}" for ch, n in enumerate(self.lines)}
        self.ok = {ch: False for ch in self.paths}
        for ch, num in enumerate(self.lines):
            try:
                p = self.paths[ch]
                if not os.path.isdir(p):
                    with open("/sys/class/gpio/export", "w") as f:
                        f.write(str(num))
                with open(f"{p}/direction", "w") as f:
                    f.write("out")
                with open(f"{p}/value", "w") as f:
                    f.write("0")
                self.ok[ch] = True
                print(f"[INFO] APU GPIO ch{ch} line {num} ready (output, low)")
            except Exception as e:
                print(f"[WARN] APU GPIO ch{ch} line {num} init failed: {e}")

    def set_channel(self, ch, on):
        if ch not in self.ok or not self.ok[ch]:
            return
        try:
            with open(f"{self.paths[ch]}/value", "w") as f:
                f.write("1" if on else "0")
        except Exception as e:
            print(f"[WARN] APU GPIO ch{ch} set({on}) failed: {e}")

    def all_off(self):
        for ch in self.ok:
            self.set_channel(ch, False)


apu_gpio = ApuGpioBank(APU_GPIO_LINES)

# Board statistics via xlnx_platformstats (die temps, voltages, power, CPU)
try:
    from xlnx_platformstats import xlnx_platformstats as _stats
    _stats.init()
    STATS_AVAILABLE = True
except Exception as _e:
    _stats = None
    STATS_AVAILABLE = False
    print(f"[WARN] xlnx_platformstats unavailable: {_e}")

# Labels come straight from the kria-dashboard source so the UI matches it.
_VOLT_LABELS = [
    "VCC_PSPLL", "PL_VCCINT", "VOLT_DDRS", "VCC_PSINTFP",
    "VCC_PS_FPD", "PS_IO_BANK_500", "VCC_PS_GTR", "VTT_PS_GTR", "total"
]
_TEMP_LABELS = ["LPD", "FPD", "PL"]

DEFAULT_PORT      = 5000
DEFAULT_VIDEO_DIR = "/home/petalinux/test_videos"
DEFAULT_JSON_DIR  = "/home/petalinux/jsons"
DEFAULT_VIDEO     = "/home/petalinux/test_videos/walking.mp4"
MAX_LOG_LINES     = 300

DISPLAY_W = 1920
DISPLAY_H = 1080

XCLBIN_LOCATION = "/lib/firmware/xilinx/multichannel-openamp-gpio/dpu.xclbin"
KERNEL_NAME = "image_processing:{image_processing_1}"

app = Flask(__name__, static_folder=".")
app.secret_key = _load_or_create_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

_PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico"}

@app.before_request
def _require_login():
    if session.get("logged_in"):
        return None
    p = request.path
    if p in _PUBLIC_PATHS:
        return None
    if p.startswith("/api/"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        if u == UI_USER and check_password_hash(UI_PASS_HASH, p):
            session["logged_in"] = True
            session["user"] = u
            return redirect("/")
        return redirect("/login?err=1")
    return send_from_directory(".", "login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

MODELS = {
    "refinedet": {
        "label": "Person Detect (RefineDet)",
        "infer_json": "kernel_refinedet.json",
        "meta_json": "metaconvert_config.json",
        "input_width": 480, "input_height": 360, "input_format": "BGR",
    },
    "densebox": {
        "label": "Face Detect (DenseBox)",
        "infer_json": "kernel_densebox.json",
        "meta_json": "metaconvert_facedetect.json",
        "input_width": 640, "input_height": 360, "input_format": "BGR",
    },
    "ssd_mobilenet": {
        "label": "Object Detect (SSD MobileNet)",
        "infer_json": "kernel_ssd_mobilenet.json",
        "meta_json": "metaconvert_ssd_person.json",
        "input_width": 300, "input_height": 300, "input_format": "RGB",
    },
}

CHANNEL_FIXED = [
    {"tee": "t0", "ima": "ima0", "disp": "disp0", "plane": 34},
    {"tee": "t1", "ima": "ima1", "disp": "disp1", "plane": 35},
    {"tee": "t2", "ima": "ima2", "disp": "disp2", "plane": 36},
    {"tee": "t3", "ima": "ima3", "disp": "disp3", "plane": 37},
]

DEFAULT_RECTS = {
    1: ["<0,0,1920,1080>"],
    2: ["<0,0,960,1080>",   "<960,0,960,1080>"],
    3: ["<0,0,960,540>",    "<960,0,960,540>",  "<0,540,960,540>"],
    4: ["<0,0,960,540>",    "<960,0,960,540>",  "<0,540,960,540>", "<960,540,960,540>"],
}


def clamp_rect(x, y, w, h):
    min_w, min_h = 160, 90
    w = max(min_w, min(w, DISPLAY_W))
    h = max(min_h, min(h, DISPLAY_H))
    x = max(0, min(x, DISPLAY_W - w))
    y = max(0, min(y, DISPLAY_H - h))
    return x, y, w, h


def rect_str(x, y, w, h):
    return f"<{x},{y},{w},{h}>"


def _split_webcam_uri(video):
    """Decompose '/dev/videoN?w=1920&h=1080&fps=30' into (path, w, h, fps).
    Returns defaults 1280x720@60 when no query is present."""
    path, _, qs = video.partition("?")
    w, h, fps = 1280, 720, 60
    if qs:
        params = urllib.parse.parse_qs(qs)
        try: w = int(params.get("w", [w])[0])
        except Exception: pass
        try: h = int(params.get("h", [h])[0])
        except Exception: pass
        try: fps = int(float(params.get("fps", [fps])[0]))
        except Exception: pass
    return path, w, h, fps


def _source_element(video, ch, num_ch=1):
    """Return a gst-launch source fragment that ends at NV12 decoded frames.
    The key elements are given channel-unique names so tracer output can be
    attributed to a specific channel. Webcam sources accept an optional
    query-string with w/h/fps (see _split_webcam_uri).

    When num_ch > 1 and the source is a webcam, a `videorate` element is
    inserted after the decoder to cap the live feed at 30 fps. This is
    a partial mitigation for the 4-channel shared-PPE-CU scheduling
    jitter documented in rpu-enablement.md §28 — it doesn't eliminate
    jitter (root cause is the single image_processing CU), but it
    regularises the input cadence to the tee so metaaffixer pairing
    drops are less bursty. Single-channel runs keep the full native
    framerate (no videorate)."""
    if video.startswith("/dev/video"):
        path, w, h, fps = _split_webcam_uri(video)
        rate = ""
        if num_ch > 1:
            rate = "! videorate drop-only=true ! video/x-raw,framerate=30/1 "
        return (
            f'v4l2src device={path} name=src{ch} do-timestamp=true ! '
            f'video/x-h264,stream-format=byte-stream,alignment=au,'
            f'width={w},height={h},framerate={fps}/1 ! '
            f'h264parse name=parse{ch} ! '
            f'omxh264dec name=dec{ch} internal-entropy-buffers=3 '
            f'{rate}'.rstrip()
        )
    # File source — always strip any query string (file sources don't
    # accept parameters). Pipeline plays the clip once; EOS is handled
    # by higher layers (soak script / UI restart).
    path = video.split("?", 1)[0]
    return (
        f'filesrc location={path} name=src{ch} ! qtdemux name=demux{ch} ! '
        f'h264parse name=parse{ch} ! '
        f'omxh264dec name=dec{ch} internal-entropy-buffers=3'
    )


def build_channel_fragment(ch, video, model_key, json_dir, rect, detect=True, num_ch=1):
    p = CHANNEL_FIXED[ch]
    disp = p["disp"]
    plane = p["plane"]
    source = _source_element(video, ch, num_ch)
    if not detect:
        # Single-sink: fpsdisplaysink wraps kmssink so we still see
        # per-channel rendered/dropped stats in the gst-launch -v log
        # without a second tee branch. The tee-with-fakesink pattern
        # stalls the VCU DMA pool at 1080p (rpu-enablement.md §27).
        return (
            f'{source} ! queue ! '
            f'fpsdisplaysink name=fps{ch} text-overlay=false sync=false '
            f'video-sink="kmssink name=ksink{ch} driver-name=xlnx plane-id={plane} '
            f'render-rectangle=\\"{rect}\\" show-preroll-frame=false sync=false can-scale=true"'
        )
    m = MODELS[model_key]
    tee = p["tee"]; ima = p["ima"]
    infer_json = os.path.join(json_dir, m["infer_json"])
    meta_json = os.path.join(json_dir, m["meta_json"])
    inp_w, inp_h, inp_fmt = m["input_width"], m["input_height"], m["input_format"]
    zones_cfg = ZONEGUARD_ZONES_FMT.format(ch=ch)
    return (
        f'{source} ! '
        f'tee name={tee} ! queue ! '
        f'vvas_xabrscaler name=scaler{ch} xclbin-location={XCLBIN_LOCATION} kernel-name="{KERNEL_NAME}" ! '
        f'video/x-raw,width={inp_w},height={inp_h},format={inp_fmt} ! queue ! '
        f'vvas_xinfer name=infer{ch} infer-config={infer_json} ! {ima}.sink_master '
        f'vvas_xmetaaffixer name={ima} {ima}.src_master ! fakesink '
        f'{tee}. ! queue max-size-buffers=1 ! {ima}.sink_slave_0 {ima}.src_slave_0 ! queue ! '
        f'vvas_xmetaconvert name=meta{ch} config-location={meta_json} ! '
        # zoneguard sits AFTER metaconvert so it can append its zone shapes to
        # the GstVvasOverlayMeta that metaconvert produced. It still reads the
        # GstInferenceMeta (still present on the buffer) for hit-testing, and
        # emits events over the Unix socket.
        f'zoneguard name=zg{ch} channel={ch} zones-config={zones_cfg} event-socket={ZONEGUARD_SOCKET} ! '
        f'vvas_xoverlay name=overlay{ch} ! '
        # Single-sink (no tee/fakesink stats branch) — see §27 for why
        # the old tee+fakesink pattern stalled the VCU at 1080p. FPS
        # is still emitted by fpsdisplaysink wrapping kmssink, which
        # the server scrapes via FPS_RE from gst-launch -v stdout.
        f'fpsdisplaysink name=fps{ch} text-overlay=false sync=false '
        f'video-sink="kmssink name=ksink{ch} driver-name=xlnx plane-id={plane} '
        f'render-rectangle=\\"{rect}\\" show-preroll-frame=false sync=false can-scale=true"'
    )


def build_gst_command(videos, models, rects, json_dir, detects=None):
    n = len(videos)
    if detects is None: detects = [True] * n
    parts = [build_channel_fragment(i, videos[i], models[i], json_dir, rects[i], detects[i], num_ch=n) for i in range(n)]
    return "gst-launch-1.0 -v " + " ".join(parts)


def command_pretty(videos, models, rects, json_dir, detects=None):
    return build_gst_command(videos, models, rects, json_dir, detects).replace(" ! ", " \\\n  ! ")


FPS_RE = re.compile(r'GstFPSDisplaySink:fps(\d+).*current:\s*(\d+\.?\d*),\s*average:\s*(\d+\.?\d*)')

# GStreamer `latency` tracer emits lines that look like:
#   TRACE .* latency, ... src-element=(string)src0, ... sink-element=(string)ksink0, ..., time=(guint64)131953879, ts=(guint64)...;
#   TRACE .* element-latency, element-id=..., element=(string)infer2, ..., time=(guint64)25000000, ts=...;
PIPELINE_LAT_RE = re.compile(
    r'latency,\s+.*?src-element=\(string\)(\w+),\s+.*?sink-element=\(string\)(\w+),\s+.*?time=\(guint64\)(\d+)'
)
ELEMENT_LAT_RE = re.compile(
    r'element-latency,\s+.*?element=\(string\)(\w+),\s+.*?time=\(guint64\)(\d+)'
)

# Map element-name prefix → latency stage label we report.
LATENCY_STAGES = [
    ("dec",     "vcu_ms"),
    ("scaler",  "preproc_ms"),   # image_processing kernel (VVAS scaler)
    ("infer",   "infer_ms"),     # DPU forward + postproc
    ("overlay", "overlay_ms"),
    ("meta",    "meta_ms"),
]
STAGE_PREFIXES = {prefix for prefix, _ in LATENCY_STAGES}


class PipelineManager:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.logs = deque(maxlen=MAX_LOG_LINES)
        self.start_time = None
        self.config = {}
        self.command = ""
        self.fps = self._empty_fps(4)
        # Rolling windows of latency samples; one deque per (ch, stage).
        # Key form: (ch, "vcu_ms" | "preproc_ms" | "infer_ms" | "overlay_ms" | "meta_ms" | "e2e_ms")
        self.lat_samples = {}
        self.lat_window = 30  # keep last N samples per stage
        # Zone / line definitions per channel. Each entry is:
        #   {"type": "rect", "x": int, "y": int, "w": int, "h": int, "name": str}
        #   {"type": "line", "x1": int, "y1": int, "x2": int, "y2": int, "name": str}
        # Coordinates are in the channel's render-rectangle space (e.g. 0..960, 0..540).
        self.zones = {0: [], 1: [], 2: [], 3: []}
        # Global-ish switch to draw zones on the HDMI video (read by zoneguard
        # via its per-channel JSON, so we mirror it into every channel file).
        self.draw_overlay = True
        # Per-channel alert state. Set by the event classifier (stub for now).
        # Each value: {"active": bool, "since": epoch_s, "reason": str}
        self.alerts = {i: {"active": False, "since": None, "reason": None,
                           "rpu_confirmed": False, "rpu_rtt_ms": None} for i in range(4)}
        # Rolling log of recent events (zone entries, line crossings, clears).
        self.events = deque(maxlen=100)
        # LED source for ch-0 alert: "apu" (APU writes sysfs gpio 504) or
        # "rpu" (R5 firmware drives the same pin over MMIO via rpmsg trigger).
        # In "rpu" mode the APU explicitly does not touch the gpio, so that
        # the R5 owns the data register.
        self.led_mode = "apu"

    @staticmethod
    def _empty_fps(n):
        return [{"fps": None, "mean_fps": None, "model": None} for _ in range(n)]

    def _record_latency(self, ch, stage, ns):
        ms = ns / 1_000_000.0
        key = (ch, stage)
        dq = self.lat_samples.get(key)
        if dq is None:
            dq = deque(maxlen=self.lat_window)
            self.lat_samples[key] = dq
        dq.append(ms)

    # ---- Zones / events ----

    def _write_zones_file(self, ch):
        """Persist this channel's zones to the JSON file zoneguard watches.

        The UI stores pixel coordinates in the channel's render-rectangle
        space (whatever `ch.rect.w × ch.rect.h` happens to be). zoneguard,
        however, sees detection boxes in the *decoded video frame's* space,
        which can be anything (480×270 for vid_4.mp4, 960×540 for
        walking.mp4, 1280×720 for webcam, …). We bridge the two by
        normalising zones to fractions of their channel's render rect on
        the way out — zoneguard then multiplies by the current caps
        frame size each time it evaluates a zone.
        """
        path = ZONEGUARD_ZONES_FMT.format(ch=ch)
        tmp = path + ".tmp"
        rw = self._render_rect_wh(ch)
        if not rw:
            rw = (1920, 1080)
        rw_w, rw_h = rw
        out = []
        for z in self.zones[ch]:
            if z.get("type") == "rect":
                out.append({
                    "type": "rect", "name": z.get("name", ""),
                    "x": max(0.0, min(1.0, z["x"] / rw_w)),
                    "y": max(0.0, min(1.0, z["y"] / rw_h)),
                    "w": max(0.0, min(1.0, z["w"] / rw_w)),
                    "h": max(0.0, min(1.0, z["h"] / rw_h)),
                })
            elif z.get("type") == "line":
                out.append({
                    "type": "line", "name": z.get("name", ""),
                    "x1": max(0.0, min(1.0, z["x1"] / rw_w)),
                    "y1": max(0.0, min(1.0, z["y1"] / rw_h)),
                    "x2": max(0.0, min(1.0, z["x2"] / rw_w)),
                    "y2": max(0.0, min(1.0, z["y2"] / rw_h)),
                })
        payload = {"draw_overlay": bool(self.draw_overlay), "zones": out}
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except Exception as e:
            self._log(f"[SERVER] zone-file write failed for CH{ch}: {e}")

    def _render_rect_wh(self, ch):
        """Return (w, h) of the render rect currently configured for ch,
        or None if unknown. rect strings live in self.config['rects'] in the
        form '<x,y,w,h>'. Fallback to 1920x1080 if nothing's been set yet."""
        try:
            rect = (self.config.get("rects") or [])[ch]
            m = re.match(r'<(\d+),(\d+),(\d+),(\d+)>', rect)
            if m:
                return int(m.group(3)), int(m.group(4))
        except Exception:
            pass
        return None

    def set_zones(self, ch, zones):
        """Replace the zone list for one channel. `zones` is a list of dicts."""
        cleared = False
        with self.lock:
            self.zones[ch] = list(zones or [])
            # Clearing zones also clears any active alert on that channel.
            if not zones and self.alerts[ch]["active"]:
                self.alerts[ch] = {"active": False, "since": None, "reason": None,
                                   "rpu_confirmed": False, "rpu_rtt_ms": None}
                self._push_event(ch, "ALERT_CLEARED", "zones removed")
                cleared = True
        if cleared and self.led_mode == "apu":
            apu_gpio.set_channel(ch, False)
        # Write outside the lock (disk I/O)
        self._write_zones_file(ch)

    def get_zones(self, ch=None):
        with self.lock:
            if ch is None:
                return {str(k): list(v) for k, v in self.zones.items()}
            return list(self.zones.get(ch, []))

    def trigger_alert(self, ch, reason="manual"):
        """Set alert state for a channel. Called by the event classifier
        (currently stubbed — can be invoked via /api/events/trigger for
        UI testing until metadata-reading is wired up)."""
        fired = False
        with self.lock:
            if not self.alerts[ch]["active"]:
                self.alerts[ch] = {"active": True, "since": time.time(),
                                   "reason": reason,
                                   "rpu_confirmed": False, "rpu_rtt_ms": None}
                self._push_event(ch, "ALERT_RAISED", reason)
                fired = True
        if fired and self.led_mode == "apu":
            apu_gpio.set_channel(ch, True)
        if fired and rpu_bridge is not None:
            rpu_bridge.send_trigger(ch, "ENTER", reason)
            rpu_bridge.set_channel_active(ch, True)

    def clear_alert(self, ch, reason="manual"):
        fired = False
        with self.lock:
            if self.alerts[ch]["active"]:
                self.alerts[ch] = {"active": False, "since": None, "reason": None,
                                   "rpu_confirmed": False, "rpu_rtt_ms": None}
                self._push_event(ch, "ALERT_CLEARED", reason)
                fired = True
        if fired and self.led_mode == "apu":
            apu_gpio.set_channel(ch, False)
        if fired and rpu_bridge is not None:
            rpu_bridge.send_trigger(ch, "CLEAR", reason)
            rpu_bridge.set_channel_active(ch, False)

    def set_led_mode(self, mode):
        """Switch between APU-driven LEDs (sysfs) and RPU-driven LEDs (R5 MMIO).
        Returns (ok, message). On a mode change we reconcile all 4 channel
        pin states so the transition is clean: APU→RPU drops every sysfs bit
        to 0 and hands the bank to the R5; RPU→APU re-applies current
        per-channel alert state."""
        mode = (mode or "").lower()
        if mode not in ("apu", "rpu"):
            return False, f"invalid mode {mode!r}"
        with self.lock:
            prev = self.led_mode
            self.led_mode = mode
            active_map = {ch: self.alerts[ch]["active"] for ch in range(4)}
        if prev == mode:
            if mode == "rpu":
                ok, msg = ensure_rpu_running()
                if not ok:
                    self._log(f"[SERVER] ensure_rpu_running: {msg}")
            return True, f"already {mode}"
        if mode == "rpu":
            ok, msg = ensure_rpu_running()
            self._log(f"[SERVER] ensure_rpu_running: {msg}")
            if not ok:
                with self.lock:
                    self.led_mode = prev
                return False, f"rpu start failed: {msg}"
            apu_gpio.all_off()
        else:  # switching back to APU
            for ch, on in active_map.items():
                apu_gpio.set_channel(ch, bool(on))
        return True, f"led_mode={mode}"

    def mark_rpu_confirmed(self, ch, kind, rtt_ms):
        """Called from the RpuBridge reader thread when an echo/ack returns."""
        with self.lock:
            a = self.alerts.get(ch)
            if not a:
                return
            # Only attach confirmation to a live ENTER alert; CLEAR acks still
            # get logged but don't resurrect state.
            if kind == "ENTER" and a["active"]:
                a["rpu_confirmed"] = True
                a["rpu_rtt_ms"] = round(rtt_ms, 2)
            self._push_event(ch, "RPU_ACK", f"{kind} rtt={rtt_ms:.1f}ms")

    def _push_event(self, ch, kind, detail):
        """Caller must hold self.lock."""
        self.events.append({
            "ts": time.time(),
            "ch": ch,
            "kind": kind,
            "detail": detail,
        })
        # Also emit to the log pane so it shows up in Pipeline Logs.
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] [EVENT] CH{ch} {kind}: {detail}")

    def events_snapshot(self):
        with self.lock:
            alerts = {str(k): dict(v) for k, v in self.alerts.items()}
            events = list(self.events)[-30:]
        return {"alerts": alerts, "events": events}

    def _latency_snapshot(self):
        """Return per-channel latency dict: {ch: {stage: avg_ms, ...}}."""
        out = {}
        for (ch, stage), dq in self.lat_samples.items():
            if not dq:
                continue
            out.setdefault(ch, {})[stage] = round(sum(dq) / len(dq), 2)
        return out

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        for line in msg.splitlines():
            self.logs.append(f"[{ts}] {line}")

    def start(self, videos, models, rects, json_dir, detects=None):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return False, "Pipeline is already running"
            cur_mode = self.led_mode
            n = len(videos)
        if cur_mode == "rpu":
            ok, msg = ensure_rpu_running()
            self._log(f"[SERVER] ensure_rpu_running (start): {msg}")
            if not ok:
                return False, f"rpu start failed: {msg}"
        with self.lock:
            if not 1 <= n <= 4:
                return False, f"Invalid channel count: {n}"
            if detects is None: detects = [True] * n
            for mk, d in zip(models, detects):
                if d and mk not in MODELS:
                    return False, f"Unknown model: {mk}"
            if not os.path.isdir(json_dir):
                return False, f"JSON dir not found: {json_dir}"
            for i, v in enumerate(videos):
                if not v or not v.strip():
                    return False, f"Channel {i} source is empty"
                src = v.strip()
                if src.startswith("/dev/video"):
                    path = src.split("?", 1)[0]
                    if not os.path.exists(path):
                        return False, f"Channel {i} webcam not found: {path}"
                else:
                    fpath = src.split("?", 1)[0]
                    if not os.path.isfile(fpath):
                        return False, f"Channel {i} video not found: {v}"
            cmd = build_gst_command(videos, models, rects, json_dir, detects)
            self.command = command_pretty(videos, models, rects, json_dir, detects)
            self._log("[SERVER] Launching combined VVAS pipeline")
            env = os.environ.copy()
            # Enable built-in GStreamer tracers for FPS + per-element / end-to-end
            # latency. These add a small per-buffer bookkeeping overhead but no
            # allocations, and we already name key elements so the tracer output
            # can be attributed to a specific channel.
            env["GST_TRACERS"] = "latency(flags=pipeline+element)"
            # GST_TRACER:7 = latency samples, zoneguard:3 = our WARN diagnostics.
            env["GST_DEBUG"] = "GST_TRACER:7,zoneguard:3"
            env["GST_DEBUG_NO_COLOR"] = "1"
            try:
                self.proc = subprocess.Popen(
                    cmd, shell=True, executable="/bin/bash",
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, start_new_session=True, env=env,
                )
            except Exception as e:
                self._log(f"[SERVER] launch failed: {e}")
                return False, str(e)
            self.start_time = time.time()
            self._cleanup_done = False  # arm EOS / stop() cleanup hook
            self.config = {"videos": videos, "models": models, "rects": rects,
                           "detects": detects, "json_dir": json_dir, "num_ch": n}
            self.fps = self._empty_fps(n)
            self.lat_samples.clear()
            # Make sure a zones config file exists on disk for every channel;
            # we drive zoneguard via mtime watching on these. Re-emit them now
            # using the proper normalised-to-fraction writer.
            # We temporarily populate rects in self.config so _render_rect_wh
            # can read them (it needs to be available before the subprocess
            # starts or zoneguard will default to no zones).
            self.config.setdefault("rects", rects)
            for i in range(n):
                self._write_zones_file(i)
            threading.Thread(target=self._stream_logs, daemon=True).start()
            return True, f"Pipeline started ({n} channel{'s' if n>1 else ''})"

    def _on_pipeline_ended(self, reason="unknown"):
        """Idempotent cleanup that runs whenever the pipeline goes from
        running to not-running, regardless of cause: explicit stop(),
        natural EOS (video clip finished), or process crash. Clears any
        outstanding alerts and zeroes the TCM mailbox so the R5 mirror
        drops the LEDs. Safe to call multiple times — the _cleanup_done
        flag prevents duplicate work."""
        with self.lock:
            if getattr(self, "_cleanup_done", True):
                return
            self._cleanup_done = True
            self.start_time = None
            stuck = [ch for ch, a in self.alerts.items() if a["active"]]
        for ch in stuck:
            self.clear_alert(ch, reason=f"pipeline ended: {reason}")
        try:
            apu_gpio.all_off()
        except Exception as _e:
            self._log(f"[SERVER] apu_gpio.all_off failed: {_e}")
        # Belt-and-braces cleanup: zero TCM flags AND directly zero
        # the AXI GPIO data register at 0xA0010000. R5 should mirror
        # the TCM zero to GPIO automatically, but some firmware
        # variants only fire on a seq-change atomic write — a bare
        # APU flag-only zero may not propagate. Writing AXI GPIO
        # directly from APU forces LEDs off regardless of R5 state.
        try:
            rproc_running = False
            try:
                with open("/sys/class/remoteproc/remoteproc0/state") as _f:
                    rproc_running = _f.read().startswith("running")
            except Exception:
                rproc_running = False
            if rproc_running:
                import mmap as _mm, struct as _st
                # 1. Zero TCM[+0x4] (and bump seq so R5 sees a fresh write).
                try:
                    with open("/dev/mem", "r+b") as _fd:
                        _m = _mm.mmap(_fd.fileno(), 0x1000,
                                       prot=_mm.PROT_READ | _mm.PROT_WRITE,
                                       flags=_mm.MAP_SHARED, offset=0xFFE20000)
                        # ensure magic is set (in case it got cleared)
                        _st.pack_into("<I", _m, 0, 0x5A4C4544)
                        # zero flags
                        _st.pack_into("<I", _m, 4, 0)
                        # bump seq so R5 sees the change as a fresh event
                        _cur_seq = _st.unpack_from("<I", _m, 8)[0]
                        _st.pack_into("<I", _m, 8, (_cur_seq + 1) & 0xFFFFFFFF)
                        _m.close()
                except Exception as _e:
                    self._log(f"[SERVER] tcm zero failed: {_e}")
                # 2. Directly zero AXI GPIO data register so LEDs go
                # off immediately even if R5 is stuck. The R5 may
                # re-write within microseconds, but if its source
                # (TCM) is also zero the value sticks.
                try:
                    with open("/dev/mem", "r+b") as _fd:
                        _m = _mm.mmap(_fd.fileno(), 0x1000,
                                       prot=_mm.PROT_READ | _mm.PROT_WRITE,
                                       flags=_mm.MAP_SHARED, offset=0xA0010000)
                        _st.pack_into("<I", _m, 0, 0)
                        # set tri-state to 0 (output) on the four LED bits
                        # so the data register actually drives the pins.
                        _cur_tri = _st.unpack_from("<I", _m, 4)[0]
                        _st.pack_into("<I", _m, 4, _cur_tri & ~0xF)
                        _m.close()
                except Exception as _e:
                    self._log(f"[SERVER] gpio zero failed: {_e}")
        except Exception:
            pass
        self._log(f"[SERVER] Pipeline ended ({reason})")

    def stop(self):
        # Idempotent: works whether the gst-launch process is still alive
        # or has already exited (EOS, crash, etc.). The actual cleanup is
        # in _on_pipeline_ended which is also invoked by _stream_logs at
        # natural EOS.
        with self.lock:
            proc = self.proc
        already_exited = proc is None or proc.poll() is not None
        if not already_exited:
            import signal as _s, os as _o
            try: _o.killpg(_o.getpgid(proc.pid), _s.SIGTERM)
            except: proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: _o.killpg(_o.getpgid(proc.pid), _s.SIGKILL)
                except: proc.kill()
                proc.wait()
        self._on_pipeline_ended("stop()" if not already_exited else "already-exited")
        if already_exited and proc is None:
            return False, "Pipeline is not running"
        return True, "Pipeline stopped"

    def reconfigure(self, videos, models, rects, json_dir, detects=None):
        if self.proc and self.proc.poll() is None:
            self.stop()
        return self.start(videos, models, rects, json_dir, detects)

    def status(self):
        with self.lock:
            running = bool(self.proc and self.proc.poll() is None)
            uptime = int(time.time() - self.start_time) if running and self.start_time else 0
            latency = self._latency_snapshot()
            return {
                "running": running,
                "pid": self.proc.pid if running else None,
                "uptime_s": uptime,
                "config": self.config,
                "command": self.command,
                "fps": list(self.fps),
                "latency": latency,
                "channels": [],
            }

    def get_logs(self, since_line=0):
        with self.lock:
            lines = list(self.logs)
        return lines[since_line:]

    def _stream_logs(self):
        for line in self.proc.stdout:
            line = line.rstrip()

            # Suppress the extremely noisy tracer lines from the log UI — we
            # consume them internally but don't want them flooding the Logs pane.
            is_tracer = "GST_TRACER" in line

            if not is_tracer:
                self._log(line)

            fm = FPS_RE.search(line)
            if fm:
                ch_i = int(fm.group(1))
                with self.lock:
                    if 0 <= ch_i < len(self.fps):
                        self.fps[ch_i]["fps"] = float(fm.group(2))
                        self.fps[ch_i]["mean_fps"] = float(fm.group(3))
                        if self.config.get("models") and ch_i < len(self.config["models"]):
                            self.fps[ch_i]["model"] = self.config["models"][ch_i]
                continue

            if not is_tracer:
                continue

            # End-to-end pipeline latency (source → sink). Our sinks are named
            # ksink0/1/2/3, so the channel is the trailing digit of sink-element.
            pm = PIPELINE_LAT_RE.search(line)
            if pm:
                _src_el, sink_el, ns = pm.groups()
                if sink_el.startswith("ksink") and sink_el[5:].isdigit():
                    ch = int(sink_el[5:])
                    with self.lock:
                        self._record_latency(ch, "e2e_ms", int(ns))
                continue

            # Per-element latency. Element names are of the form
            # dec0 / scaler1 / infer2 / overlay3 / meta0 — prefix = stage, suffix = channel.
            em = ELEMENT_LAT_RE.search(line)
            if em:
                el_name, ns = em.groups()
                # Walk known prefixes to find the match
                for prefix, stage in LATENCY_STAGES:
                    if el_name.startswith(prefix):
                        tail = el_name[len(prefix):]
                        if tail.isdigit():
                            ch = int(tail)
                            with self.lock:
                                self._record_latency(ch, stage, int(ns))
                        break
                continue

        rc = self.proc.wait()
        self._log(f"[SERVER] gst-launch-1.0 exited with code {rc}")
        with self.lock:
            self.fps = self._empty_fps(4)
            self.lat_samples.clear()
            self.start_time = None
            # Clear any latched zone alerts — there is nothing analysing
            # frames any more, so nothing can flip them off. Leaving them
            # sticky would give the operator a false live alert and keep
            # the RPU heartbeat thread firing.
            stuck = [i for i in range(4) if self.alerts[i]["active"]]
            for i in stuck:
                self.alerts[i] = {"active": False, "since": None, "reason": None,
                                  "rpu_confirmed": False, "rpu_rtt_ms": None}
                self._push_event(i, "ALERT_CLEARED", "pipeline stopped")
        if stuck and self.led_mode == "apu":
            for i in stuck:
                apu_gpio.set_channel(i, False)
        if rpu_bridge is not None:
            for i in stuck:
                rpu_bridge.set_channel_active(i, False)
        # When _stream_logs returns the gst-launch subprocess has
        # exited (EOS, crash, or stop()-killed). Run the unified
        # post-pipeline cleanup which zeros the TCM mailbox so the R5
        # mirror drops the LEDs. _on_pipeline_ended is idempotent.
        try:
            self._on_pipeline_ended("EOS")
        except Exception as _e:
            try: self._log(f"[SERVER] EOS cleanup raised: {_e}")
            except Exception: pass


pipeline = PipelineManager()

# APU↔RPU alert bridge. Started after pipeline so the callback can close over it.
# If /dev/rpmsg0 isn't present (R5 not booted), bridge stays disabled and
# trigger_alert / clear_alert skip the send_trigger call — no functional impact.
def _rpu_on_confirm(ch, kind, rtt_ms):
    pipeline.mark_rpu_confirmed(ch, kind, rtt_ms)

rpu_bridge = RpuBridge(on_confirm=_rpu_on_confirm,
                       logger=lambda msg: (pipeline._log(msg) if pipeline else print(msg)))
rpu_bridge.start()


def _zoneguard_listener():
    """Receive newline-terminated JSON events from the zoneguard GStreamer
    plugin over a Unix-domain datagram socket and translate them into
    pipeline.trigger_alert / pipeline.clear_alert calls."""
    try:
        if os.path.exists(ZONEGUARD_SOCKET):
            os.unlink(ZONEGUARD_SOCKET)
    except OSError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(ZONEGUARD_SOCKET)
        os.chmod(ZONEGUARD_SOCKET, 0o666)   # allow the pipeline subprocess (root) AND us
    except OSError as e:
        print(f"[WARN] zoneguard socket bind failed: {e}")
        return
    print(f"[INFO] zoneguard listener bound at {ZONEGUARD_SOCKET}")
    while True:
        try:
            data, _ = sock.recvfrom(4096)
        except Exception:
            continue
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue
            ch = int(evt.get("ch", -1))
            if not 0 <= ch < 4:
                continue
            kind = evt.get("kind", "")
            reason = evt.get("reason") or kind
            if kind in ("ENTER", "CROSS"):
                pipeline.trigger_alert(ch, reason)
            elif kind == "CLEAR":
                pipeline.clear_alert(ch, reason or "no person in any zone")


# Start the listener thread as soon as the module loads so the socket
# exists before any gst-launch subprocess tries to sendto() it.
threading.Thread(target=_zoneguard_listener, daemon=True).start()


def _parse_start_payload(data):
    num_ch = max(1, min(4, int(data.get("num_ch", 4))))
    videos = [data.get(f"ch{i}", DEFAULT_VIDEO) for i in range(num_ch)]
    models = [data.get(f"model{i}", "refinedet") for i in range(num_ch)]
    detects = [bool(data.get(f"detect{i}", True)) for i in range(num_ch)]
    custom = data.get("rects", [])
    if custom and len(custom) == num_ch:
        rects = []
        for r in custom:
            x, y, w, h = clamp_rect(int(r.get("x", 0)), int(r.get("y", 0)),
                                    int(r.get("w", 960)), int(r.get("h", 540)))
            rects.append(rect_str(x, y, w, h))
    else:
        rects = list(DEFAULT_RECTS[num_ch])
    # Apply any zones sent with the start payload.
    for i in range(num_ch):
        zkey = f"zones{i}"
        if zkey in data:
            pipeline.set_zones(i, data.get(zkey) or [])
    return num_ch, videos, models, rects, detects


@app.route("/api/models")
def get_models():
    return jsonify({k: {"label": v["label"]} for k, v in MODELS.items()})


@app.route("/api/files")
def list_files():
    directory = request.args.get("dir", app.config["VIDEO_DIR"])
    try:
        files = sorted(set(
            os.path.join(directory, f) for f in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, f)) and f.lower().endswith(('.mp4', '.h264', '.mov', '.mkv'))
        ))
    except Exception:
        files = []
    return jsonify({"files": files, "dir": directory})


def _webcam_name(path):
    """Read the card's friendly name from /sys/class/video4linux/videoN/name.
    Returns '' if unavailable."""
    try:
        node = os.path.basename(path)
        with open(f"/sys/class/video4linux/{node}/name") as f:
            return f.read().strip()
    except Exception:
        return ""


def _webcam_h264_modes(path):
    """Parse v4l2-ctl --list-formats-ext and return only the H.264 modes
    as a list of {width, height, fps} dicts, sorted highest-res-first
    with fps as the tiebreaker. Our GStreamer pipeline expects H.264
    from v4l2src, so non-H264 modes are not offered to the UI."""
    try:
        out = subprocess.run(
            ["v4l2-ctl", "--device", path, "--list-formats-ext"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return []
    cur_fmt = None
    cur_size = None
    modes = []
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("[") and "]:" in line and "'" in line:
            # e.g.  [2]: 'H264' (H.264, compressed)
            cur_fmt = line.split("'")[1] if "'" in line else None
            cur_size = None
        elif line.startswith("Size:") and cur_fmt in ("H264",):
            # e.g.  Size: Discrete 1920x1080
            parts = line.split()
            if len(parts) >= 3 and "x" in parts[-1]:
                try:
                    w, h = parts[-1].split("x")
                    cur_size = (int(w), int(h))
                except ValueError:
                    cur_size = None
        elif line.startswith("Interval:") and cur_fmt in ("H264",) and cur_size:
            # e.g.  Interval: Discrete 0.033s (30.000 fps)
            fps = None
            try:
                fps_str = line.split("(")[1].split()[0]
                fps = int(round(float(fps_str)))
            except Exception:
                fps = None
            if fps:
                modes.append({"width": cur_size[0], "height": cur_size[1],
                              "fps": fps})
    # Dedupe + sort (highest-res first, then highest-fps).
    uniq = {(m["width"], m["height"], m["fps"]): m for m in modes}.values()
    return sorted(uniq, key=lambda m: (-m["width"]*m["height"], -m["fps"]))


@app.route("/api/webcams")
def list_webcams():
    """Return enriched webcam info: path, friendly name, and the list of
    H.264 modes the camera supports. Capture nodes are discovered via
    gst-device-monitor (same as before — skips the metadata nodes that
    UVC exposes alongside the capture node)."""
    devs = []
    paths = []
    try:
        out = subprocess.run(
            ["gst-device-monitor-1.0", "Video/Source"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        seen = set()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("device.path"):
                path = line.split("=", 1)[1].strip()
                if path.startswith("/dev/video") and path not in seen:
                    seen.add(path)
                    paths.append(path)
    except Exception:
        try:
            for name in sorted(os.listdir("/dev")):
                if name.startswith("video") and name[5:].isdigit():
                    paths.append(f"/dev/{name}")
        except Exception:
            pass
    for p in paths:
        devs.append({
            "path":  p,
            "name":  _webcam_name(p),
            "modes": _webcam_h264_modes(p),
        })
    return jsonify({"webcams": devs})


@app.route("/api/pipeline/start", methods=["POST"])
def start_pipeline():
    _, videos, models, rects, detects = _parse_start_payload(request.get_json(force=True))
    ok, msg = pipeline.start(videos, models, rects, app.config["JSON_DIR"], detects)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/pipeline/stop", methods=["POST"])
def stop_pipeline():
    ok, msg = pipeline.stop()
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/pipeline/reconfigure", methods=["POST"])
def reconfigure_pipeline():
    _, videos, models, rects, detects = _parse_start_payload(request.get_json(force=True))
    ok, msg = pipeline.reconfigure(videos, models, rects, app.config["JSON_DIR"], detects)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/pipeline/status")
def pipeline_status():
    return jsonify(pipeline.status())


@app.route("/api/pipeline/logs")
def pipeline_logs():
    since = int(request.args.get("since", 0))
    lines = pipeline.get_logs(since)
    return jsonify({"lines": lines, "total": since + len(lines)})


@app.route("/api/pipeline/preview")
def pipeline_preview():
    d = request.args
    num_ch = max(1, min(4, int(d.get("num_ch", 4))))
    videos = [d.get(f"ch{i}", DEFAULT_VIDEO) for i in range(num_ch)]
    models = [d.get(f"model{i}", "refinedet") for i in range(num_ch)]
    detects = [d.get(f"detect{i}", "1") != "0" for i in range(num_ch)]
    rects = list(DEFAULT_RECTS[num_ch])
    for i in range(num_ch):
        rv = d.get(f"rect{i}")
        if rv:
            try:
                parts = [int(v) for v in rv.split(",")]
                x, y, w, h = clamp_rect(*parts)
                rects[i] = rect_str(x, y, w, h)
            except Exception:
                pass
    return jsonify({"command": command_pretty(videos, models, rects, app.config["JSON_DIR"], detects)})


@app.route("/api/display")
def display_info():
    return jsonify({"width": DISPLAY_W, "height": DISPLAY_H})


# ---------------------------------------------------------------------------
# Zones + events (for the event classifier / alert system)
# ---------------------------------------------------------------------------

@app.route("/api/pipeline/zones", methods=["GET"])
def get_zones():
    """Return { "0": [...], "1": [...], ... } — current zones per channel."""
    return jsonify(pipeline.get_zones())


@app.route("/api/pipeline/zones", methods=["POST"])
def set_zones():
    """Body: { "ch": int, "zones": [ { type, ... }, ... ] }
    Replaces the zone list for that channel. Takes effect immediately; if
    the pipeline is running the zones are used on the next inference tick."""
    data = request.get_json(force=True)
    ch = int(data.get("ch", 0))
    if not 0 <= ch < 4:
        return jsonify({"ok": False, "message": f"Bad ch={ch}"}), 400
    zones = data.get("zones", [])
    # Validate / normalise each zone.
    clean = []
    for z in zones:
        t = z.get("type")
        name = str(z.get("name", f"zone{len(clean)}"))[:40]
        if t == "rect":
            clean.append({
                "type": "rect", "name": name,
                "x": int(z.get("x", 0)), "y": int(z.get("y", 0)),
                "w": int(z.get("w", 0)), "h": int(z.get("h", 0)),
            })
        elif t == "line":
            clean.append({
                "type": "line", "name": name,
                "x1": int(z.get("x1", 0)), "y1": int(z.get("y1", 0)),
                "x2": int(z.get("x2", 0)), "y2": int(z.get("y2", 0)),
            })
    pipeline.set_zones(ch, clean)
    return jsonify({"ok": True, "zones": pipeline.get_zones(ch)})


@app.route("/api/pipeline/zone_overlay", methods=["POST"])
def set_zone_overlay():
    """Live toggle for zone-on-video drawing. Body: {"show": true/false}.
    Rewrites every channel's zoneguard JSON; zoneguard reloads on mtime."""
    data = request.get_json(force=True)
    show = bool(data.get("show", True))
    pipeline.draw_overlay = show
    for ch in range(4):
        pipeline._write_zones_file(ch)
    return jsonify({"ok": True, "show": show})


@app.route("/api/events")
def api_events():
    """Alert states (per channel) + last 30 events. Poll this from the UI."""
    return jsonify(pipeline.events_snapshot())


@app.route("/api/led/mode", methods=["GET"])
def get_led_mode():
    """Current LED source + per-channel pin state so the UI can show which
    LED is actually lit."""
    pins = {}
    for ch, num in enumerate(APU_GPIO_LINES):
        try:
            with open(f"/sys/class/gpio/gpio{num}/value") as f:
                pins[ch] = int(f.read().strip())
        except Exception:
            pins[ch] = None
    # Backwards-compat: "pin" reflects channel 0; new UI should use "pins".
    return jsonify({"mode": pipeline.led_mode,
                    "pin": pins.get(0),
                    "pins": pins,
                    "rpu_link": (rpu_bridge is not None and rpu_bridge.enabled)})


@app.route("/api/led/mode", methods=["POST"])
def set_led_mode_route():
    """Switch LED source. Body: {"mode": "apu"|"rpu"}."""
    data = request.get_json(force=True) or {}
    ok, msg = pipeline.set_led_mode(data.get("mode"))
    return jsonify({"ok": ok, "message": msg, "mode": pipeline.led_mode}), (200 if ok else 400)


@app.route("/api/shm/stats")
def api_shm_stats():
    """Read the APU↔R5 shared-memory alert page. Returns
    {magic: int, flags: int, seq: int, ts_ns: int, ok: bool, error: str|null}.
    The UI polls this to visualise the fast-path flag state independently
    of the rpmsg confirmation badge. Cheap: one mmap/read/munmap per call.
    """
    # The OCM page at 0xFFE20000 is only mapped while the R5 firmware is
    # running. Reading it when RPU is down raises SIGBUS (a signal, not a
    # Python exception) which kills the whole server. Gate on the kernel
    # remoteproc state instead of rpu_bridge.enabled. The bridge flag
    # tracks the rpmsg slow-path control link, which may be in a
    # reconnect loop while the TCM fast-path data plane keeps working
    # fine. Reading TCM is only unsafe when the R5 firmware itself is
    # offline. The actual /dev/mem read still runs in a child process so
    # any SIGBUS dies in the child, not in the Flask main process.
    try:
        with open("/sys/class/remoteproc/remoteproc0/state") as _f:
            _r5_state = _f.read().strip()
    except Exception:
        _r5_state = "unknown"
    if _r5_state != "running":
        return jsonify({"ok": False, "magic": 0, "flags": 0, "seq": 0,
                        "ts_ns": 0, "error": f"r5 not running (state={_r5_state})"}), 200
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "import mmap,struct,sys;"
             "fd=open('/dev/mem','rb');"
             "m=mmap.mmap(fd.fileno(),0x1000,prot=mmap.PROT_READ,"
             "flags=mmap.MAP_SHARED,offset=0xFFE20000);"
             "a=struct.unpack_from('<I',m,0)[0];"
             "b=struct.unpack_from('<I',m,4)[0];"
             "c=struct.unpack_from('<I',m,8)[0];"
             "d=struct.unpack_from('<Q',m,16)[0];"
             "m.close();fd.close();"
             "sys.stdout.write(f'{a} {b} {c} {d}')"],
            capture_output=True, text=True, timeout=2)
        if r.returncode != 0 or not r.stdout.strip():
            return jsonify({"ok": False, "magic": 0, "flags": 0, "seq": 0,
                            "ts_ns": 0,
                            "error": (r.stderr or "shm read failed").strip()}), 200
        magic, flags, seq, ts_ns = (int(x) for x in r.stdout.split())
        return jsonify({"ok": True, "magic": magic, "flags": flags,
                        "seq": seq, "ts_ns": ts_ns, "error": None})
    except Exception as e:
        return jsonify({"ok": False, "magic": 0, "flags": 0, "seq": 0,
                        "ts_ns": 0, "error": str(e)}), 200


@app.route("/api/events/trigger", methods=["POST"])
def api_events_trigger():
    """Manual alert trigger — used for UI testing until the detection
    metadata reader is wired up. Body: { "ch": int, "reason": str }."""
    data = request.get_json(force=True)
    ch = int(data.get("ch", 0))
    reason = str(data.get("reason", "manual"))[:120]
    if not 0 <= ch < 4:
        return jsonify({"ok": False, "message": f"Bad ch={ch}"}), 400
    pipeline.trigger_alert(ch, reason)
    return jsonify({"ok": True})


@app.route("/api/events/clear", methods=["POST"])
def api_events_clear():
    data = request.get_json(force=True)
    ch = int(data.get("ch", 0))
    if not 0 <= ch < 4:
        return jsonify({"ok": False, "message": f"Bad ch={ch}"}), 400
    pipeline.clear_alert(ch)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Board statistics (power, thermal, CPU, DPU)
# ---------------------------------------------------------------------------

def _read_meminfo():
    """Parse /proc/meminfo into {MemTotal: bytes, MemAvailable: bytes, ...}."""
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if parts and parts[0].isdigit():
                    info[key] = int(parts[0]) * 1024  # kB -> bytes
    except Exception:
        pass
    return info


def _read_loadavg():
    try:
        with open("/proc/loadavg") as f:
            a, b, c, *_ = f.read().split()
        return [float(a), float(b), float(c)]
    except Exception:
        return [0.0, 0.0, 0.0]


def _read_uptime_s():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


_dpu_state = {"last_usage": None, "last_ts": 0.0, "usage_per_s": 0.0, "cu": {}}


def _read_dpu_stats():
    """Parse `xbutil examine -d 0 --report dynamic-regions` for CU usage."""
    out = {"cu": {}, "active": False}
    try:
        proc = subprocess.run(
            ["xbutil", "examine", "-d", "0", "--report", "dynamic-regions"],
            capture_output=True, text=True, timeout=3,
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            m = re.match(r"(\d+)\s+(\S+)\s+(0x[0-9a-fA-F]+)\s+(\d+)\s+\(([A-Z_]+)\)", line)
            if not m:
                continue
            idx, name, addr, usage, status = m.groups()
            out["cu"][name] = {"usage": int(usage), "status": status}
    except Exception:
        pass

    # Compute delta usage-per-second for DPU CU
    now = time.time()
    dpu_total = sum(v["usage"] for k, v in out["cu"].items() if "DPU" in k)
    last = _dpu_state["last_usage"]
    if last is not None and now - _dpu_state["last_ts"] > 0:
        out["usage_per_s"] = round((dpu_total - last) / (now - _dpu_state["last_ts"]), 1)
    else:
        out["usage_per_s"] = 0.0
    _dpu_state["last_usage"] = dpu_total
    _dpu_state["last_ts"] = now
    # "active" if any CU is in BUSY state
    out["active"] = any(v["status"] == "BUSY" for v in out["cu"].values())
    return out


@app.route("/api/stats")
def board_stats():
    """Return a snapshot of board statistics: power, thermal, CPU, DPU, memory."""
    result = {
        "available": STATS_AVAILABLE,
        "timestamp": time.time(),
    }

    if STATS_AVAILABLE:
        try:
            result["cpu_utilization"] = _stats.get_cpu_utilization()
        except Exception:
            result["cpu_utilization"] = None
        try:
            freqs = []
            for i in range(4):
                r = _stats.get_cpu_frequency(i)
                khz = r[1] if isinstance(r, (list, tuple)) and len(r) >= 2 else r
                freqs.append(round(khz / 1000.0, 0) if khz else 0)
            result["cpu_frequency_mhz"] = freqs
        except Exception:
            result["cpu_frequency_mhz"] = None
        try:
            t = _stats.get_temperatures()
            if isinstance(t, (list, tuple)) and len(t) >= 4:
                vals = [round(v / 1000.0, 1) for v in t[1:4]]
                result["temperatures_c"] = dict(zip(_TEMP_LABELS, vals))
        except Exception:
            result["temperatures_c"] = None
        try:
            v = _stats.get_voltages()
            if isinstance(v, (list, tuple)) and len(v) >= 10:
                result["voltages_mv"] = dict(zip(_VOLT_LABELS, v[1:10]))
        except Exception:
            result["voltages_mv"] = None
        try:
            p = _stats.get_power()
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                result["power_uw"] = p[1]
                result["power_w"] = round(p[1] / 1_000_000.0, 2)
        except Exception:
            result["power_uw"] = None
        try:
            c = _stats.get_current()
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                result["current_ma"] = c[1]
        except Exception:
            result["current_ma"] = None

    # /proc-derived stats (always available)
    mem = _read_meminfo()
    if mem:
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        used = total - avail
        result["memory"] = {
            "total_mb": round(total / 1024 / 1024, 1),
            "available_mb": round(avail / 1024 / 1024, 1),
            "used_mb": round(used / 1024 / 1024, 1),
            "used_pct": round(100 * used / total, 1) if total else 0,
        }
        # CMA (contiguous memory for DMA — large for Xilinx hw pipelines)
        cma_total = mem.get("CmaTotal", 0)
        cma_free = mem.get("CmaFree", 0)
        if cma_total:
            result["cma"] = {
                "total_mb": round(cma_total / 1024 / 1024, 1),
                "free_mb": round(cma_free / 1024 / 1024, 1),
                "used_mb": round((cma_total - cma_free) / 1024 / 1024, 1),
                "used_pct": round(100 * (cma_total - cma_free) / cma_total, 1),
            }

    result["load_avg"] = _read_loadavg()
    result["uptime_s"] = _read_uptime_s()

    # DPU / compute-unit usage
    result["dpu"] = _read_dpu_stats()
    result["pipeline_running"] = bool(pipeline.proc and pipeline.proc.poll() is None)

    # RPU link stats (for APU↔RPU visualization). Client may pass ?since=<seq>
    # to request only events newer than what it has already rendered.
    try:
        since = int(request.args.get("since", "0"))
    except (TypeError, ValueError):
        since = 0
    try:
        result["rpu"] = rpu_bridge.get_stats(since_seq=since)
    except Exception as e:
        result["rpu"] = {"enabled": False, "last_error": f"stats: {e}"}
    return jsonify(result)


FAN_PWM_PATH = "/sys/class/hwmon/hwmon2/pwm1"
FANCONTROL_UNIT = "fancontrol.service"


def _fancontrol_active():
    try:
        r = subprocess.run(["systemctl", "is-active", FANCONTROL_UNIT],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _fancontrol_set(active: bool):
    """start or stop fancontrol.service. Returns (ok, message)."""
    action = "start" if active else "stop"
    try:
        r = subprocess.run(["systemctl", action, FANCONTROL_UNIT],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
    except Exception as e:
        return False, str(e)
    return True, "ok"


@app.route("/api/fan", methods=["GET"])
def get_fan():
    try:
        with open(FAN_PWM_PATH) as f:
            pwm = int(f.read().strip())
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    auto = _fancontrol_active()
    return jsonify({
        "ok": True, "pwm": pwm, "pct": round(pwm * 100 / 255, 1),
        "mode": "auto" if auto else "manual",
    })


@app.route("/api/fan", methods=["POST"])
def set_fan():
    data = request.get_json(force=True) or {}
    if "pwm" in data:
        pwm = int(data["pwm"])
    elif "pct" in data:
        pwm = int(round(float(data["pct"]) * 255 / 100))
    else:
        return jsonify({"ok": False, "message": "need pwm (0-255) or pct (0-100)"}), 400
    pwm = max(0, min(255, pwm))
    # Writing while fancontrol is active is pointless — it will be overwritten
    # within a few seconds. Auto-switch to manual mode so the setting sticks.
    auto_before = _fancontrol_active()
    if auto_before:
        _fancontrol_set(False)
    try:
        with open(FAN_PWM_PATH, "w") as f:
            f.write(str(pwm))
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    return jsonify({"ok": True, "pwm": pwm, "pct": round(pwm * 100 / 255, 1),
                    "mode": "manual"})


@app.route("/api/fan/mode", methods=["POST"])
def set_fan_mode():
    """Switch between lm_sensors fancontrol ('auto') and manual slider control.
    Body: {"mode": "auto"|"manual"}."""
    data = request.get_json(force=True) or {}
    mode = (data.get("mode") or "").lower()
    if mode not in ("auto", "manual"):
        return jsonify({"ok": False, "message": "mode must be 'auto' or 'manual'"}), 400
    ok, msg = _fancontrol_set(mode == "auto")
    if not ok:
        return jsonify({"ok": False, "message": msg}), 500
    return jsonify({"ok": True, "mode": mode})


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


MODULES_LOG_PATH = "/home/petalinux/modules.log"

def _dump_modules_log():
    """Write all installed Python distributions + key runtime versions to
    modules.log. This is used as Figure 18 ("installed package list") in the
    v2.docx report and also provides a post-mortem record for each run."""
    try:
        import importlib.metadata as md
    except Exception:
        return
    import platform, sys as _sys
    try:
        dists = sorted(
            ((d.metadata["Name"], d.version) for d in md.distributions()),
            key=lambda x: (x[0] or "").lower()
        )
    except Exception:
        dists = []
    lines = []
    lines.append(f"# server.py module snapshot @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"# python   : {platform.python_version()} ({_sys.executable})")
    lines.append(f"# platform : {platform.platform()}")
    lines.append(f"# pid      : {os.getpid()}")
    lines.append("")
    lines.append("[python distributions]")
    for name, ver in dists:
        if not name:
            continue
        lines.append(f"{name}=={ver}")
    lines.append("")
    lines.append("[key imports in server.py]")
    for mod in ("flask", "werkzeug", "rpu_bridge"):
        try:
            m = __import__(mod)
            v = getattr(m, "__version__", "?")
            lines.append(f"{mod}={v} ({getattr(m, '__file__', '?')})")
        except Exception as e:
            lines.append(f"{mod}=ERR ({e})")
    try:
        with open(MODULES_LOG_PATH, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[WARN] failed to write {MODULES_LOG_PATH}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--video-dir", default=DEFAULT_VIDEO_DIR)
    parser.add_argument("--json-dir", default=DEFAULT_JSON_DIR)
    args = parser.parse_args()
    app.config["VIDEO_DIR"] = args.video_dir
    app.config["JSON_DIR"] = args.json_dir
    _dump_modules_log()
    print("\n" + "="*60)
    print(" VVAS 3.0 — COMBINED (single gst-launch) fallback variant")
    print(f" Listening: http://0.0.0.0:{args.port}")
    print(f" Login:     user={UI_USER!r} (UI_PASS env to override)")
    print(f" Modules:   {MODULES_LOG_PATH}")
    print("="*60 + "\n")
    app.run(host="0.0.0.0", port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
