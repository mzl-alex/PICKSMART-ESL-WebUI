#!/usr/bin/env bash
# setup_bt.sh – Attach a serial Bluetooth HCI controller to the Linux BT stack
#
# Use this if your Bluetooth adapter appears as a serial port (e.g. an ESP32
# flashed with a BT HCI firmware, or a hardware UART BT module).
#
# Usage:
#   ./setup_bt.sh [device] [baud]
#
# Defaults:
#   device : /dev/ttyUSB0
#   baud   : 115200

set -euo pipefail

DEVICE="${1:-/dev/ttyUSB0}"
BAUD="${2:-115200}"

if [ ! -e "$DEVICE" ]; then
  echo "ERROR: $DEVICE not found."
  echo "       Available serial devices:"
  ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "       (none)"
  exit 1
fi

# Kill any existing hciattach for this device
pkill -f "hciattach $DEVICE" 2>/dev/null || true
sleep 0.5

echo "Attaching $DEVICE at $BAUD baud as hci0 …"
hciattach "$DEVICE" any "$BAUD" flow

echo ""
echo "Result:"
hciconfig
