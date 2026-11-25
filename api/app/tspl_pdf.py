"""PDF de etiqueta (Blue, Chilexpress, A6 o 100x150) a TSPL con BITMAP.

Cada página se dibuja al ancho de la etiqueta en dots, sin reescalar después
(pdftoppm la dibuja a ese tamaño, así las barras quedan nítidas), se gira si
viene apaisada, se centra y se pasa a 1 bit con umbral fijo, sin tramado.
En BITMAP de TSPL un bit 1 es blanco y un bit 0 es negro (modo 0, OVERWRITE).
DIRECTION 0 es la misma que usó el driver de CUPS en la prueba del 24-nov-2026.
"""
from __future__ import annotations

from PIL import Image

from .config import PrinterConfig

UMBRAL = 160          # gris 0-255: bajo esto se imprime
MAX_PAGINAS = 50


def _paginas(pdf: bytes, ancho_dots: int, alto_dots: int) -> list[Image.Image]:
    from pdf2image import convert_from_bytes, pdfinfo_from_bytes

    info = pdfinfo_from_bytes(pdf)
    n = int(info.get("Pages", 0))
    if n < 1:
        raise ValueError("el PDF no trae páginas")
    if n > MAX_PAGINAS:
        raise ValueError(f"el PDF trae {n} páginas; el máximo es {MAX_PAGINAS}")
    w_pt, h_pt = (float(v) for v in str(info["Page size"]).split(" pts")[0].split(" x "))
    girar = w_pt > h_pt
    # Tamaño de dibujo: la página entera cabe en la etiqueta, sin deformarla.
    pw, ph = (h_pt, w_pt) if girar else (w_pt, h_pt)
    escala = min(ancho_dots / pw, alto_dots / ph)
    lado = (round(ph * escala), round(pw * escala)) if girar else (round(pw * escala), round(ph * escala))
    imgs = convert_from_bytes(pdf, size=lado, grayscale=True, thread_count=1)
    out = []
    for im in imgs:
        im = im.convert("L")
        if girar:
            im = im.rotate(90, expand=True)
        lienzo = Image.new("L", (ancho_dots, alto_dots), 255)
        lienzo.paste(im.crop((0, 0, min(im.width, ancho_dots), min(im.height, alto_dots))),
                     ((ancho_dots - min(im.width, ancho_dots)) // 2, (alto_dots - min(im.height, alto_dots)) // 2))
        out.append(lienzo)
    return out


def _bits(im: Image.Image) -> bytes:
    # 1 bit: blanco (>= UMBRAL) queda en 1, negro en 0, que es lo que espera BITMAP.
    return im.point(lambda v: 255 if v >= UMBRAL else 0, mode="1").tobytes()


def build_pdf_label(pdf: bytes, printer: PrinterConfig, copies: int = 1,
                    density: int | None = None, speed: int | None = None) -> tuple[bytes, int]:
    """Devuelve (bytes TSPL, páginas). Una etiqueta por página, `copies` de cada una."""
    dpmm = printer.dots_per_mm
    ancho_dots = int(round(printer.default_width_mm * dpmm)) // 8 * 8
    alto_dots = int(round(printer.default_height_mm * dpmm))
    # El área de dibujo es 2 mm más corta que el paso, para no rozar el corte.
    imgs = _paginas(pdf, ancho_dots, alto_dots - 2 * dpmm)
    dens = printer.default_density if density is None else density
    vel = printer.default_speed if speed is None else speed
    out = bytearray()
    for im in imgs:
        lienzo = Image.new("L", (ancho_dots, alto_dots), 255)
        lienzo.paste(im, (0, dpmm))
        cab = [
            f"SIZE {printer.default_width_mm} mm,{printer.default_height_mm} mm",
            f"GAP {printer.default_gap_mm} mm,0 mm",
            "DIRECTION 0",
            f"DENSITY {dens}",
            f"SPEED {vel}",
            "CLS",
        ]
        out += ("\r\n".join(cab) + "\r\n").encode("ascii")
        out += f"BITMAP 0,0,{ancho_dots // 8},{alto_dots},0,".encode("ascii") + _bits(lienzo) + b"\r\n"
        out += f"PRINT 1,{copies}\r\n".encode("ascii")
    return bytes(out), len(imgs)
