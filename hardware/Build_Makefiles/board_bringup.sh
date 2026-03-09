#!/bin/bash
# board_bringup.sh — run ON the board as root (or with sudo)
# Loads kernel modules, verifies DT, loads R5 firmware, starts R5.
# Prerequisite: /lib/firmware/image_echo_test must exist (see note below).

set -e

FW=image_echo_test
FW_PATH=/lib/firmware/${FW}
RPROC=/sys/class/remoteproc/remoteproc0

echo "=== 1. Check firmware ELF ==="
if [ ! -f "$FW_PATH" ]; then
  echo "ERROR: $FW_PATH not found."
  echo ""
  echo "Get it one of two ways:"
  echo "  A) Vitis 2022.2 → New App Project → psu_cortexr5_0 → OpenAMP echo-test"
  echo "     Build → copy Debug/<project>.elf → rename to image_echo_test → scp here"
  echo "  B) From the PetaLinux 2022.2 rootfs build (openamp-fw-echo-testd recipe)"
  exit 1
fi
ls -lh "$FW_PATH"

echo ""
echo "=== 2. Verify R5 node in live DT ==="
if [ -d /sys/firmware/devicetree/base ]; then
  dtc -I fs /sys/firmware/devicetree/base 2>/dev/null | grep -q rf5ss \
    && echo "OK — rf5ss node present" \
    || echo "MISSING — DTBO may not have applied. Check xmutil listapps."
fi

echo ""
echo "=== 3. Load kernel modules ==="
for mod in zynqmp_r5_remoteproc virtio_rpmsg_bus rpmsg_char rpmsg_ns; do
  modprobe $mod && echo "  loaded: $mod" || echo "  WARN: $mod already loaded or failed"
done

echo ""
echo "=== 4. Check remoteproc device ==="
if [ ! -d "$RPROC" ]; then
  echo "ERROR: $RPROC does not exist."
  echo "Most likely cause: rf5ss DT node missing or zynqmp_r5_remoteproc didn't bind."
  echo "Run: dmesg | grep -iE 'remoteproc|r5|rpmsg' for details."
  exit 1
fi
echo "Found: $RPROC"
echo "State: $(cat ${RPROC}/state)"

echo ""
echo "=== 5. Load firmware and start R5 ==="
echo "$FW" > ${RPROC}/firmware
echo "Firmware set to: $(cat ${RPROC}/firmware)"

echo start > ${RPROC}/state
sleep 1
echo "State after start: $(cat ${RPROC}/state)"

echo ""
echo "=== 6. Check rpmsg endpoints ==="
dmesg | tail -20 | grep -iE "remoteproc|rpmsg|virtio|r5f" || true
echo ""
ls /dev/rpmsg* 2>/dev/null && echo "rpmsg endpoints ready" || echo "WARN: no /dev/rpmsg* yet — check dmesg"

echo ""
echo "=== Done ==="
echo "If /dev/rpmsg0 exists, run: python3 rpu_test.py"
echo "Or run the built-in echo test: sudo echo_test"
