#!/usr/bin/env bash
# Muestra las impresoras USB conectadas con la información necesaria
# para crear reglas udev (vendor, product, serial) y ver el /dev asignado.

set -euo pipefail

echo "== lsusb =="
lsusb

echo
echo "== Dispositivos /dev/usb/lp* =="
ls -l /dev/usb/lp* 2>/dev/null || echo "(no hay ninguno)"

echo
for dev in /dev/usb/lp*; do
  [[ -e "$dev" ]] || continue
  echo "== $dev =="
  udevadm info -a -n "$dev" 2>/dev/null | \
    grep -E 'idVendor|idProduct|serial|manufacturer|product' | \
    head -n 15
  echo
done
