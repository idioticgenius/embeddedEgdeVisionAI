#!/bin/sh
# Blink axi_gpio_0 line on PMOD J2.
# Usage: ./blink_led.sh [gpio_num] [period_sec] [count]
#   gpio_num   : sysfs GPIO number (default 504 = axi_gpio_0 line 0 = J2 pin 1 / H12)
#   period_sec : full on+off cycle seconds (default 1.0)
#   count      : number of blinks, 0 = forever (default 0)

GPIO=${1:-504}
PERIOD=${2:-1.0}
COUNT=${3:-0}
HALF=$(awk "BEGIN{print $PERIOD/2}")

SYS=/sys/class/gpio
CHIP=$SYS/gpio$GPIO

if [ ! -d "$CHIP" ]; then
    echo "$GPIO" > $SYS/export || { echo "export failed"; exit 1; }
fi
echo out > $CHIP/direction

cleanup() {
    echo 0 > $CHIP/value 2>/dev/null
    echo "$GPIO" > $SYS/unexport 2>/dev/null
    exit 0
}
trap cleanup INT TERM

i=0
while :; do
    echo 1 > $CHIP/value
    sleep $HALF
    echo 0 > $CHIP/value
    sleep $HALF
    i=$((i+1))
    [ "$COUNT" -gt 0 ] && [ "$i" -ge "$COUNT" ] && break
done

cleanup
