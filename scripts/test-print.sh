#!/usr/bin/env bash
# Envía una etiqueta de prueba al endpoint /print/<printer_id>.
#
# Uso:
#   ./scripts/test-print.sh [printer_id] [host]
#
# Variables:
#   API_TOKEN   Token Bearer si tu .env lo define.

set -euo pipefail

PRINTER_ID="${1:-principal}"
HOST="${2:-http://localhost:8000}"
TOKEN="${API_TOKEN:-}"

AUTH=()
if [[ -n "$TOKEN" ]]; then
  AUTH=(-H "Authorization: Bearer $TOKEN")
fi

read -r -d '' BODY <<'JSON' || true
{
  "width_mm": 100,
  "height_mm": 60,
  "gap_mm": 2,
  "copies": 1,
  "elements": [
    {"type": "text",    "x_mm": 5,  "y_mm": 5,  "content": "Impresoras POS", "font": "4"},
    {"type": "text",    "x_mm": 5,  "y_mm": 18, "content": "Prueba XP-420B"},
    {"type": "barcode", "x_mm": 5,  "y_mm": 28, "content": "POS-000001", "symbology": "128", "height_mm": 12},
    {"type": "qrcode",  "x_mm": 70, "y_mm": 28, "content": "https://example.com/pedido/1", "cell_width": 4},
    {"type": "line",    "x_mm": 5,  "y_mm": 50, "width_mm": 90, "thickness_mm": 0.4}
  ]
}
JSON

curl -sS -X POST "$HOST/print/$PRINTER_ID" \
  -H "Content-Type: application/json" \
  "${AUTH[@]}" \
  -d "$BODY"
echo
