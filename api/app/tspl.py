from .config import PrinterConfig
from .models import (
    BarcodeElement,
    BoxElement,
    Element,
    LabelSpec,
    LineElement,
    QRCodeElement,
    TextElement,
)


def _mm_to_dots(mm: float, dpmm: int) -> int:
    return int(round(mm * dpmm))


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render_element(el: Element, dpmm: int) -> str:
    x = _mm_to_dots(el.x_mm, dpmm)
    y = _mm_to_dots(el.y_mm, dpmm)

    if isinstance(el, TextElement):
        return (
            f'TEXT {x},{y},"{el.font}",{el.rotation},'
            f'{el.x_scale},{el.y_scale},"{_escape(el.content)}"'
        )

    if isinstance(el, BarcodeElement):
        height = _mm_to_dots(el.height_mm, dpmm)
        return (
            f'BARCODE {x},{y},"{el.symbology}",{height},{el.human_readable},'
            f'{el.rotation},{el.narrow},{el.wide},"{_escape(el.content)}"'
        )

    if isinstance(el, QRCodeElement):
        return (
            f'QRCODE {x},{y},{el.ecc},{el.cell_width},A,'
            f'{el.rotation},"{_escape(el.content)}"'
        )

    if isinstance(el, BoxElement):
        x_end = x + _mm_to_dots(el.width_mm, dpmm)
        y_end = y + _mm_to_dots(el.height_mm, dpmm)
        return f"BOX {x},{y},{x_end},{y_end},{el.thickness}"

    if isinstance(el, LineElement):
        width = _mm_to_dots(el.width_mm, dpmm)
        thickness = max(1, _mm_to_dots(el.thickness_mm, dpmm))
        return f"BAR {x},{y},{width},{thickness}"

    raise ValueError(f"Tipo de elemento no soportado: {el!r}")


def build_label(spec: LabelSpec, printer: PrinterConfig) -> bytes:
    width = spec.width_mm or printer.default_width_mm
    height = spec.height_mm or printer.default_height_mm
    gap = spec.gap_mm if spec.gap_mm is not None else printer.default_gap_mm
    density = spec.density if spec.density is not None else printer.default_density
    speed = spec.speed if spec.speed is not None else printer.default_speed
    dpmm = printer.dots_per_mm

    lines = [
        f"SIZE {width} mm,{height} mm",
        f"GAP {gap} mm,0 mm",
        f"DIRECTION {spec.direction}",
        f"DENSITY {density}",
        f"SPEED {speed}",
        # Requerido para que la impresora interprete como UTF-8 los bytes que
        # ya emitimos con .encode("utf-8") al final. Sin esto la Xprinter
        # asume CP437 y los acentos (á, é, í, ó, ú, ñ) salen como espacios
        # vacíos o glifos erróneos.
        "CODEPAGE UTF-8",
        "CLS",
    ]
    lines.extend(_render_element(el, dpmm) for el in spec.elements)
    lines.append(f"PRINT {spec.copies},1")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")
