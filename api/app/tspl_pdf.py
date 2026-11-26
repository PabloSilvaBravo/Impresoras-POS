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
# Franjas negras grandes (26-nov-2026): la etiqueta de Blue trae una franja negra de casi todo el ancho y ~19 mm de alto («EXPRESS / SCL - SANTIAGO»).
# La POS-9200L la imprime gris y rayada (154 líneas seguidas con más de la mitad de los puntos encendidos; ni la velocidad, ni la oscuridad, ni otra fuente
# lo cambian), mientras las de Mercado Libre, con bloques chicos, salen nítidas. Con invertir_bloques, cada bloque negro grande sale como recuadro: lo de
# adentro invertido (letra negra sobre blanco) y un borde. Los códigos de barra no califican: sus celdas no son negras parejas.
CELDA = 8             # 1 mm a 203 dpi
CELDA_NEGRA = 64      # promedio de gris de la celda (0 negro, 255 blanco): bajo esto, la celda es negra pareja (>= 75 %)
BLOQUE_MIN_MM = (30, 6)   # ancho y alto mínimos de un bloque para invertirlo
BLOQUE_LLENO = 0.7    # fracción mínima de celdas negras dentro del rectángulo del bloque
BORDE = 3             # puntos del borde del recuadro
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


def _bits(im: Image.Image, umbral: int = UMBRAL) -> bytes:
    # 1 bit: blanco (>= umbral) queda en 1, negro en 0, que es lo que espera BITMAP.
    return im.point(lambda v: 255 if v >= umbral else 0, mode="1").tobytes()


def bloques_negros(im: Image.Image) -> list[tuple[int, int, int, int]]:
    """Rectángulos (x0, y0, x1, y1) en puntos de los bloques negros grandes de una imagen L. Pura."""
    cw, ch = im.width // CELDA, im.height // CELDA
    if cw < 1 or ch < 1:
        return []
    chica = im.crop((0, 0, cw * CELDA, ch * CELDA)).resize((cw, ch), Image.BOX)
    datos = list(chica.getdata())
    negra = [v < CELDA_NEGRA for v in datos]
    visto = [False] * len(negra)
    out = []
    for i, n in enumerate(negra):
        if not n or visto[i]:
            continue
        pila, celdas = [i], []
        visto[i] = True
        while pila:
            j = pila.pop()
            celdas.append(j)
            x, y = j % cw, j // cw
            for k in ((j - 1) if x > 0 else -1, (j + 1) if x < cw - 1 else -1, j - cw, j + cw):
                if 0 <= k < len(negra) and negra[k] and not visto[k]:
                    visto[k] = True
                    pila.append(k)
        xs = [j % cw for j in celdas]
        ys = [j // cw for j in celdas]
        x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
        ancho_mm, alto_mm = (x1 - x0) * CELDA / 8, (y1 - y0) * CELDA / 8
        if ancho_mm < BLOQUE_MIN_MM[0] or alto_mm < BLOQUE_MIN_MM[1]:
            continue
        # Lo lleno se mide con las celdas negras de todo el rectángulo (las letras blancas de adentro dejan huecos).
        dentro = sum(1 for yy in range(y0, y1) for xx in range(x0, x1) if negra[yy * cw + xx])
        if dentro / ((x1 - x0) * (y1 - y0)) >= BLOQUE_LLENO:
            out.append((x0 * CELDA, y0 * CELDA, x1 * CELDA, y1 * CELDA))
    return out


def invertir_bloques(im: Image.Image) -> tuple[Image.Image, int]:
    """Cada bloque negro grande pasa a recuadro: adentro invertido y un borde de BORDE puntos. Devuelve (imagen, cuántos)."""
    from PIL import ImageDraw, ImageOps

    bloques = bloques_negros(im)
    if not bloques:
        return im, 0
    im = im.copy()
    dibujo = ImageDraw.Draw(im)
    for x0, y0, x1, y1 in bloques:
        # El rectángulo de celdas puede quedar corto del borde real del bloque: se agranda hasta la tinta de alrededor (máximo una celda).
        x0, y0 = max(0, x0 - CELDA), max(0, y0 - CELDA)
        x1, y1 = min(im.width, x1 + CELDA), min(im.height, y1 + CELDA)
        zona = im.crop((x0, y0, x1, y1))
        # Solo se invierte lo que es parte del bloque: el margen blanco de alrededor queda blanco.
        caja = ImageOps.invert(zona).getbbox()
        if caja:
            x0, y0, x1, y1 = x0 + caja[0], y0 + caja[1], x0 + caja[2], y0 + caja[3]
        im.paste(ImageOps.invert(im.crop((x0, y0, x1, y1))), (x0, y0))
        dibujo.rectangle((x0, y0, x1 - 1, y1 - 1), outline=0, width=BORDE)
    return im, len(bloques)


def build_pdf_label(pdf: bytes, printer: PrinterConfig, copies: int = 1,
                    density: int | None = None, speed: int | None = None,
                    invertir: bool = False, umbral: int | None = None) -> tuple[bytes, int]:
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
        if invertir:
            lienzo, _ = invertir_bloques(lienzo)
        cab = [
            f"SIZE {printer.default_width_mm} mm,{printer.default_height_mm} mm",
            f"GAP {printer.default_gap_mm} mm,0 mm",
            "DIRECTION 0",
            f"DENSITY {dens}",
            f"SPEED {vel}",
            "CLS",
        ]
        out += ("\r\n".join(cab) + "\r\n").encode("ascii")
        out += f"BITMAP 0,0,{ancho_dots // 8},{alto_dots},0,".encode("ascii") + _bits(lienzo, umbral or UMBRAL) + b"\r\n"
        out += f"PRINT 1,{copies}\r\n".encode("ascii")
    return bytes(out), len(imgs)
