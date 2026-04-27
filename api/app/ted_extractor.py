"""
TED Extractor - Extrae el Timbre Electrónico SII (PDF417) del PDF generado por
Bsale y lo convierte en bitmap raster ESC/POS para imprimir en POS-8370.

Workflow:
    1. download_pdf(url)             → bytes del PDF oficial Bsale
    2. extract_ted_image(pdf, tpl)   → PIL.Image B/N 384px wide del TED recortado
    3. escpos_raster_bytes(img)      → bytes ESC/POS GS v 0 listos para enviar

Cache por venta_id en disk (/tmp/ted_cache/{venta_id}.png) con TTL de 7 días.
Concurrencia limitada con Semaphore para no agotar memoria de la Raspberry Pi.

Coordenadas iniciales (TED_ROI_BY_TEMPLATE) son aproximadas. Se ajustan por
template tras inspeccionar PDFs reales de Bsale. Override por env var
TED_ROI_JSON o por TedSpec.roi del request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image
from pdf2image import convert_from_bytes

log = logging.getLogger(__name__)

# ── Configuración via env vars ──────────────────────────────────────────────

CACHE_DIR     = Path(os.environ.get("TED_CACHE_DIR", "/tmp/ted_cache"))
CACHE_TTL_DAYS = int(os.environ.get("TED_CACHE_TTL_DAYS", "7"))
RENDER_DPI    = int(os.environ.get("TED_RENDER_DPI", "300"))
TARGET_WIDTH  = int(os.environ.get("TED_TARGET_WIDTH_PX", "384"))
DOWNLOAD_TIMEOUT = float(os.environ.get("TED_DOWNLOAD_TIMEOUT", "8"))
MAX_CONCURRENT = int(os.environ.get("TED_MAX_CONCURRENT", "2"))

# Coordenadas (x, y, w, h) fraccionales [0..1] del TED en la página 1 del PDF.
# Default conservador — ajustar tras inspeccionar PDFs reales de Bsale.
# El TED suele estar en el tercio inferior izquierdo de boleta y factura.
_DEFAULT_ROI = {
    "boleta":  (0.04, 0.78, 0.48, 0.19),
    "factura": (0.04, 0.78, 0.48, 0.19),
}

def _load_roi_overrides() -> dict[str, tuple[float, float, float, float]]:
    """Lee override de env var TED_ROI_JSON si existe."""
    raw = os.environ.get("TED_ROI_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {k: tuple(v) for k, v in data.items() if isinstance(v, (list, tuple)) and len(v) == 4}
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"TED_ROI_JSON inválido, ignorado: {e}")
        return {}

TED_ROI_BY_TEMPLATE = {**_DEFAULT_ROI, **_load_roi_overrides()}

# Concurrencia para evitar OOM en la Pi (cada render 300dpi ~= 25MB RAM)
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# ── Cache ───────────────────────────────────────────────────────────────────

def _cache_path(venta_id: int, target_width: int | None = None) -> Path:
    """
    Path del PNG cacheado. Si hay target_width distinto del default global,
    se incluye en el nombre del archivo para no mezclar bitmaps de tamaños
    distintos (ej. boleta chica vs factura grande del mismo venta_id).
    """
    if target_width and target_width != TARGET_WIDTH:
        return CACHE_DIR / f"{venta_id}_w{target_width}.png"
    return CACHE_DIR / f"{venta_id}.png"

def _cache_valid(path: Path) -> bool:
    if not path.exists():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return age_days < CACHE_TTL_DAYS

def cleanup_cache(max_files: int = 500) -> int:
    """Borra archivos de cache vencidos y mantiene como mucho max_files (LRU)."""
    if not CACHE_DIR.exists():
        return 0
    cutoff = time.time() - CACHE_TTL_DAYS * 86400
    removed = 0
    files = sorted(CACHE_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for i, p in enumerate(files):
        if i >= max_files or p.stat().st_mtime < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed

# ── Pipeline principal ──────────────────────────────────────────────────────

async def download_pdf(url: str) -> bytes:
    """Descarga el PDF de Bsale. Lanza httpx.HTTPError en falla."""
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content

def extract_ted_image(
    pdf_bytes: bytes,
    template: str,
    roi: tuple[float, float, float, float] | None = None,
    target_width: int | None = None,
) -> Image.Image:
    """
    Renderiza la pág. 1 del PDF, recorta el ROI del TED y lo prepara para
    impresión raster: ancho `target_width` px (default TED_TARGET_WIDTH_PX),
    1-bit con Floyd-Steinberg dither.
    """
    if roi is None:
        roi = TED_ROI_BY_TEMPLATE.get(template, _DEFAULT_ROI["boleta"])
    tw = target_width if target_width else TARGET_WIDTH

    pages = convert_from_bytes(pdf_bytes, dpi=RENDER_DPI, first_page=1, last_page=1)
    if not pages:
        raise ValueError("PDF sin páginas")
    page = pages[0]

    W, H = page.size
    x, y, w, h = roi
    box = (
        max(0, int(W * x)),
        max(0, int(H * y)),
        min(W, int(W * (x + w))),
        min(H, int(H * (y + h))),
    )
    cropped = page.crop(box)

    # Resize manteniendo aspect ratio a `tw` de ancho
    src_w, src_h = cropped.size
    scale = tw / src_w
    new_h = int(src_h * scale)
    cropped = cropped.resize((tw, new_h), Image.LANCZOS)

    # 1-bit con dither Floyd-Steinberg para preservar el código de barras
    bw = cropped.convert("1", dither=Image.FLOYDSTEINBERG)
    return bw

async def get_or_cache(
    venta_id: int,
    pdf_url: str,
    template: str,
    target_width: int | None = None,
) -> Path:
    """
    Devuelve el path al PNG cacheado del TED. Si no existe o venció, lo genera.
    El cache key incluye target_width cuando hay override (así boletas y facturas
    con tamaños distintos no comparten cache).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(venta_id, target_width)
    if _cache_valid(path):
        return path

    async with _semaphore:
        if _cache_valid(path):
            return path
        log.info(f"TED extracting venta_id={venta_id} template={template} tw={target_width or TARGET_WIDTH}")
        pdf_bytes = await download_pdf(pdf_url)
        loop = asyncio.get_event_loop()
        img = await loop.run_in_executor(None, extract_ted_image, pdf_bytes, template, None, target_width)
        img.save(path, format="PNG")
        log.info(f"TED cached venta_id={venta_id} path={path} size={path.stat().st_size}")
        return path

