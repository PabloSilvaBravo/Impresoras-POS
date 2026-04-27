"""
IPP (Internet Printing Protocol) client - implementación mínima.

Permite mandar PDFs a impresoras de red que soporten IPP nativo (la mayoría
de las modernas: Brother, EPSON, HP, Canon recientes). NO requiere CUPS ni
drivers — el protocolo IPP es HTTP + binary attributes.

Probado contra: Brother DCP-T720DW (192.168.1.42:631/ipp/print).

Referencias:
    RFC 8011 - Internet Printing Protocol/1.1: Model and Semantics
    RFC 2911 - Internet Printing Protocol/1.0: Model and Semantics
"""

from __future__ import annotations

import logging
import struct

import httpx

log = logging.getLogger(__name__)


# ── Constantes IPP (RFC 8011) ────────────────────────────────────────────────

IPP_VERSION = b"\x02\x00"  # IPP 2.0

# Operations (request)
OP_PRINT_JOB              = 0x0002
OP_GET_PRINTER_ATTRIBUTES = 0x000B

# Status codes (response)
STATUS_OK                       = 0x0000
STATUS_OK_IGNORED               = 0x0001
STATUS_OK_ATTRIBUTES_OR_VALUES  = 0x0002
STATUS_CLIENT_ERROR_BAD_REQUEST = 0x0400

# Delimiter tags
TAG_OPERATION_ATTRS = 0x01
TAG_JOB_ATTRS       = 0x02
TAG_END_ATTRS       = 0x03
TAG_PRINTER_ATTRS   = 0x04

# Value tags
TAG_INTEGER       = 0x21
TAG_BOOLEAN       = 0x22
TAG_ENUM          = 0x23
TAG_KEYWORD       = 0x44
TAG_NAME          = 0x42
TAG_TEXT          = 0x41
TAG_URI           = 0x45
TAG_CHARSET       = 0x47
TAG_LANGUAGE      = 0x48
TAG_MIMETYPE      = 0x49


# ── Builders ────────────────────────────────────────────────────────────────

def _attr(tag: int, name: bytes, value: bytes) -> bytes:
    """Construye un atributo IPP: tag + name-len + name + value-len + value."""
    return (
        bytes([tag])
        + struct.pack(">H", len(name)) + name
        + struct.pack(">H", len(value)) + value
    )


def _build_print_job_request(
    printer_uri: str,
    pdf_bytes: bytes,
    *,
    request_id: int = 1,
    copies: int = 1,
    document_format: str = "application/pdf",
    requesting_user: str = "anonymous",
    job_name: str = "dashboard-print",
) -> bytes:
    """Arma el request IPP Print-Job binario completo."""
    header = (
        IPP_VERSION
        + struct.pack(">H", OP_PRINT_JOB)
        + struct.pack(">I", request_id)
    )
    op_attrs = (
        bytes([TAG_OPERATION_ATTRS])
        + _attr(TAG_CHARSET,   b"attributes-charset",            b"utf-8")
        + _attr(TAG_LANGUAGE,  b"attributes-natural-language",   b"en")
        + _attr(TAG_URI,       b"printer-uri",                   printer_uri.encode("ascii"))
        + _attr(TAG_NAME,      b"requesting-user-name",          requesting_user.encode("utf-8"))
        + _attr(TAG_NAME,      b"job-name",                      job_name.encode("utf-8"))
        + _attr(TAG_MIMETYPE,  b"document-format",               document_format.encode("ascii"))
    )
    job_attrs = (
        bytes([TAG_JOB_ATTRS])
        + _attr(TAG_INTEGER,   b"copies",                        struct.pack(">i", copies))
    )
    end = bytes([TAG_END_ATTRS])
    return header + op_attrs + job_attrs + end + pdf_bytes


# ── Parser de respuesta (mínimo, solo extrae status) ────────────────────────

def _parse_response_status(data: bytes) -> tuple[int, str]:
    """Devuelve (status_code, reason)."""
    if len(data) < 8:
        return (-1, "respuesta IPP truncada")
    status = struct.unpack(">H", data[2:4])[0]
    reason = "OK" if status < 0x0100 else f"IPP status 0x{status:04X}"
    return status, reason


# ── API pública ─────────────────────────────────────────────────────────────

async def print_pdf(
    printer_uri: str,
    pdf_bytes: bytes,
    *,
    copies: int = 1,
    document_format: str = "application/pdf",
    requesting_user: str = "anonymous",
    job_name: str = "dashboard-print",
    timeout: float = 30.0,
) -> dict:
    """
    Envía un PDF a una impresora IPP. Lanza httpx.HTTPError o IPPError en
    falla. Devuelve un dict con info del job.

    Args:
        printer_uri: URI completa, ej "ipp://192.168.1.42:631/ipp/print"
        pdf_bytes: contenido del PDF
        copies: número de copias (1..10)
        document_format: típicamente "application/pdf" o "application/octet-stream"

    Returns:
        { "status": "printed", "ipp_status": int, "bytes": int, "copies": int }
    """
    request = _build_print_job_request(
        printer_uri,
        pdf_bytes,
        copies=copies,
        document_format=document_format,
        requesting_user=requesting_user,
        job_name=job_name,
    )

    # IPP corre sobre HTTP. Convertimos ipp:// → http:// para la request real.
    http_url = printer_uri.replace("ipp://", "http://", 1).replace("ipps://", "https://", 1)

    headers = {
        "Content-Type": "application/ipp",
        "Content-Length": str(len(request)),
    }

    log.info(f"IPP Print-Job → {http_url} ({len(pdf_bytes)} bytes PDF, {copies} copias)")
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        response = await client.post(http_url, content=request, headers=headers)
        response.raise_for_status()

    status, reason = _parse_response_status(response.content)

    # Cualquier 0x00XX es OK (RFC 8011 §3.1.6.1)
    if status >= 0x0100:
        raise IPPError(f"Impresora rechazó el job: {reason}")

    return {
        "status": "printed",
        "ipp_status": status,
        "bytes": len(pdf_bytes),
        "copies": copies,
    }


class IPPError(Exception):
    """Error reportado por la impresora IPP."""
