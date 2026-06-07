import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml


@dataclass
class PrinterConfig:
    id: str
    name: str
    device: str
    model: str = "xp-420b"
    # "tspl"   → etiquetas térmicas (Xprinter XP-420B y similares)
    # "escpos" → recibos de punto de venta (POS-80xx, POS-58xx, EPSON TM-*, etc.)
    # "ipp"    → impresora WiFi/red con IPP nativo (Brother, EPSON, HP)
    #            En este caso `device` debe ser una URI ipp://host:port/path
    protocol: Literal["tspl", "escpos", "ipp"] = "tspl"
    # TSPL
    dots_per_mm: int = 8
    default_width_mm: float = 100.0
    default_height_mm: float = 60.0
    default_gap_mm: float = 2.0
    default_density: int = 8
    default_speed: int = 4
    # ESC/POS — encabezado fijo de la empresa prependado a cada recibo
    # (razón social, RUT, giro, dirección, web). Primera línea sale en
    # bold + doble ancho, el resto en tamaño normal. Vacío = sin header.
    company_header: list[str] = field(default_factory=list)


def load_printers(path: str | None = None) -> dict[str, PrinterConfig]:
    path = path or os.environ.get("CONFIG_PATH", "/app/config/printers.yml")
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    printers: dict[str, PrinterConfig] = {}
    for item in data.get("printers", []):
        cfg = PrinterConfig(**item)
        printers[cfg.id] = cfg
    return printers


def api_token() -> str | None:
    token = os.environ.get("API_TOKEN", "").strip()
    return token or None
