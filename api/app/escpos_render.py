"""
Renderer ESC/POS para impresoras térmicas de recibos (POS-80xx, POS-58xx, TM-*).

Convierte un ReceiptSpec (estructura de alto nivel: header, items, totales,
etc.) en los bytes ESC/POS que la impresora entiende. No depende de ninguna
librería externa — solo las secuencias básicas bien documentadas del
protocolo.

Codepages soportados:
  cp437  → default del firmware, SIN acentos
  cp850  → multilingual Latin 1, CON acentos españoles (á é í ó ú ñ) ← default
  cp858  → cp850 + símbolo €

Probado en: POS-8370 (Winbond 0416:5011, 80mm, 48 chars por línea)
"""

from .config import PrinterConfig
from .models import ReceiptSpec

# ── Secuencias ESC/POS (bytes crudos) ────────────────────────────────────────

ESC = b"\x1B"
GS  = b"\x1D"

RESET           = ESC + b"@"
ALIGN_LEFT      = ESC + b"a\x00"
ALIGN_CENTER    = ESC + b"a\x01"
ALIGN_RIGHT     = ESC + b"a\x02"
BOLD_ON         = ESC + b"E\x01"
BOLD_OFF        = ESC + b"E\x00"
UNDERLINE_ON    = ESC + b"-\x01"
UNDERLINE_OFF   = ESC + b"-\x00"
# GS ! n: tamaño. Nibble alto = altura (0..7), nibble bajo = ancho (0..7).
SIZE_NORMAL     = GS + b"!\x00"
SIZE_DBL_WIDTH  = GS + b"!\x10"
SIZE_DBL_HEIGHT = GS + b"!\x01"
SIZE_DBL_BOTH   = GS + b"!\x11"
# Codepage (ESC t n)
CODEPAGE_MAP = {
    "cp437": ESC + b"t\x00",
    "cp850": ESC + b"t\x02",
    "cp858": ESC + b"t\x13",
}
# Corte de papel (GS V n; n=0 full, n=1 partial)
CUT_FULL        = GS + b"V\x00"
# Feed n líneas antes del corte (ESC d n)
def _feed(n: int) -> bytes:
    return ESC + b"d" + bytes([max(0, min(255, n))])

LF = b"\n"


# ── Helpers de formato ──────────────────────────────────────────────────────

def _encode(text: str, encoding: str) -> bytes:
    """Codifica texto al codepage de la impresora, reemplazando lo que no mapea."""
    try:
        return text.encode(encoding, errors="replace")
    except LookupError:
        return text.encode("cp850", errors="replace")


def _clp(n: float) -> str:
    """Formato CLP: 12990 → '$12.990'. Sin decimales, punto como separador de miles."""
    val = int(round(n))
    sign = "-" if val < 0 else ""
    s = f"{abs(val):,}".replace(",", ".")
    return f"{sign}${s}"


def _pad_line(left: str, right: str, width: int) -> str:
    """
    Construye una línea con `left` a la izquierda y `right` a la derecha,
    rellenando con espacios hasta `width`. Si no cabe, trunca `left`.
    """
    right = right or ""
    left = left or ""
    avail = width - len(right)
    if avail <= 1:
        # No cabe, mejor cortar suavemente
        return (left[: max(0, width - len(right))] + right)[:width]
    if len(left) > avail - 1:
        left = left[: avail - 2] + "…"
    return left + " " * (width - len(left) - len(right)) + right


def _separator(width: int, char: str = "-") -> str:
    return char * width


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


# ── Renderers por sección ───────────────────────────────────────────────────

def _render_header(spec: ReceiptSpec, enc: str) -> bytes:
    if not spec.header_lines:
        return b""
    out = ALIGN_CENTER
    for i, line in enumerate(spec.header_lines):
        if i == 0:
            # Primera línea (típicamente el nombre de la tienda): bold + doble ancho
            out += BOLD_ON + SIZE_DBL_WIDTH
            out += _encode(line, enc) + LF
            out += SIZE_NORMAL + BOLD_OFF
        else:
            out += _encode(line, enc) + LF
    out += LF
    return out


def _render_title(spec: ReceiptSpec, enc: str) -> bytes:
    if not spec.title:
        return b""
    out = ALIGN_CENTER + BOLD_ON + SIZE_DBL_BOTH
    out += _encode(spec.title, enc) + LF
    out += SIZE_NORMAL + BOLD_OFF
    if spec.subtitle:
        out += _encode(spec.subtitle, enc) + LF
    out += LF
    return out


