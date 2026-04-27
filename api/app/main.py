import logging
import os
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from . import ipp_client, ted_extractor
from .config import api_token, load_printers
from .escpos_render import build_receipt
from .models import DocumentSpec, LabelSpec, PrinterPublic, RawPayload, ReceiptSpec
from .printers import PrinterManager, detect_usb_printers
from .tspl import build_label

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="Impresoras POS", version="0.1.0")
_manager = PrinterManager(load_printers())
_expected_token = api_token()


@app.on_event("startup")
async def _startup_cleanup_ted_cache() -> None:
    """Limpia cache vencido de TED al arrancar el servicio (LRU + TTL)."""
    try:
        removed = ted_extractor.cleanup_cache()
        if removed:
            logging.getLogger(__name__).info(f"TED cache cleanup: borrados {removed} archivos")
    except Exception as e:
        logging.getLogger(__name__).warning(f"TED cache cleanup falló: {e}")


def auth(authorization: str | None = Header(default=None)) -> None:
    if not _expected_token:
        return
    expected = f"Bearer {_expected_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
        )


@app.get("/health")
def health() -> dict:
    return {"ok": True, "printers": len(_manager.printers)}


@app.get("/printers", response_model=list[PrinterPublic], dependencies=[Depends(auth)])
def list_printers() -> list[PrinterPublic]:
    return [
        PrinterPublic(
            id=p.id,
            name=p.name,
            model=p.model,
            device=p.device,
            available=_manager.is_available(p.id),
            protocol=p.protocol,
        )
        for p in _manager.printers.values()
    ]


@app.get("/printers/discover", dependencies=[Depends(auth)])
def discover() -> dict:
    return {"devices": detect_usb_printers()}


@app.post("/print/{printer_id}", dependencies=[Depends(auth)])
async def print_label(printer_id: str, spec: LabelSpec) -> dict:
    cfg = _manager.printers.get(printer_id)
    if not cfg:
        raise HTTPException(404, f"Impresora '{printer_id}' no encontrada")
    if cfg.protocol != "tspl":
        raise HTTPException(
            422,
            f"Impresora '{printer_id}' usa protocolo '{cfg.protocol}'. "
            f"Para recibos ESC/POS usa POST /receipt/{printer_id}.",
        )
    if not _manager.is_available(printer_id):
        raise HTTPException(503, f"Dispositivo {cfg.device} no disponible")
    payload = build_label(spec, cfg)
    await _manager.send(printer_id, payload)
    return {"status": "printed", "bytes": len(payload), "copies": spec.copies}


@app.post("/print/{printer_id}/raw", dependencies=[Depends(auth)])
async def print_raw(printer_id: str, body: RawPayload) -> dict:
    cfg = _manager.printers.get(printer_id)
    if not cfg:
        raise HTTPException(404, f"Impresora '{printer_id}' no encontrada")
    if not _manager.is_available(printer_id):
        raise HTTPException(503, f"Dispositivo {cfg.device} no disponible")
    payload = body.data.encode("utf-8") if isinstance(body.data, str) else body.data
    await _manager.send(printer_id, payload)
    return {"status": "printed", "bytes": len(payload)}


@app.post("/receipt/{printer_id}", dependencies=[Depends(auth)])
async def print_receipt(printer_id: str, spec: ReceiptSpec) -> dict:
    cfg = _manager.printers.get(printer_id)
    if not cfg:
        raise HTTPException(404, f"Impresora '{printer_id}' no encontrada")
    if cfg.protocol != "escpos":
        raise HTTPException(
            422,
            f"Impresora '{printer_id}' usa protocolo '{cfg.protocol}'. "
            f"Para etiquetas TSPL usa POST /print/{printer_id}.",
        )
    if not _manager.is_available(printer_id):
        raise HTTPException(503, f"Dispositivo {cfg.device} no disponible")

    # Si hay TED, asegurar que esté cacheado antes del render. El prefetch
    # async pudo haberlo generado ya; en el peor caso, esto bloquea 1-3s.
    # Si falla, no abortamos: build_receipt usará fallback de texto.
    ted_status = "n/a"
    if spec.ted is not None:
        try:
            await ted_extractor.get_or_cache(
                spec.ted.venta_id,
                spec.ted.pdf_url,
                spec.ted.template,
                target_width=spec.ted.target_width_px,
            )
            ted_status = "cached"
        except Exception as e:
            logging.getLogger(__name__).warning(
                f"TED extract fail venta_id={spec.ted.venta_id}: {e}"
            )
            ted_status = f"failed: {type(e).__name__}"

    payload = build_receipt(spec, cfg)
    await _manager.send(printer_id, payload)
    return {
        "status": "printed",
        "bytes": len(payload),
        "copies": spec.copies,
        "ted": ted_status,
    }


