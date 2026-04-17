# Impresoras-POS

Servicio HTTP en una Raspberry Pi 5 para imprimir etiquetas en impresoras
térmicas **Xprinter XP-420B** (u otras compatibles con TSPL) vía API /
webhook. Preparado para crecer a varias impresoras USB conectadas a la misma
Pi.

Stack:

- Raspberry Pi OS Lite (64-bit), headless.
- Docker + Docker Compose.
- FastAPI + generador de comandos **TSPL** nativo de la XP-420B.
- Acceso directo al dispositivo USB (`/dev/usb/lpX`) — sin CUPS.

---

## 1. Flashear la Raspberry Pi desde tu Mac (sin pantalla)

1. Instala **Raspberry Pi Imager** en el Mac:
   `brew install --cask raspberry-pi-imager` (o descárgalo desde
   raspberrypi.com).
2. Inserta la microSD en el Mac.
3. Abre Raspberry Pi Imager y elige:
   - **Dispositivo:** Raspberry Pi 5
   - **Sistema:** Raspberry Pi OS **Lite (64-bit)**
   - **Almacenamiento:** tu microSD
4. Pulsa **"Siguiente" → "Editar ajustes"** y configura:
   - **Hostname:** `impresoras` (será `impresoras.local`).
   - **Usuario / contraseña:** por ejemplo `pi` y una contraseña fuerte.
   - **Wi-Fi:** SSID y contraseña de tu red (country: CL o el que
     corresponda). Si vas a usar Ethernet puedes saltarlo.
   - **Locale:** tu zona horaria y teclado.
   - **Pestaña Services:** habilita **SSH** (con contraseña, o mejor con
     clave pública si ya tienes una `~/.ssh/id_*.pub`).
5. Guarda, escribe la tarjeta y espera a que termine.
6. Inserta la microSD en la Pi, conecta la impresora XP-420B por USB, y
   alimenta la Pi. Espera ~1 minuto a que bootee y se conecte al Wi-Fi.

### Conectarte por SSH

Desde el Mac:

```bash
ssh pi@impresoras.local
```

Si `impresoras.local` no resuelve:

- Conéctala por cable Ethernet al router, o
- busca la IP en tu router / `arp -a`, y usa `ssh pi@<ip>`.

---

## 2. Clonar el repo y ejecutar el bootstrap

Dentro de la Pi (por SSH):

```bash
sudo apt-get update && sudo apt-get -y install git
git clone https://github.com/PabloSilvaBravo/Impresoras-POS.git
cd Impresoras-POS
./scripts/bootstrap.sh
```

El script hará:

- `apt upgrade` básico e instala `curl`, `git`, `usbutils`.
- Instala Docker Engine + Compose (oficial).
- Agrega tu usuario al grupo `docker`.
- Carga el módulo del kernel `usblp` (crea `/dev/usb/lp0`, `/dev/usb/lp1`, …).
- Instala una regla udev con permisos para impresoras USB.
- Copia `.env.example` → `.env` y `api/config/printers.example.yml` →
  `api/config/printers.yml`.

**Cierra la sesión SSH y vuelve a entrar** (`exit` y `ssh` de nuevo) para
que tome efecto el grupo `docker`.

---

## 3. Configurar tus impresoras

1. Verifica que la Pi ve la XP-420B:

   ```bash
   ./scripts/detect-printers.sh
   ```

   Deberías ver algo en `lsusb` y `/dev/usb/lp0` presente.

2. Edita `api/config/printers.yml` con los datos reales (ya hay un ejemplo
   con `id: principal` y `device: /dev/usb/lp0`). Para más impresoras a
   futuro, duplica el bloque cambiando el `id` y el `device` (`/dev/usb/lp1`,
   etc.).

3. Edita `.env` y cambia `API_TOKEN` por un token propio (deja vacío para
   desactivar la autenticación, sólo recomendable en red privada).

### (Opcional) Nombres estables por impresora

Si vas a conectar varias XP-420B iguales, el orden de `lp0/lp1` puede
cambiar entre reinicios. Para fijarlos:

1. Conecta **sólo una** y corre `./scripts/detect-printers.sh`. Copia
   `idVendor`, `idProduct` y `serial`.
2. Edita `/etc/udev/rules.d/99-impresoras-pos.rules` (ya instalado por el
   bootstrap) y descomenta la línea `SYMLINK+="usb/xprinter-principal"`
   reemplazando los `XXXX/YYYY/ZZZZ`.
