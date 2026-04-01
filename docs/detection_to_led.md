# Detection → LED — plan and implementation log

End-to-end objective: when zoneguard reports a person inside a zone,
the web UI shows the alert (already works) **and** the physical LED on
PMOD J2 pin 1 (bit 0 of `axi_gpio_0`) stays ON for exactly as long as
at least one tracked person remains inside any zone, across any
channel. On CLEAR → LED OFF.

Two phases. Phase A is a throwaway-friendly stepping stone that proves
the state-tracking logic end-to-end; Phase B is the real delivery for
Objective 6 (APU↔RPU rpmsg + R5-driven output).

---

## Architecture

### Existing

```
gst pipeline
   └── zoneguard (C element)
          │  hit-test vs. /tmp/zoneguard_ch{0..3}.json
          ▼
   SOCK_DGRAM /tmp/zoneguard.sock
          │   "{\"ch\":1,\"zone\":\"A\",\"state\":\"ENTER\",\"id\":17,...}"
          ▼
   server.py event loop (reads socket, updates in-memory event log)
          │
          ├── /api/events          ── web UI polls, renders alert banner
          ├── /api/events/trigger  ── manual override
          └── /api/events/clear
```

### Phase A adds

```
server.py event loop
   └── on ENTER/CLEAR: update aggregate occupancy counter
         if counter > 0: echo 1 > /sys/class/gpio/gpio504/value
         if counter = 0: echo 0 > /sys/class/gpio/gpio504/value
```

Aggregate counter = number of `(channel, track_id)` pairs currently
inside any zone. Incremented on ENTER, decremented on CLEAR, floored
at 0. LED = 1 iff counter > 0.

### Phase B replaces the gpio sink with rpmsg

```
server.py event loop
   └── on ENTER/CLEAR: write one byte to /dev/rpmsg0
         byte = 0x01 if counter > 0 else 0x00
                            │
                            ▼
                  R5 rpmsg endpoint handler
                            │
                            ▼
                  XGpio_DiscreteWrite(&gpio, 1, byte & 0xF)
```

Advantage: R5 owns the pin. If the APU hangs, the R5 can still drive
the machinery-stop signal (this is why the end-goal wants R5 in the
loop at all).

---

## Phase A — APU direct-drive (steps)

> Precondition: `multichannel-openamp-gpio` overlay loaded, APU sysfs
> blink already verified. If `rpu-blink` firmware is running on R5,
> stop it first so the two don't fight for the register.

### A.1 Stop R5 firmware (one-time)

```sh
echo stop | sudo tee /sys/class/remoteproc/remoteproc0/state
cat       /sys/class/remoteproc/remoteproc0/state   # expect: offline
```

### A.2 Add a GPIO sink to server.py

Introduce a tiny module `gpio_alert.py` (or inline helper) that owns
the sysfs handle and exposes `set(on: bool)`. Keep it single-writer.

Sketch:

```python
# gpio_alert.py
import os, threading
GPIO = "504"
BASE = f"/sys/class/gpio/gpio{GPIO}"

_lock = threading.Lock()
_inited = False
_last = None

def _init():
    global _inited
    if _inited: return
    if not os.path.isdir(BASE):
        with open("/sys/class/gpio/export", "w") as f: f.write(GPIO)
    with open(f"{BASE}/direction", "w") as f: f.write("out")
    _inited = True

def set(on: bool):
    global _last
    with _lock:
        _init()
        val = "1" if on else "0"
        if val == _last: return             # skip redundant writes
        with open(f"{BASE}/value", "w") as f: f.write(val)
        _last = val

def close():
    with _lock:
        try:
            with open(f"{BASE}/value",    "w") as f: f.write("0")
            with open("/sys/class/gpio/unexport","w") as f: f.write(GPIO)
        except OSError: pass
```

### A.3 Wire to the zoneguard event loop

In `server.py`, find the function that consumes `/tmp/zoneguard.sock`
(`trigger_alert` / `_on_event` / the dgram recv loop — whatever the
existing handler is called). Track an occupancy set:

```python
import gpio_alert

# module-scope
_occupied = set()   # of (channel:int, track_id:int)

def on_zoneguard_event(evt: dict):
    key = (evt["ch"], evt["id"])
    if evt["state"] == "ENTER":
        _occupied.add(key)
    elif evt["state"] == "CLEAR":
        _occupied.discard(key)
    gpio_alert.set(len(_occupied) > 0)
    # existing behaviour (web UI log, etc.) unchanged
```

Register `gpio_alert.close` on shutdown (`atexit` or Flask teardown).

### A.4 Restart server and smoke-test

```sh
sudo pkill -9 -f 'python3 /home/petalinux/server.py' 2>/dev/null
: > /tmp/server.log
sudo python3 /home/petalinux/server.py > /tmp/server.log 2>&1 &
disown $!

# manual trigger path:
curl -s -X POST http://127.0.0.1:5000/api/events/trigger \
     -H 'content-type: application/json' \
     -d '{"ch":0,"zone":"A","id":999,"state":"ENTER"}'
# LED should be ON

curl -s -X POST http://127.0.0.1:5000/api/events/trigger \
     -H 'content-type: application/json' \
     -d '{"ch":0,"zone":"A","id":999,"state":"CLEAR"}'
# LED should be OFF
```

