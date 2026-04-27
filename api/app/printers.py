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


def detect_usb_printers() -> list[str]:
    """Devuelve la lista de dispositivos /dev/usb/lp* visibles."""
    return sorted(glob.glob("/dev/usb/lp*"))