# ── IPP: imprimir PDF en impresora de red (Brother, EPSON, etc.) ────────────

@app.post("/document/{printer_id}", dependencies=[Depends(auth)])
async def print_document(printer_id: str, spec: DocumentSpec) -> dict:
    """
    Descarga un PDF desde la URL provista y lo envía a la impresora IPP
    indicada (típicamente facturas A4 a una Brother/EPSON/HP en red).

    El servicio nunca cachea el PDF: cada print es un download fresco para
    asegurar que el documento es la versión actual de Bsale (las URLs llevan
    parámetros de firma).
    """
    cfg = _manager.printers.get(printer_id)
    if not cfg:
        raise HTTPException(404, f"Impresora '{printer_id}' no encontrada")
    if cfg.protocol != "ipp":
        raise HTTPException(
            422,
            f"Impresora '{printer_id}' usa protocolo '{cfg.protocol}'. "
            f"Para PDFs por red usa una impresora con protocol=ipp.",
        )

    # Descargar el PDF
    import httpx as _httpx
    log = logging.getLogger(__name__)
    log.info(f"IPP document → {printer_id} pdf_url={spec.pdf_url}")
    try:
        async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(spec.pdf_url)
            r.raise_for_status()
            pdf_bytes = r.content
    except _httpx.HTTPError as e:
        raise HTTPException(502, f"Error descargando PDF: {e}")

    if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        raise HTTPException(
            502,
            f"La URL no devolvió un PDF válido (primeros bytes: {pdf_bytes[:8]!r}, size={len(pdf_bytes)})",
        )

    # Enviar a la impresora IPP. El cfg.device contiene la URI ipp://host:port/path
    try:
        result = await ipp_client.print_pdf(
            cfg.device,
            pdf_bytes,
            copies=spec.copies,
            document_format=spec.document_format,
            requesting_user=spec.requesting_user_name,
            job_name=f"dashboard-{printer_id}",
        )
    except ipp_client.IPPError as e:
        raise HTTPException(502, f"Impresora IPP rechazó: {e}")
    except _httpx.ConnectError as e:
        raise HTTPException(503, f"Impresora IPP no responde: {e}")
    except _httpx.HTTPError as e:
        raise HTTPException(502, f"Error IPP: {e}")

    return result


# ── TED Prefetch ─────────────────────────────────────────────────────────────

class TedPrefetchBody(BaseModel):
    pdf_url: str
    template: Literal["boleta", "factura"]
    target_width_px: int | None = None


@app.post("/ted/prefetch/{venta_id}", dependencies=[Depends(auth)])
async def ted_prefetch(venta_id: int, body: TedPrefetchBody, bg: BackgroundTasks) -> dict:
    """
    Pre-genera el bitmap del TED en background. Llamar desde el dashboard
    inmediatamente tras emitir una venta — así cuando el cajero apriete
    "Imprimir", el TED ya está cacheado y la impresión es instantánea.

    Responde 202 inmediato — el procesamiento corre asíncrono.
    """
    bg.add_task(_safe_prefetch, venta_id, body.pdf_url, body.template, body.target_width_px)
    return {"accepted": True, "venta_id": venta_id}


async def _safe_prefetch(
    venta_id: int,
    pdf_url: str,
    template: str,
    target_width: int | None = None,
) -> None:
    """Wrapper que loguea errores sin propagarlos (es background task)."""
    try:
        await ted_extractor.get_or_cache(venta_id, pdf_url, template, target_width)
        logging.getLogger(__name__).info(f"TED prefetched venta_id={venta_id} tw={target_width}")
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"TED prefetch fail venta_id={venta_id}: {type(e).__name__}: {e}"
        )
