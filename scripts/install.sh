#!/usr/bin/env bash
# Instalador remoto one-liner para la Raspberry Pi.
#
# Uso (en la Pi, por SSH):
#   curl -fsSL https://raw.githubusercontent.com/PabloSilvaBravo/Impresoras-POS/main/scripts/install.sh | bash
#
# Variables opcionales:
#   REPO_URL   URL del repo git (default: https://github.com/PabloSilvaBravo/Impresoras-POS.git)
#   REPO_REF   Rama o tag a clonar (default: main)
#   REPO_DIR   Directorio destino (default: $HOME/Impresoras-POS)

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/PabloSilvaBravo/Impresoras-POS.git}"
REPO_REF="${REPO_REF:-main}"
REPO_DIR="${REPO_DIR:-$HOME/Impresoras-POS}"

log() { printf "\033[1;34m[install]\033[0m %s\n" "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Ejecuta como tu usuario normal (no root)." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  log "Instalando git..."
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get -y -qq install git >/dev/null
fi

if [[ -d "$REPO_DIR/.git" ]]; then
  log "Repo ya existe en $REPO_DIR, actualizando..."
  git -C "$REPO_DIR" fetch --all --prune
  git -C "$REPO_DIR" checkout "$REPO_REF"
  git -C "$REPO_DIR" pull --ff-only
else
  log "Clonando $REPO_URL ($REPO_REF) en $REPO_DIR..."
  git clone --branch "$REPO_REF" "$REPO_URL" "$REPO_DIR"
fi

log "Ejecutando bootstrap..."
exec "$REPO_DIR/scripts/bootstrap.sh"
