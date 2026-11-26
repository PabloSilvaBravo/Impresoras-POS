import asyncio
import glob
import logging
import os
from pathlib import Path

from .config import PrinterConfig

log = logging.getLogger(__name__)


class PrinterManager:
    """Maneja el envío de datos a cada impresora con un lock por device."""

    def __init__(self, printers: dict[str, PrinterConfig]):
        self.printers = printers
        self._locks: dict[str, asyncio.Lock] = {
            pid: asyncio.Lock() for pid in printers
        }

    def is_available(self, printer_id: str) -> bool:
        cfg = self.printers.get(printer_id)
        if not cfg:
            return False
        # Para impresoras de red (IPP) no podemos chequear con Path; asumimos
        # disponible. El endpoint que la usa manejará timeout/connect errors.
        if cfg.protocol == "ipp":
            return True
        # USB: chequeo del device file
        return Path(cfg.device).exists()

    async def send(self, printer_id: str, payload: bytes) -> None:
        cfg = self.printers.get(printer_id)
        if not cfg:
            raise KeyError(printer_id)
        lock = self._locks.setdefault(printer_id, asyncio.Lock())
        async with lock:
            await asyncio.to_thread(self._write, cfg.device, payload)

    @staticmethod
    def _write(device: str, payload: bytes) -> None:
        fd = os.open(device, os.O_WRONLY)
        try:
            total = 0
            while total < len(payload):
                n = os.write(fd, payload[total:])
                if n <= 0:
                    raise IOError(f"No se pudo escribir en {device}")
                total += n
        finally:
            os.close(fd)


LPGETSTATUS = 0x060B
# Byte de estado del puerto paralelo (IEEE 1284) que usblp devuelve: 0x20 sin papel, 0x10 seleccionada, 0x08 sin error (activo en alto).
PAPEL_VACIO = 0x20
SELECCIONADA = 0x10
SIN_ERROR = 0x08


def leer_estado(device: str) -> int | None:
    """El byte de estado de una impresora usblp, o None si no se pudo leer."""
    import fcntl
    import struct

    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = fcntl.ioctl(fd, LPGETSTATUS, struct.pack("i", 0))
        return struct.unpack("i", buf)[0] & 0xFF
    except OSError:
        return None
    finally:
        os.close(fd)


def estado_papel(device: str) -> tuple[bool, str]:
    """(lista, motivo). Lista: con papel, seleccionada y sin error. Si no se puede leer el estado no se frena nada (lista, 'sin_estado')."""
    st = leer_estado(device)
    if st is None:
        return True, "sin_estado"
    if st & PAPEL_VACIO:
        return False, f"sin papel (estado 0x{st:02x})"
    if not st & SIN_ERROR:
        return False, f"impresora con error o tapa abierta (estado 0x{st:02x})"
    return True, f"0x{st:02x}"


def detect_usb_printers() -> list[str]:
    """Devuelve la lista de dispositivos /dev/usb/lp* visibles."""
    return sorted(glob.glob("/dev/usb/lp*"))
