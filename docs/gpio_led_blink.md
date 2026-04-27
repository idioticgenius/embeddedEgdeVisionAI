# GPIO LED blink — APU and RPU paths

KV260 multichannel-openamp-gpio overlay. Drive a PMOD-connected LED from
either the APU (Linux sysfs) or the RPU (R5 baremetal / rpmsg-triggered).
This is the first smoke test of the GPIO path before wiring it into the
zoneguard → RPU trigger chain.

---

## 1. Architecture

```
 ┌───────────────────────── PS ─────────────────────────┐      ┌──── PL ────┐
 │                                                      │      │             │
 │  APU (A53, Linux 5.15)                               │      │             │
 │    └─ /sys/class/gpio/gpio504  ── gpio-xilinx driver ┼──┐   │             │
 │                                                      │  │   │             │
 │  RPU (R5 core 0, baremetal / OpenAMP)                │  │   │             │
 │    └─ XGpio_DiscreteWrite(0xA0010000, ...)  ─────────┼──┤   │             │
 │                                                      │  │   │             │
 │                                   AXI ◄──────────────┼──┴──►│ axi_gpio_0  │
 │                                                      │      │  @0xA0010000│
 │                                                      │      │  4-bit out  │
 │                                                      │      │             │
 └──────────────────────────────────────────────────────┘      └──────┬──────┘
                                                                      │
                                              FPGA pin H12 (PMOD J2.1)│
                                                                      ▼
                                                            ┌──────── PMOD J2 ────────┐
                                                            │  pin1 H12 ── R ── LED ──┤ GND(pin5/11)
                                                            └─────────────────────────┘
```

Key point: both APU and RPU reach `axi_gpio_0` over the PS→PL AXI
interconnect. Whoever writes the data register last wins. There is no
hardware arbitration — coordination is a software decision (typical
pattern: APU never touches the pin while RPU firmware owns it).

---

## 2. Hardware side

### 2.1 FPGA block

Vivado block design adds **AXI GPIO IP** (`axi_gpio_0`):

- Base address: `0xA0010000`, 64 KB aperture.
- Width: 4 bits, all outputs.
- Connected to SoC PL-PS master via `pspmc/M_AXI_HPM0_FPD`.
- Pin constraints in XDC pin the 4 output bits to PMOD J2 FPGA balls:

| GPIO bit | sysfs# (Linux) | FPGA ball | PMOD J2 pin |
|----------|----------------|-----------|-------------|
| 0        | 504            | H12       | 1           |
| 1        | 505            | E10       | 2           |
| 2        | 506            | D10       | 3           |
| 3        | 507            | C11       | 4           |

> `IOSTANDARD` and `DRIVE` on these pins are set in the XDC. If an LED
> looks dim, drive strength is baked into the bitstream — fix the XDC
> and re-synth, or lower the series resistor.

### 2.2 Carrier / PMOD wiring

KV260 PMOD J2 is 3.3 V (level-shifted on the SOM carrier). Typical LED
wiring:

```
J2.1 ──►├R├─►|├── J2.5 (GND)
         220Ω  LED
```

Forward-voltage reference — red ~2.0 V, green ~2.1 V, blue/white ~3.0 V.
With 3.3 V PMOD rail:

- 220 Ω + red ≈ 6 mA (good brightness, PMOD spec headroom).
- 1 kΩ + red ≈ 1.3 mA (dim — what we hit first).
- White/blue LED + 3.3 V rail: barely lights even without a resistor.

### 2.3 Device-tree overlay

`kv260-multichannel-gpio.dtsi` adds the axi_gpio node inside
`fragment@X/__overlay__/&amba_pl`:

```
axi_gpio_0: gpio@a0010000 {
    #gpio-cells = <2>;
    compatible = "xlnx,axi-gpio-2.0", "xlnx,xps-gpio-1.00.a";
    gpio-controller;
    reg = <0x0 0xa0010000 0x0 0x10000>;
    xlnx,all-inputs = <0x0>;
    xlnx,all-outputs = <0x1>;
    xlnx,gpio-width = <0x4>;
    xlnx,interrupt-present = <0x0>;
};
```

Compiled: `/lib/firmware/xilinx/multichannel-openamp-gpio/kv260-multichannel-gpio.dtbo`.
Two dtsi pitfalls fixed in session 2026-04-20 (see
`rpu-enablement.md §16`):

1. Do **not** redeclare `reserved-memory` inside the overlay — those
   nodes already exist in the base DTB and duplicate-apply returns
   `-EINVAL`.
2. `tcm_0a` / `tcm_0b` need `power-domain = <&zynqmp_firmware 15/16>`
   or `rproc_boot` silently skips TCM loading and later fails with
   "bad phdr da 0x0".

---

## 3. Bring-up sequence (post-reboot)

```sh
sudo xmutil unloadapp                       # clear autoloaded k26-starter-kits
sudo xmutil dp_unbind                       # release DP for overlay reload
sudo xmutil loadapp multichannel-openamp-gpio
sudo xmutil listapps                        # expect Active_slot=0 for openamp-gpio
dmesg | tail -40                            # only memleak WARNs, no err=-22
```

Verify the overlay brought in the expected nodes:

```sh
ls /sys/bus/platform/devices/ | grep gpio
   firmware:zynqmp-firmware:gpio
   a0010000.gpio                     ◄── the new AXI GPIO
   ff0a0000.gpio                     ◄── PS MIO/EMIO bank

cat /sys/class/gpio/gpiochip504/label    → a0010000.gpio
cat /sys/class/gpio/gpiochip504/ngpio    → 4

ls /sys/class/remoteproc/                → remoteproc0
cat /sys/class/remoteproc/remoteproc0/state → offline
```

