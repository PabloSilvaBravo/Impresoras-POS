---
name: crash-sd-rpi-printer
description: Use when the Impresoras-POS Raspberry Pi 5 printer service becomes unreachable — symptoms include `print.mechatronicstore.cl` returning Cloudflare error 1033, SSH to `impresoras.local` / `192.168.1.176` timing out, solid green LED on the Pi, dashboard printing failing, or the user reporting "la Pi está caída / no imprime / no responde". Covers diagnosis (power vs network vs SD corruption), SD inspection from Mac, full reflash with state restoration from `~/impresoras-backup/`, cloudflared re-setup (reusing UUID when possible), and VPS token sync following the dashboard's strict config protocol.
---

# crash-sd-rpi-printer

Recuperación de la Raspberry Pi 5 que hospeda el servicio FastAPI + TSPL para la impresora térmica XP-420B, expuesto a internet vía Cloudflare Tunnel en `print.mechatronicstore.cl` y consumido desde el dashboard en la VPS `147.93.11.63`.

## Arquitectura de referencia

```
[Dashboard PHP · VPS 147.93.11.63 · docker dashboard-web]
        │  HTTPS Bearer Token
        ▼
[Cloudflare Tunnel · print.mechatronicstore.cl]
        ▼
[Raspberry Pi 5 · impresoras.local · 192.168.1.176]
  - Docker container impresoras-api (FastAPI, puerto 8000)
  - cloudflared como systemd service
  - /dev/usb/lp0 → Xprinter XP-420B
```

Estado que vive solo en la Pi (resto está en git):
- `~/Impresoras-POS/.env` con `API_TOKEN`
- `~/Impresoras-POS/api/config/printers.yml`
- `~/.cloudflared/cert.pem`
- `~/.cloudflared/<UUID>.json`

Backup canónico de esos 4 archivos: `~/impresoras-backup/` en la Mac del usuario.

## Fase 1 — Diagnóstico (desde Mac)

Pide al usuario correr esto y pegar el output antes de decidir camino:

```bash
curl -sS --max-time 5 https://print.mechatronicstore.cl/health; echo
ping -c 3 192.168.1.176
arp -a | grep -E "192\.168\.1\." | sort
```

Interpretación:

| `/health` | `ping .176` | ARP `.176` | Diagnóstico | Acción |
|-----------|-------------|------------|-------------|--------|
| `{"ok":true,...}` | responde | resuelto | Todo OK | No hacer nada. Escalar otra causa. |
| Falla | responde | resuelto | Pi viva, cloudflared caído | SSH a la Pi, revisar `sudo systemctl status cloudflared`. |
| Falla | 100% packet loss | resuelto | Wi-Fi on pero servicio caído | SSH y revisar docker + cloudflared. |
| Falla | 100% packet loss | `(incomplete)` | Pi fuera de la red | Fase 2 (inspección física + SD). |

Si Pi fuera de red, preguntar LEDs:
- **Rojo apagado** → problema de power. Fuente insuficiente o cable malo.
- **Verde parpadeando** durante boot → normal, espera 60s y reintenta.
- **Verde fijo desde el inicio del boot** → indicador clásico de rootfs corrupto. Saltar a Fase 3.

## Fase 2 — Inspección de SD desde Mac

Si la Pi no arranca, pedirle sacar la microSD y conectarla al Mac con adaptador.

```bash
diskutil list
ls /Volumes/
ls -la /Volumes/bootfs/ 2>/dev/null
```

- Si `bootfs` monta con `cmdline.txt`, `config.txt`, `kernel*.img` → la SD **no** está físicamente muerta. Casi seguro rootfs ext4 corrupto.
- La partición Linux (ext4) no monta nativamente en Mac — eso es normal, no es síntoma.
- Si ni `bootfs` aparece → SD físicamente dañada; hay que comprar otra.

Verificar integridad del boot:
```bash
cat /Volumes/bootfs/cmdline.txt
cat /Volumes/bootfs/config.txt
```

`network-config` y `user-data` contienen credenciales Wi-Fi y hash del usuario; no pedir que se peguen completos.

**Decisión:** si bootfs está íntegro pero la Pi no levanta → reflash (Fase 3). Intentar reparar ext4 desde Mac requiere macFUSE + ext4fuse y raramente vale la pena porque el proyecto es casi stateless.

## Fase 3 — Reflash con restauración de state

### 3.1. Flashear Pi OS Lite

1. Expulsar SD limpio:
   ```bash
   diskutil unmountDisk /dev/diskN   # el N que salió en diskutil list
   ```
2. Abrir **Raspberry Pi Imager** → Pi 5 → **Raspberry Pi OS Lite (64-bit)**.
3. **Edit Settings → General:**
   - Hostname: `impresoras`
   - Username: `pi`, **password real** (NO dejar vacío — bloquea SSH).
   - Wi-Fi: SSID `TP-Mechatronics` (o la que corresponda), country `CL`.
   - Locale: `America/Santiago`, keyboard `es`.