# ── ESC/POS raster encoder ──────────────────────────────────────────────────

def escpos_raster_bytes(img: Image.Image) -> bytes:
    """
    Convierte una imagen 1-bit a comando ESC/POS GS v 0:
      GS v 0 m xL xH yL yH d1...dn
    donde m=0 (modo normal), xL+xH*256 = width_bytes, yL+yH*256 = height.
    Cada bit del data = 1 pixel (1=negro, 0=blanco).
    """
    if img.mode != "1":
        img = img.convert("1")

    w, h = img.size
    # ESC/POS exige width múltiplo de 8 dots (1 byte por 8 pixels horizontales)
    if w % 8 != 0:
        # Pad con blancos a la derecha
        new_w = (w // 8 + 1) * 8
        padded = Image.new("1", (new_w, h), color=1)  # blanco
        padded.paste(img, (0, 0))
        img = padded
        w = new_w

    width_bytes = w // 8
    xL = width_bytes & 0xFF
    xH = (width_bytes >> 8) & 0xFF
    yL = h & 0xFF
    yH = (h >> 8) & 0xFF

    # Convertir pixels a bytes. PIL en modo "1" tiene 0=negro, 255=blanco;
    # ESC/POS espera 1=negro, 0=blanco. Invertir.
    pixels = img.load()
    data = bytearray()
    for y in range(h):
        for byte_x in range(width_bytes):
            byte = 0
            for bit in range(8):
                px = pixels[byte_x * 8 + bit, y]
                if px == 0:  # negro en PIL "1"
                    byte |= (0x80 >> bit)
            data.append(byte)

    header = bytes([0x1D, 0x76, 0x30, 0x00, xL, xH, yL, yH])
    return header + bytes(data)
