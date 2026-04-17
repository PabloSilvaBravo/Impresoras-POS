#!/usr/bin/env bash
# Bootstrap para Raspberry Pi OS Lite (64-bit) en una Raspberry Pi 5.
# Instala Docker + Docker Compose, carga el módulo usblp y deja el
# repositorio listo para ejecutar `docker compose up -d`.
#
# Uso (desde la Pi, conectado por SSH):
#   cd ~/Impresoras-POS
#   ./scripts/bootstrap.sh

set -euo pipefail

log() { printf "\033[1;34m[bootstrap]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[bootstrap]\033[0m %s\n" "$*" >&2; }

if [[ $EUID -eq 0 ]]; then
  warn "Ejecuta este script con tu usuario normal (no root). Usará sudo cuando lo necesite."
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

log "Actualizando paquetes base del sistema..."
sudo apt-get update
sudo apt-get -y upgrade
sudo apt-get -y install ca-certificates curl gnupg lsb-release git usbutils

if ! command -v docker >/dev/null 2>&1; then
  log "Instalando Docker Engine..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  log "Se agregó $USER al grupo docker. Deberás cerrar sesión y volver a entrar para aplicar."
else
  log "Docker ya está instalado."
fi

log "Habilitando el módulo usblp (crea /dev/usb/lpX para impresoras USB)..."
if ! lsmod | grep -q '^usblp'; then
  sudo modprobe usblp || warn "No se pudo cargar usblp en caliente."
fi
echo usblp | sudo tee /etc/modules-load.d/usblp.conf >/dev/null

log "Instalando regla udev para permisos de impresoras USB..."
sudo install -m 0644 "$REPO_DIR/scripts/99-impresoras-pos.rules" /etc/udev/rules.d/99-impresoras-pos.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb || true

if [[ ! -f "$REPO_DIR/.env" ]]; then
  log "Creando .env desde .env.example (recuerda cambiar API_TOKEN)."
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
fi

if [[ ! -f "$REPO_DIR/api/config/printers.yml" ]]; then
  log "Creando api/config/printers.yml desde el ejemplo."
  cp "$REPO_DIR/api/config/printers.example.yml" "$REPO_DIR/api/config/printers.yml"
fi

log "Detectando impresoras USB conectadas:"
lsusb || true
ls -l /dev/usb/lp* 2>/dev/null || warn "No se detectaron /dev/usb/lpX (¿impresora conectada y encendida?)"

log "Listo. Próximos pasos:"
cat <<'EOF'
  1) Revisa y ajusta api/config/printers.yml (id, device, medidas).
  2) Si Docker recién se instaló, cierra sesión y vuelve a entrar (ssh de nuevo)
     para que tu usuario tome el grupo docker.
  3) Levanta el servicio:
        docker compose up -d --build
  4) Prueba:
        curl http://localhost:8000/health
EOF