4. **Services:** habilitar SSH con password auth. Opcional: pegar `~/.ssh/id_ed25519.pub` para entrar sin password.
5. Escribir (~5-10 min).

### 3.2. Boot inicial

Cable USB-C con **fuente oficial Pi 5 (5V/5A)**, no del Mac. Idealmente Ethernet para descartar Wi-Fi. Esperar ~60s.

```bash
ssh-keygen -R impresoras.local
ssh-keygen -R 192.168.1.176
ping -c 3 impresoras.local
ssh pi@impresoras.local
```

### 3.3. Restaurar state ANTES de bootstrap (desde Mac, nueva pestaña)

```bash
scp ~/impresoras-backup/.env pi@impresoras.local:~/Impresoras-POS/.env 2>/dev/null || true
scp ~/impresoras-backup/printers.yml pi@impresoras.local:~/Impresoras-POS/api/config/printers.yml 2>/dev/null || true
ssh pi@impresoras.local 'mkdir -p ~/.cloudflared'
scp ~/impresoras-backup/cert.pem pi@impresoras.local:~/.cloudflared/
scp ~/impresoras-backup/*.json pi@impresoras.local:~/.cloudflared/
```

Los primeros dos fallan silenciosamente si el repo aún no está clonado — eso se arregla en el paso 3.4 (bootstrap) si antes clonamos el repo y luego sobrescribimos.

Orden correcto: clonar repo primero, restaurar `.env` y `printers.yml` sobre el clonado, luego correr bootstrap.

### 3.4. Clonar + restaurar + bootstrap

En la SSH a la Pi:

```bash
sudo apt update && sudo apt install -y git
cd ~
git clone https://github.com/PabloSilvaBravo/Impresoras-POS.git
cd Impresoras-POS
```

Si el repo está privado: ping al usuario para hacerlo público temporalmente (Settings → Change visibility → Public), clonar, y volver a privado después.

Después de clonar, pedir al usuario correr los `scp` de `.env` y `printers.yml` desde Mac (sobreescriben los del repo con el backup).

Luego en la Pi:
```bash
bash scripts/bootstrap.sh
```

Si ya existe `.env` con `API_TOKEN=xxx` no vacío, el bootstrap lo **respeta** (no sobreescribe). Eso preserva el token original, crítico para no tener que tocar la VPS.

### 3.5. Cloudflared

Si los archivos `cert.pem` y `<UUID>.json` se restauraron:

```bash
ARCH=$(dpkg --print-architecture)
curl -L -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb"
sudo dpkg -i /tmp/cloudflared.deb

# Recrear config.yml apuntando al UUID restaurado
TUNNEL_UUID=$(ls ~/.cloudflared/*.json | head -1 | xargs basename | sed 's/.json//')
echo "UUID: $TUNNEL_UUID"
cat > ~/.cloudflared/config.yml <<EOF
tunnel: ${TUNNEL_UUID}
credentials-file: /home/pi/.cloudflared/${TUNNEL_UUID}.json

ingress:
  - hostname: print.mechatronicstore.cl
    service: http://localhost:8000
  - service: http_status:404
EOF

sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/
sudo cp ~/.cloudflared/${TUNNEL_UUID}.json /etc/cloudflared/
sudo sed -i 's|/home/pi/.cloudflared/|/etc/cloudflared/|g' /etc/cloudflared/config.yml

sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl restart cloudflared
```

El túnel sigue ligado al mismo UUID → **no hay que recrear DNS**, sigue apuntando al mismo lugar.

Si NO hay backup del cert.pem (escenario peor): seguir flujo de setup inicial (`cloudflared tunnel login` → `cloudflared tunnel delete -f impresoras-pos` → `cloudflared tunnel create impresoras-pos` → nuevo UUID → `cloudflared tunnel route dns --overwrite-dns impresoras-pos print.mechatronicstore.cl`). En ese caso el token también cambió (ver fase 4).

### 3.6. Verificación

Desde la Pi:
```bash
curl -s http://localhost:8000/health
grep API_TOKEN ~/Impresoras-POS/.env
```

Desde Mac:
```bash
curl -s https://print.mechatronicstore.cl/health; echo
```

Ambos deben devolver `{"ok":true,"printers":1}`.

## Fase 4 — Sincronizar token en VPS (solo si cambió)

**Saltar si se restauró `.env` desde backup** (token no cambió).

Si hay token nuevo, respetar el **protocolo estricto** del dashboard (documentado en `/opt/dashboard/CLAUDE.md` en la VPS). NO editar directo el archivo vivo; NO usar `config.example.php` como base; NO reemplazar el persistente por un template.

Desde la Mac:
```bash
ssh root@147.93.11.63
```