def _render_metadata(spec: ReceiptSpec, enc: str) -> bytes:
    if not spec.metadata:
        return b""
    out = ALIGN_LEFT
    for m in spec.metadata:
        line = _pad_line(m.key + ":", m.value, spec.width_chars)
        out += _encode(line, enc) + LF
    out += LF
    return out


def _render_items(spec: ReceiptSpec, enc: str) -> bytes:
    if not spec.items:
        return b""
    w = spec.width_chars
    out = ALIGN_LEFT
    out += _encode(_separator(w), enc) + LF
    for item in spec.items:
        # Línea 1: nombre del item, truncado si no cabe
        out += _encode(_truncate(item.name, w), enc) + LF
        # Línea 2: "  qty x unit_price" a la izquierda, total a la derecha
        if item.unit_price is not None:
            qty_str = f"{item.qty:g}" if item.qty != int(item.qty) else f"{int(item.qty)}"
            detail = f"  {qty_str} x {_clp(item.unit_price)}"
        else:
            detail = ""
        line = _pad_line(detail, _clp(item.total), w)
        out += _encode(line, enc) + LF
    out += _encode(_separator(w), enc) + LF
    return out


def _render_totals(spec: ReceiptSpec, enc: str) -> bytes:
    if not spec.totals:
        return b""
    w = spec.width_chars
    out = ALIGN_LEFT
    for t in spec.totals:
        label = t.label
        amount = _clp(t.amount)
        if t.big:
            # Tamaño doble → la mitad de chars por línea
            out += BOLD_ON + SIZE_DBL_BOTH
            line = _pad_line(label, amount, max(20, w // 2))
            out += _encode(line, enc) + LF
            out += SIZE_NORMAL + BOLD_OFF
        elif t.bold:
            out += BOLD_ON
            out += _encode(_pad_line(label, amount, w), enc) + LF
            out += BOLD_OFF
        else:
            out += _encode(_pad_line(label, amount, w), enc) + LF
    out += LF
    return out


def _render_footer(spec: ReceiptSpec, enc: str) -> bytes:
    if not spec.footer_lines:
        return b""
    out = ALIGN_CENTER
    for line in spec.footer_lines:
        out += _encode(line, enc) + LF
    out += LF
    return out


def _render_qr(spec: ReceiptSpec) -> bytes:
    """
    QR Code ESC/POS nativo (GS ( k ...). Soporte casi universal en impresoras
    modernas. Modelo 2, tamaño 8, ECC M.
    """
    if not spec.qr_content:
        return b""
    content = spec.qr_content.encode("utf-8")
    out = ALIGN_CENTER
    # Seleccionar modelo: GS ( k pL pH cn=0x31 fn=0x41 n1=0x32 n2=0x00
    out += GS + b"(k\x04\x00\x31\x41\x32\x00"
    # Tamaño del módulo: GS ( k pL pH cn=0x31 fn=0x43 n (1..16, default 3; usamos 8)
    out += GS + b"(k\x03\x00\x31\x43\x08"
    # Nivel ECC: GS ( k pL pH cn=0x31 fn=0x45 n (48=L, 49=M, 50=Q, 51=H)
    out += GS + b"(k\x03\x00\x31\x45\x31"
    # Almacenar data: GS ( k pL pH cn=0x31 fn=0x50 m=0x30 <data>
    length = len(content) + 3
    pL = length & 0xFF
    pH = (length >> 8) & 0xFF
    out += GS + b"(k" + bytes([pL, pH]) + b"\x31\x50\x30" + content
    # Imprimir: GS ( k pL pH cn=0x31 fn=0x51 m=0x30
    out += GS + b"(k\x03\x00\x31\x51\x30"
    out += LF
    return out


def _render_cut(spec: ReceiptSpec) -> bytes:
    out = _feed(3)  # 3 líneas de feed para dar espacio al corte
    if spec.cut:
        out += CUT_FULL
    return out


# ── API pública ─────────────────────────────────────────────────────────────

def build_receipt(spec: ReceiptSpec, printer: PrinterConfig) -> bytes:
    """
    Construye los bytes ESC/POS completos para un recibo.

    Los bytes se pueden escribir directamente al /dev/usb/lp* correspondiente.
    Respeta `copies` repitiendo el bloque N veces.
    """
    enc = spec.encoding or "cp850"
    codepage = CODEPAGE_MAP.get(enc, CODEPAGE_MAP["cp850"])

    single = b""
    single += RESET
    single += codepage
    single += _render_header(spec, enc)
    single += _render_title(spec, enc)
    single += _render_metadata(spec, enc)
    single += _render_items(spec, enc)
    single += _render_totals(spec, enc)
    single += _render_footer(spec, enc)
    single += _render_qr(spec)
    single += _render_cut(spec)

    copies = max(1, min(5, spec.copies))
    return single * copies
