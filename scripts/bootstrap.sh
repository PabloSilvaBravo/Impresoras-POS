#!/usr/bin/env bash
# Bootstrap idempotente para Raspberry Pi OS Lite (64-bit) en una Raspberry Pi 5.
# Instala Docker + Compose, carga usblp, instala udev, genera .env con un
# API_TOKEN aleatorio, levanta el stack con docker compose y espera a que
# /health responda. Pensado para ejecutarse sin intervención.
#
# Uso (desde la Pi, conectado por SSH):
#   cd ~/Impresoras-POS
#   ./scripts/bootstrap.sh

set -euo pipefail

log()  { printf "\033[1;34m[bootstrap]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[bootstrap]\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[1;31m[bootstrap]\033[0m %s\n" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "Ejecuta este script con tu usuario normal (no root). Usará sudo cuando lo necesite."

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

log "Actualizando paquetes base del sistema..."
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get -y -qq install ca-certificates curl gnupg lsb-release git usbutils openssl >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  log "Instalando Docker Engine..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
else
  log "Docker ya está instalado."
  sudo usermod -aG docker "$USER" || true
fi

log "Habilitando el módulo usblp (crea /dev/usb/lpX para impresoras USB)..."
sudo modprobe usblp 2>/dev/null || warn "No se pudo cargar usblp en caliente (quizás ya estaba)."
echo usblp | sudo tee /etc/modules-load.d/usblp.conf >/dev/null

log "Instalando regla udev para permisos de impresoras USB..."
sudo install -m 0644 "$REPO_DIR/scripts/99-impresoras-pos.rules" /etc/udev/rules.d/99-impresoras-pos.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb || true

if [[ ! -f "$REPO_DIR/.env" ]]; then
  log "Creando .env con un API_TOKEN aleatorio."
  TOKEN="$(openssl rand -hex 24)"
  {
    echo "API_PORT=8000"
    echo "API_TOKEN=${TOKEN}"
    echo "CONFIG_PATH=/app/config/printers.yml"
    echo "LOG_LEVEL=info"
  } > "$REPO_DIR/.env"
else
  log ".env ya existe, no se toca."
fi

if [[ ! -f "$REPO_DIR/api/config/printers.yml" ]]; then
  log "Creando api/config/printers.yml desde el ejemplo."
  cp "$REPO_DIR/api/config/printers.example.yml" "$REPO_DIR/api/config/printers.yml"
fi

log "Impresoras USB detectadas:"
lsusb | grep -iE 'printer|xprinter|zebra|epson|star|thermal' || lsusb | head -n 20
if ls /dev/usb/lp* >/dev/null 2>&1; then
  ls -l /dev/usb/lp*
else
  warn "No se detectaron /dev/usb/lpX (¿impresora conectada y encendida?). Puedes continuar y conectarla después."
fi

log "Levantando el servicio con docker compose..."
# Usamos `sg docker -c` para tomar la membresía del grupo docker sin
# necesidad de cerrar sesión.
sg docker -c "cd '$REPO_DIR' && docker compose up -d --build"

log "Esperando a que /health responda..."
for i in {1..30}; do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    log "Servicio respondiendo OK en http://localhost:8000"
    break
  fi
  sleep 2
  if [[ $i -eq 30 ]]; then
    warn "El servicio no respondió en 60s. Revisa: sg docker -c 'docker compose logs -f api'"
  fi
done

# shellcheck disable=SC1091
API_TOKEN_VALUE="$(grep '^API_TOKEN=' "$REPO_DIR/.env" | cut -d= -f2-)"
IP="$(hostname -I | awk '{print $1}')"

cat <<EOF

─────────────────────────────────────────────────────────────
 Impresoras-POS listo
─────────────────────────────────────────────────────────────
 URL local:   http://localhost:8000
 URL en LAN:  http://${IP}:8000
 Hostname:    http://$(hostname).local:8000
 API_TOKEN:   ${API_TOKEN_VALUE}

 Probar:
   curl -H "Authorization: Bearer ${API_TOKEN_VALUE}" \\
        http://localhost:8000/printers

 Imprimir etiqueta de prueba:
   API_TOKEN=${API_TOKEN_VALUE} ./scripts/test-print.sh

 Logs:
   sg docker -c 'docker compose logs -f api'
─────────────────────────────────────────────────────────────
EOF