3. `sudo udevadm control --reload-rules && sudo udevadm trigger`.
4. En `printers.yml`, usa `device: /dev/usb/xprinter-principal`.

---

## 4. Levantar el servicio

```bash
docker compose up -d --build
docker compose logs -f api      # opcional
curl http://localhost:8000/health
```

Desde tu Mac u otro equipo en la red:

```bash
curl http://impresoras.local:8000/health
```

---

## 5. Imprimir

### Prueba rápida

```bash
API_TOKEN=<tu token> ./scripts/test-print.sh principal http://impresoras.local:8000
```

### API

Todas las rutas aceptan `Authorization: Bearer <API_TOKEN>` si lo
configuraste.

- `GET /health` — estado del servicio.
- `GET /printers` — impresoras configuradas y si están conectadas.
- `GET /printers/discover` — lista los `/dev/usb/lpX` visibles.
- `POST /print/{printer_id}` — imprime una etiqueta estructurada.
- `POST /print/{printer_id}/raw` — envía comandos TSPL crudos (útil para
  diseños avanzados).

Estructura de `POST /print/{printer_id}`:

```json
{
  "width_mm": 100,
  "height_mm": 60,
  "gap_mm": 2,
  "copies": 1,
  "elements": [
    {"type": "text",    "x_mm": 5,  "y_mm": 5,  "content": "Pedido #123", "font": "4"},
    {"type": "barcode", "x_mm": 5,  "y_mm": 20, "content": "POS-000123", "symbology": "128", "height_mm": 12},
    {"type": "qrcode",  "x_mm": 70, "y_mm": 20, "content": "https://tu-dominio/pedido/123", "cell_width": 4},
    {"type": "line",    "x_mm": 5,  "y_mm": 45, "width_mm": 90, "thickness_mm": 0.4},
    {"type": "box",     "x_mm": 5,  "y_mm": 50, "width_mm": 90, "height_mm": 5, "thickness": 2}
  ]
}
```

Tipos de elementos soportados: `text`, `barcode`, `qrcode`, `line`, `box`.
Coordenadas y medidas siempre en **milímetros** — el API convierte a
puntos según `dots_per_mm` de cada impresora (XP-420B = 8 dots/mm = 203 dpi).

### Webhook desde otro sistema

Apunta tu POS / ERP / Zapier / n8n a:

```
POST http://<ip-pi>:8000/print/principal
Authorization: Bearer <API_TOKEN>
Content-Type: application/json
```

con el mismo JSON de arriba.

---

## 6. Agregar más impresoras a futuro

1. Conecta la nueva impresora a un puerto USB libre.
2. `./scripts/detect-printers.sh` para ver el nuevo `/dev/usb/lpX` (o el
   symlink estable si configuraste udev).
3. Añade otro bloque en `api/config/printers.yml` con un `id` nuevo.
4. `docker compose restart api`.

No necesitas rebuildear la imagen Docker — el `/dev` del host está montado
y el contenedor ve los dispositivos nuevos al reiniciar el servicio.

---

## 7. Operación y mantenimiento

- Logs del servicio: `docker compose logs -f api`.
- Actualizar código: `git pull && docker compose up -d --build`.
- Autoarranque: Docker ya está configurado para iniciar al bootear; el
  servicio usa `restart: unless-stopped`.
- Acceso remoto seguro: si vas a exponer el puerto 8000 fuera de la LAN,
  ponlo detrás de un Cloudflare Tunnel o un reverse proxy con TLS
  (Caddy/Traefik) en vez de abrirlo directo a internet.

---

## 8. Troubleshooting

| Síntoma | Qué revisar |
|--------|-------------|
| `GET /printers` dice `available: false` | `ls /dev/usb/lp*` en la Pi. Si no aparece, revisa cable/USB y `sudo modprobe usblp`. |
| `curl` se queda colgado | La impresora está sin papel o apagada. |
| Imprime caracteres raros | Confirma que la XP-420B está en modo **TSPL** (no ESC/POS). Algunos modelos se cambian por software con la utilidad del fabricante en Windows. |
| Cambió `lp0` ↔ `lp1` tras reiniciar | Usa el symlink udev estable descrito en la sección 3. |
| Permiso denegado al escribir al device | Revisa que el bootstrap haya instalado `/etc/udev/rules.d/99-impresoras-pos.rules` y reinicia. |