Then exercise for real: start a pipeline, walk a person through a zone,
confirm LED tracks.

### A.5 Edge cases to handle in the handler

- Missing `id` (older zoneguard payloads): fall back to `(ch, zone)` as
  the key; LED stays on while any zone has any detection.
- Server restart while person is inside a zone: `_occupied` is empty on
  start, LED off even though UI will re-emit ENTER on next frame → OK
  because zoneguard re-sends state every 30 frames (heartbeat).
- Pipeline stop: explicitly call `gpio_alert.set(False)` in the
  `/api/pipeline/stop` handler and in shutdown.

---

## Phase B — R5-driven GPIO via rpmsg

### B.1 Extend the R5 firmware

Start from existing `rpu-blink` app in
`.../multichannel/rpu-test/vitis/rpu-blink/`. Rename to `rpu-gpio-rpmsg`
or add a new app in the same platform. Pull in OpenAMP from the BSP
(already present — the echo app references it).

`main.c` sketch:

```c
#include "xgpio.h"
#include "openamp/open_amp.h"
#include "platform_info.h"
#include "rsc_table.h"

static XGpio gpio;

static int rpmsg_ept_cb(struct rpmsg_endpoint *ept, void *data,
                        size_t len, uint32_t src, void *priv) {
    if (len >= 1) {
        uint8_t mask = ((uint8_t *)data)[0] & 0xF;
        XGpio_DiscreteWrite(&gpio, 1, mask);
    }
    /* echo back for APU sanity */
    return rpmsg_send(ept, data, len);
}

int main(void) {
    XGpio_Initialize(&gpio, XPAR_AXI_GPIO_0_DEVICE_ID);
    XGpio_SetDataDirection(&gpio, 1, 0x0);
    XGpio_DiscreteWrite(&gpio, 1, 0x0);

    struct rpmsg_device *rpdev = platform_create_rpmsg_vdev(0, /*...*/);
    struct rpmsg_endpoint ept;
    rpmsg_create_ept(&ept, rpdev, "gpio", RPMSG_ADDR_ANY, RPMSG_ADDR_ANY,
                     rpmsg_ept_cb, NULL);
    for (;;) platform_poll(/*...*/);
}
```

Build → `rpu-gpio-rpmsg.elf`, deploy same way as `rpu-blink.elf`.
Keep `.echo.bak` for rollback; add `.blink.bak` too.

### B.2 APU side — replace the sysfs sink

Swap `gpio_alert.py` for `rpu_bridge.py`:

```python
_fd = None

def _init():
    global _fd
    if _fd is None:
        _fd = os.open("/dev/rpmsg0", os.O_WRONLY)

def set(on: bool):
    _init()
    os.write(_fd, bytes([0x01 if on else 0x00]))
```

`server.py` import line changes; the `set(...)` API is identical. No
other code changes. Watch for `/dev/rpmsg0` vs `/dev/rpmsg_ctrl0` vs
`/dev/rpmsgN` — the exact node depends on how the rpmsg char driver
probes; `ls /dev/rpmsg*` after starting R5 tells you.

### B.3 Verify

- `dmesg | grep rpmsg` after `echo start > .../state` → should show
  `virtio_rpmsg_bus: channel gpio ...` or similar.
- `printf '\x01' > /dev/rpmsg0` → LED ON. `printf '\x00'` → OFF.
- Run the full zone walk; LED should still track, now driven by R5.

### B.4 Safety fallback

R5 crash = LED state stuck at whatever the last successful write set.
Add a watchdog pattern later: R5 firmware clears the pin if it hasn't
seen a heartbeat write in > 2 s. Out of scope for the first rpmsg
version.

---

## Implementation log

### 2026-04-20 — plan authored

- Reviewed existing zoneguard → server.py → UI event chain.
- Settled on Phase A first (APU sysfs) so the detection→LED logic can
  be exercised without waiting on the rpmsg firmware rebuild, then
  Phase B swaps only the sink module.
- Phase A state model = aggregate occupancy set keyed on
  `(channel, track_id)`. LED = ON iff set non-empty.
- Phase B preserves the `set(on: bool)` API; server.py changes are a
  one-line import swap.

### (to be filled)

- Phase A — date implemented, diff against `server.py`, smoke-test
  commands + output, zone-walk validation result.
- Phase B — date, Vitis app name, ELF size, `dmesg` rpmsg lines, end-
  to-end validation result, latency measurement (zone ENTER → LED high,
  milliseconds).

---

## Open questions

1. Do we want LED ON per channel (4 LEDs, one per ch) or a single LED
   for "any zone occupied"? Phase A sketch does the latter; changing
   to the former is just `XGpio_DiscreteWrite(&gpio, 1, ch_mask)`.
2. Hysteresis — zoneguard already enforces `ENTER_STREAK=3 /
   CLEAR_STREAK=15`, so the LED should be smooth without extra
   debouncing at the sink. Leave as-is unless flicker is observed.
3. Audible alert follows same path? If yes, add a second bit / second
   rpmsg opcode.