---

## 4. Software side — APU (Linux)

### 4.1 One-shot light

```sh
sudo sh -c '
  echo 504 > /sys/class/gpio/export
  echo out > /sys/class/gpio/gpio504/direction
  echo 1   > /sys/class/gpio/gpio504/value
'
# read back
cat /sys/class/gpio/gpio504/value
```

### 4.2 Blink script

`/home/petalinux/blink_led.sh`:

```sh
sudo ./blink_led.sh                 # default: gpio504, 1 Hz, forever
sudo ./blink_led.sh 505 0.2 10      # gpio505, 200 ms period, 10 blinks
sudo ./blink_led.sh 504 0.5 0       # forever, Ctrl+C cleans up
```

Script exports → sets direction → toggles → on SIGINT/SIGTERM resets
value to 0 and unexports the pin.

### 4.3 Peek at the register directly

```sh
sudo devmem 0xA0010000 32      # AXI GPIO data reg: bit0 = LED0 state
sudo devmem 0xA0010004 32      # tri-state reg (0 = output, 1 = input)
```

Useful for sanity-checking what the kernel driver actually wrote.

### 4.4 Python example

```py
with open('/sys/class/gpio/export','w') as f: f.write('504')
with open('/sys/class/gpio/gpio504/direction','w') as f: f.write('out')
with open('/sys/class/gpio/gpio504/value','w') as f: f.write('1')
```

---

## 5. Software side — RPU (R5 baremetal)

Goal: replace the OpenAMP echo firmware with a standalone R5 ELF that
toggles `axi_gpio_0`. No rpmsg yet — first prove the R5 can drive the
pin at all.

### 5.1 Vitis project (host)

Using Vitis 2022.2 with the XSA exported from the `multichannel-openamp-gpio`
Vivado design:

1. **Create platform**: `xsct` → `platform create -name r5_gpio_plat
   -hw <design>.xsa`, select `psu_cortexr5_0`, OS = standalone.
2. **Generate BSP**: include `xilgpio` / `xgpio` driver (auto-discovered
   from `axi_gpio_0` in the XSA).
3. **Create app**: template "Empty Application (C)", link against the
   platform.
4. **Linker script** must place `.text / .data / .bss` inside the
   reserved-memory region the overlay carved out:
   - `rproc@3ED00000` — size 0x40000, firmware text/data.
   - Keep stack ≥ 4 KB.
5. **main.c** sketch:

```c
#include "xgpio.h"
#include "xparameters.h"
#include "sleep.h"

int main(void) {
    XGpio led;
    XGpio_Initialize(&led, XPAR_AXI_GPIO_0_DEVICE_ID);
    XGpio_SetDataDirection(&led, 1, 0x0);   // channel 1, all outputs
    for (;;) {
        XGpio_DiscreteWrite(&led, 1, 0x1);  // bit 0 high
        sleep(1);
        XGpio_DiscreteWrite(&led, 1, 0x0);
        sleep(1);
    }
}
```

Build → produces `r5_blink.elf` (or similar).

### 5.2 Deploy to board

```sh
# on host
scp -i ~/.ssh/id_ed25519 r5_blink.elf petalinux@192.168.1.78:/tmp/

# on board
sudo cp /lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw \
        /lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw.echo.bak
sudo cp /tmp/r5_blink.elf /lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw
```

### 5.3 Start the R5

```sh
# Make sure APU isn't also driving the pin:
[ -d /sys/class/gpio/gpio504 ] && echo 504 | sudo tee /sys/class/gpio/unexport

# Boot R5:
echo start | sudo tee /sys/class/remoteproc/remoteproc0/state
cat /sys/class/remoteproc/remoteproc0/state     # should read "running"
dmesg | grep -i remoteproc | tail -20
```

Expect the LED to blink at 1 Hz, driven entirely by R5 — APU plays no
role once the ELF is loaded.

### 5.4 Stop / switch back

```sh
echo stop | sudo tee /sys/class/remoteproc/remoteproc0/state
# restore echo firmware if needed:
sudo cp /lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw.echo.bak \
        /lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw
```

> Memory note: **never `xmutil unloadapp` while the R5 is running** with
> the openamp overlay — `zynqmp_ipi_mailbox` UAF kernel oops. Stop
> remoteproc first, then unload.

---

## 6. Next step — rpmsg-triggered GPIO

Once the standalone blink is verified, extend the firmware to:

1. Keep the existing OpenAMP echo endpoint.
2. On each rpmsg message, treat the first byte as bit-mask: write
   `(mask & 0xF)` to `axi_gpio_0`.
3. APU side: `server.py` `trigger_alert()` writes `\x01` to
   `/dev/rpmsgN` on ENTER, `\x00` on CLEAR.

End state: zoneguard → Flask event bus → rpmsg → R5 → PMOD pin → relay /
buzzer / machinery-stop signal.

---

## 7. Troubleshooting quick-ref

| Symptom | Likely cause |
|---------|--------------|
| `loadapp` → `err=-22` | overlay redeclares root-level nodes (see §2.3 pitfall 1) |
| `rproc_boot` → `bad phdr da 0x0` | TCM node missing `power-domain` (§2.3 pitfall 2) |
| LED dim | wrong resistor / blue-white LED / XDC drive low |
| APU writes succeed but LED dead | IOSTANDARD wrong in XDC, or wrong FPGA ball in constraint |
| R5 starts then APU also drives pin | uncoordinated writes — stop APU side first |
| `listapps` shows Active_slot=-1 after failed load | kernel fpga_region latched; reboot |
