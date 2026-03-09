#!/bin/bash
# deploy_openamp.sh — host side
# Compiles the modified dtsi → dtbo and deploys it to the board.
# Run from the directory that contains kv260-multichannel-openamp.dtsi
# Usage: ./deploy_openamp.sh [board_ip]

BOARD=${1:-192.168.1.78}
BOARD_USER=petalinux
DTSI=kv260-multichannel-openamp.dtsi
DTBO=kv260-multichannel-openamp.dtbo
REMOTE_DTBO=/lib/firmware/xilinx/kv260-multichannel/kv260-multichannel.dtbo

set -e

echo "=== 1. Compile DTSI → DTBO ==="
dtc -O dtb -@ -o "$DTBO" "$DTSI" 2>&1 | grep -v Warning || true
ls -lh "$DTBO"

echo ""
echo "=== 2. Backup existing DTBO on board ==="
ssh ${BOARD_USER}@${BOARD} \
  "sudo cp ${REMOTE_DTBO} ${REMOTE_DTBO}.bak && echo backed up"

echo ""
echo "=== 3. Unload multichannel app ==="
ssh ${BOARD_USER}@${BOARD} \
  "sudo xmutil unloadapp && echo unloaded"

echo ""
echo "=== 4. Copy new DTBO to board ==="
scp "$DTBO" ${BOARD_USER}@${BOARD}:/tmp/
ssh ${BOARD_USER}@${BOARD} \
  "sudo cp /tmp/${DTBO} ${REMOTE_DTBO} && echo installed"

echo ""
echo "=== 5. Reload multichannel app (now with OpenAMP nodes) ==="
ssh ${BOARD_USER}@${BOARD} \
  "sudo xmutil loadapp kv260-multichannel && echo loaded"

echo ""
echo "=== 6. Verify R5 node is in the live DT ==="
ssh ${BOARD_USER}@${BOARD} \
  "dtc -I fs /sys/firmware/devicetree/base 2>/dev/null | grep -c rf5ss && echo 'rf5ss node found in live DT' || echo 'MISSING — check overlay'"

echo ""
echo "Done. Next: run board_bringup.sh on the board."