En la VPS, reemplazar `<NUEVO_TOKEN>` con el valor real:
```bash
# 1. Backup timestamped del persistente
cp /opt/dashboard-configs/config.php /opt/dashboard-configs/config.php.pre-fix-$(date +%Y%m%d-%H%M%S)

# 2. Editar en /tmp (NO tocar el persistente aún)
cp /opt/dashboard-configs/config.php /tmp/config.php.new
sed -i "s|PRINT_API_TOKEN', '[^']*'|PRINT_API_TOKEN', '<NUEVO_TOKEN>'|" /tmp/config.php.new

# 3. Diff y syntax check (PHP vive dentro del contenedor)
diff /opt/dashboard-configs/config.php /tmp/config.php.new
docker cp /tmp/config.php.new dashboard-web:/tmp/check.php
docker exec dashboard-web php -l /tmp/check.php
docker exec dashboard-web rm /tmp/check.php
```

Solo si el diff muestra **únicamente** la línea de `PRINT_API_TOKEN` y el syntax check dice "No syntax errors detected":

```bash
# 4. Propagar a los 3 lugares
cp /tmp/config.php.new /opt/dashboard-configs/config.php
cp /tmp/config.php.new /opt/dashboard/api/config.php
docker cp /tmp/config.php.new dashboard-web:/var/www/html/api/config.php

# 5. Validador oficial
docker exec dashboard-web php /var/www/html/api/cli/validar_config_deploy.php

# 6. Smoke test end-to-end
docker exec dashboard-web sh -c 'TOKEN=$(php -r "require \"/var/www/html/api/config.php\"; echo PRINT_API_TOKEN;"); curl -sS -X POST https://print.mechatronicstore.cl/print/principal -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"copies\":1,\"elements\":[{\"type\":\"text\",\"x_mm\":3,\"y_mm\":3,\"content\":\"Recovery OK\"}]}"'
echo

# 7. Limpiar
rm /tmp/config.php.new
```

Validador debe salir 31/31 OK, 0 críticas. Smoke test debe imprimir físicamente "Recovery OK" en la XP-420B.

## Fase 5 — Re-crear backup del state

Después de cada cambio de token o UUID de túnel:

```bash
mkdir -p ~/impresoras-backup
scp pi@impresoras.local:~/Impresoras-POS/.env ~/impresoras-backup/
scp pi@impresoras.local:~/Impresoras-POS/api/config/printers.yml ~/impresoras-backup/
scp pi@impresoras.local:~/.cloudflared/cert.pem ~/impresoras-backup/
scp pi@impresoras.local:'~/.cloudflared/*.json' ~/impresoras-backup/
ls -la ~/impresoras-backup/
```

## Prevención (tras cada recuperación, recordar al usuario)

1. **Fuente oficial Pi 5 (5V/5A USB-C)**. El 90% de las corrupciones de SD son por under-voltage de fuentes insuficientes.
2. **Apagado ordenado** antes de mover o desconectar: click corto del botón power de la Pi 5 (al lado del USB-C) → esperar 15s a que los LEDs se apaguen → recién ahí desenchufar. Equivalente a `sudo shutdown -h now`. **Nunca** mantener 5+ seg ni pulling the plug.
3. **Backup actualizado** en `~/impresoras-backup/` tras cualquier cambio estructural.
4. (Opcional avanzado) Rootfs read-only con overlay filesystem para que cortes de luz sean inocuos.

## Gotchas conocidos

- Imager con **password vacío** bloquea SSH (cloud-init crea usuario sin password válido) → re-flashear.
- Repo público vs privado: si quedó privado, `git clone` por HTTPS pide credenciales. Hacer público temporal o usar PAT/SSH key.
- mDNS (`impresoras.local`) puede tardar 30-60s tras boot en resolver. Si no resuelve, usar IP directa (`192.168.1.176` típicamente por DHCP reservado).
- `docker` sin `sg docker -c` falla hasta relogin; el bootstrap usa `sg docker` internamente para evitarlo.
- La VPS bloquea `php` en el host; todos los syntax checks van vía `docker exec dashboard-web php -l ...`.
- El hook post-deploy del dashboard restaura `config.php` desde `/opt/dashboard-configs/config.php` en cada push → **siempre** hay que actualizar el persistente, no solo el vivo.

## Flujo completo de "rescate rápido" (cuando hay backup)

Tiempo estimado: ~15-20 min.

1. Flashear SD con Imager (5-10 min).
2. SSH, `apt install git`, clonar repo.
3. `scp` de los 4 archivos de backup.
4. `bash scripts/bootstrap.sh`.
5. Reinstalar cloudflared `.deb` + `sudo cloudflared service install`.
6. Verificar `/health` local y público.
7. Listo — VPS intacta.

Sin backup: ~45 min (flashear + bootstrap + cloudflared login + tunnel create + DNS route + VPS token sync).
