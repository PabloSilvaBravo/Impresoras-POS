import logging
import os
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import ipp_client, ted_extractor
from .config import api_token, load_printers
from .escpos_render import build_receipt
from .models import DocumentSpec, LabelSpec, PdfLabelSpec, PrinterPublic, RawPayload, ReceiptSpec
from .printers import PrinterManager, detect_usb_printers, estado_papel
from .tspl import build_label
from .tspl_pdf import build_pdf_label

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


PDF_MAX_BYTES = 8 * 1024 * 1024
ESPERA_IMPRESORA_S = 25.0
_esperando: set = set()   # escrituras que siguen esperando a la impresora (se guarda la referencia para que no se pierdan)


@app.post("/print-pdf/{printer_id}", dependencies=[Depends(auth)])
async def print_pdf_label(printer_id: str, spec: PdfLabelSpec) -> dict:
    """PDF de etiqueta (una por página) a una térmica TSPL como mapa de bits."""
    import asyncio as _asyncio
    import base64 as _b64

    import httpx as _httpx

    cfg = _manager.printers.get(printer_id)
    if not cfg:
        raise HTTPException(404, f"Impresora '{printer_id}' no encontrada")
    if cfg.protocol != "tspl":
        raise HTTPException(422, f"Impresora '{printer_id}' usa protocolo '{cfg.protocol}'; /print-pdf es para tspl.")
    if (spec.pdf_url is None) == (spec.pdf_base64 is None):
        raise HTTPException(422, "Manda pdf_url o pdf_base64, uno de los dos.")
    if not _manager.is_available(printer_id):
        raise HTTPException(503, f"Dispositivo {cfg.device} no disponible")
    if spec.pdf_url is not None:
        try:
            async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                r = await client.get(spec.pdf_url)
                r.raise_for_status()
                pdf = r.content
        except _httpx.HTTPError as e:
            raise HTTPException(502, f"Error descargando PDF: {type(e).__name__}")
    else:
        try:
            pdf = _b64.b64decode(spec.pdf_base64, validate=True)
        except Exception:
            raise HTTPException(422, "pdf_base64 no es base64 válido")
    if len(pdf) > PDF_MAX_BYTES:
        raise HTTPException(413, "El PDF pesa más de 8 MB")
    if pdf[:5] != b"%PDF-":
        raise HTTPException(422, "No es un PDF")
    if cfg.check_paper:
        lista, motivo = estado_papel(cfg.device)
        if not lista:
            logging.getLogger(__name__).warning(f"print-pdf {printer_id}: no se manda, {motivo}")
            raise HTTPException(503, f"Impresora no lista: {motivo}")
    try:
        payload, paginas = await _asyncio.to_thread(build_pdf_label, pdf, cfg, spec.copies, spec.density, spec.speed, spec.invert_blocks, spec.threshold)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        logging.getLogger(__name__).exception("print-pdf: no se pudo convertir")
        raise HTTPException(500, f"No se pudo convertir el PDF: {type(e).__name__}")
    if cfg.check_paper:
        # 26-nov-2026, medido en la tienda: con la tapa abierta la POS-9200L sigue diciendo 0x18 (lista) por USB, pero no recibe datos: la escritura
        # queda esperando y la etiqueta sale sola al cerrar la tapa. Si ya hay una esperando, esta no se manda (503: se sabe que no salió). Si esta no
        # entra en ESPERA_IMPRESORA_S, se responde 202 waiting_printer y la escritura sigue en segundo plano: saldrá al cerrar, no hay que reenviarla.
        lock = _manager._locks.get(printer_id)
        if lock is not None and lock.locked():
            raise HTTPException(503, "Impresora no lista: tapa abierta o sin papel, hay una etiqueta esperando")
        tarea = _asyncio.create_task(_manager.send(printer_id, payload))
        hecho, _ = await _asyncio.wait({tarea}, timeout=ESPERA_IMPRESORA_S)
        if not hecho:
            logging.getLogger(__name__).warning(f"print-pdf {printer_id}: la impresora no recibe hace {ESPERA_IMPRESORA_S} s; la etiqueta queda esperando")
            _esperando.add(tarea)
            tarea.add_done_callback(_esperando.discard)
            return JSONResponse(status_code=202, content={"status": "waiting_printer", "pages": paginas, "copies": spec.copies,
                "detail": "la impresora no recibe (tapa abierta o sin papel); la etiqueta sale sola al cerrarla"})
        tarea.result()
    else:
        await _manager.send(printer_id, payload)
    respuesta = {"status": "printed", "bytes": len(payload), "pages": paginas, "copies": spec.copies}
    if cfg.check_paper:
        # Lo que tarda en salir el papel (unos 4 s por etiqueta a velocidad 4) y se mira de nuevo: si se acabó el papel a medias, el dashboard no la da por impresa.
        await _asyncio.sleep(min(20.0, 2.0 + 4.0 * paginas * spec.copies))
        lista, motivo = estado_papel(cfg.device)
        respuesta["paper_ok_after"] = lista
        respuesta["status_after"] = motivo
    return respuesta


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

    # Enviar a la impresora IPP. Las Brother T-series (y muchas inkjet de
    # oficina) NO soportan PDF ni PostScript via IPP. Suelen aceptar:
    #   - image/jpeg
    #   - image/pwg-raster (IPP Everywhere)
    #   - image/urf (AirPrint)
    #   - application/vnd.brother-hbpl (propietario)
    #
    # Cascada: PDF → PostScript → JPEG (1 job por página). El primero que
    # acepte la impresora gana. Para Brother DCP-T720DW va directo al JPEG.
    async def _try_print(payload: bytes, fmt: str, job_suffix: str = "") -> dict:
        return await ipp_client.print_pdf(
            cfg.device,
            payload,
            copies=spec.copies,
            document_format=fmt,
            requesting_user=spec.requesting_user_name,
            job_name=f"dashboard-{printer_id}{job_suffix}",
        )

    def _is_format_error(exc: Exception) -> bool:
        return "format-not-supported" in str(exc).lower()

    async def _convert_pdf_to_ps(pdf: bytes) -> bytes:
        """Convierte PDF a PostScript usando pdftops (poppler-utils)."""
        import asyncio as _asyncio
        import subprocess as _sp
        proc = await _asyncio.create_subprocess_exec(
            "pdftops", "-", "-",
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        out, err = await _asyncio.wait_for(proc.communicate(input=pdf), timeout=30.0)
        if proc.returncode != 0:
            raise RuntimeError(f"pdftops failed: {err.decode('utf-8', errors='replace')[:200]}")
        return out

    def _pdf_to_jpeg_pages(pdf: bytes, dpi: int = 200) -> list[bytes]:
        """Convierte cada página del PDF a JPEG bytes con pdf2image."""
        from pdf2image import convert_from_bytes
        from io import BytesIO
        pages = convert_from_bytes(pdf, dpi=dpi)
        out = []
        for p in pages:
            buf = BytesIO()
            p.convert("RGB").save(buf, format="JPEG", quality=92, optimize=True)
            out.append(buf.getvalue())
        return out

    try:
        # Intento 1: PDF
        try:
            result = await _try_print(pdf_bytes, spec.document_format)
            result["format"] = spec.document_format
            return result
        except ipp_client.IPPError as e:
            if not _is_format_error(e):
                raise

        # Intento 2: PostScript
        log.info("Impresora rechazó PDF, intentando PostScript...")
        try:
            ps_bytes = await _convert_pdf_to_ps(pdf_bytes)
            log.info(f"PDF→PS OK ({len(pdf_bytes)}→{len(ps_bytes)} bytes), reintentando...")
            result = await _try_print(ps_bytes, "application/postscript")
            result["format"] = "application/postscript"
            result["converted_from"] = "application/pdf"
            return result
        except ipp_client.IPPError as e:
            if not _is_format_error(e):
                raise
        except FileNotFoundError:
            log.warning("pdftops no disponible, salto PostScript")
        except (RuntimeError, Exception) as e:
            log.warning(f"PDF→PS falló: {e}, salto a JPEG")

        # Intento 3: JPEG (1 job por página)
        log.info("Impresora rechazó PostScript, convirtiendo a JPEG por página...")
        import asyncio as _asyncio2
        try:
            loop = _asyncio2.get_event_loop()
            jpegs = await loop.run_in_executor(None, _pdf_to_jpeg_pages, pdf_bytes, 200)
        except Exception as e:
            raise HTTPException(500, f"Error convirtiendo PDF a JPEG: {e}")

        log.info(f"PDF→JPEG OK ({len(pdf_bytes)} bytes → {len(jpegs)} págs)")
        last_result = None
        for idx, jpg in enumerate(jpegs, start=1):
            log.info(f"Imprimiendo pág {idx}/{len(jpegs)}: {len(jpg)} bytes JPEG")
            last_result = await _try_print(jpg, "image/jpeg", job_suffix=f"-p{idx}")
        last_result["format"] = "image/jpeg"
        last_result["converted_from"] = "application/pdf"
        last_result["pages"] = len(jpegs)
        return last_result

    except ipp_client.IPPError as e:
        raise HTTPException(502, f"Impresora IPP rechazó: {e}")
    except _httpx.ConnectError as e:
        raise HTTPException(503, f"Impresora IPP no responde: {e}")
    except _httpx.HTTPError as e:
        raise HTTPException(502, f"Error IPP: {e}")


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
