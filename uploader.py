"""
uploader.py — GCS Resumable Upload verdaderamente incremental.

Protocolo GCS Resumable Upload:
  - Chunks intermedios: Content-Range: bytes {start}-{end}/*
  - Chunk final:        Content-Range: bytes {start}-{end}/{total}
  - GCS responde 308 (Resume Incomplete) en chunks intermedios  ✅
  - GCS responde 200/201 en el chunk final                      ✅

Esto permite escribir CSVs de cualquier tamaño sin conocer el total
de bytes de antemano, y sin acumular nada en memoria más allá del
chunk actual.

Ref: https://cloud.google.com/storage/docs/resumable-uploads
"""

import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TOKEN_TTL_SECONDS = 3000   # refresca el token cada ~50 min


class PlatformUploader:
    def __init__(self, backend_url: str, token: str):
        self._backend_url = backend_url.rstrip("/")
        self._token       = token

    # ── Autenticación (cacheada) ──────────────────────────────────────────────

    def get_token(self) -> str:
        return self._token

    # ── Paso 2: Obtener URL firmada ───────────────────────────────────────────

    def get_resumable_url(self, filename: str) -> dict:
        """
        POST /api/v1/uploads/signed-urls
        Nuevo envelope de respuesta:
          { success, message, data: [{ uploadId, fileName, uploadUrl }], timestamp }
        Devuelve: { objectName, resumableUrl }  (claves internas del microservicio)
        """
        token = self.get_token()
        logger.info(f"Solicitando resumable URL para '{filename}'...")
        resp = httpx.post(
            f"{self._backend_url}/api/v1/uploads/signed-urls",
            json={"fileNames": [filename]},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
                "App-Identifier": "dedomena"
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            logger.error(f"El backend rechazó la petición: {resp.text}")
        resp.raise_for_status()

        body = resp.json()

        # Nuevo formato: envelope con clave 'data'
        if isinstance(body, dict) and "data" in body:
            entries = body["data"]
        elif isinstance(body, list):
            # Compatibilidad con el formato anterior (lista directa)
            entries = body
        else:
            raise RuntimeError(f"Respuesta inesperada de /api/v1/uploads/signed-urls: {body}")

        if not entries:
            raise RuntimeError("La API devolvió una lista vacía en 'data'.")

        entry = entries[0]

        # Mapear claves nuevas → claves internas
        # Nuevo:    uploadId, fileName, uploadUrl
        # Interno:  objectName, resumableUrl
        object_name  = entry.get("uploadId") or entry.get("objectName") or ""
        resumable_url = entry.get("uploadUrl") or entry.get("resumableUrl") or ""

        if not resumable_url:
            raise RuntimeError(f"No se encontró 'uploadUrl' en la respuesta: {entry}")

        logger.info(f"uploadId/objectName='{object_name}' obtenido.")
        return {"objectName": object_name, "resumableUrl": resumable_url}

    # ── Paso 3: Iniciar sesión resumable ─────────────────────────────────────

    def init_resumable_session(self, resumable_url: str) -> str:
        """
        POST {resumableUrl} con x-goog-resumable: start
        Devuelve el Location header (upload URI).
        """
        logger.info("Iniciando sesión resumable en GCS...")
        resp = httpx.post(
            resumable_url,
            headers={
                "x-goog-resumable": "start",
                "Content-Type":     "text/csv",
                "Content-Length":   "0",
            },
            content=b"",
            timeout=30,
        )
        resp.raise_for_status()

        location = resp.headers.get("location") or resp.headers.get("Location")
        if not location:
            raise RuntimeError(
                f"GCS no devolvió Location header. "
                f"Status={resp.status_code}, Headers={dict(resp.headers)}"
            )

        logger.info("Sesión resumable iniciada.")
        return location

    # ── Paso 4a: Subir chunk intermedio ──────────────────────────────────────

    def upload_chunk(self, location: str, chunk_bytes: bytes, byte_offset: int) -> int:
        """
        PUT {location} con Content-Range: bytes {start}-{end}/*
        GCS debe responder 308 (Resume Incomplete).
        Devuelve el nuevo byte_offset (para el siguiente chunk).
        """
        start = byte_offset
        end   = byte_offset + len(chunk_bytes) - 1

        logger.info(f"  → Chunk intermedio: bytes {start}-{end}/* "
                    f"({len(chunk_bytes):,} bytes)")

        resp = httpx.put(
            location,
            content=chunk_bytes,
            headers={
                "Content-Type":  "text/csv",
                "Content-Range": f"bytes {start}-{end}/*",
            },
            timeout=120,
        )

        # 308 = GCS ha recibido el chunk, pide más  ✅
        # 200/201 = GCS cerró la sesión (no debería pasar aquí, pero lo manejamos)
        if resp.status_code == 308:
            new_offset = end + 1
            logger.info(f"  ✓ 308 Resume Incomplete — offset ahora en {new_offset:,}")
            return new_offset
        elif resp.status_code in (200, 201):
            # GCS cerró la sesión anticipadamente (chunk era el último implícito)
            logger.warning("GCS cerró la sesión con 200/201 en chunk intermedio.")
            return end + 1
        else:
            raise RuntimeError(
                f"GCS respondió {resp.status_code} en chunk intermedio: {resp.text[:300]}"
            )

    # ── Paso 4b: Cerrar con el chunk final ───────────────────────────────────

    def finalize_upload(self, location: str, chunk_bytes: bytes,
                        byte_offset: int) -> int:
        """
        PUT {location} con Content-Range: bytes {start}-{end}/{total}
        Cierra la sesión resumable. GCS debe responder 200 o 201.
        Devuelve el total de bytes escritos.
        """
        start = byte_offset
        end   = byte_offset + len(chunk_bytes) - 1
        total = end + 1

        logger.info(f"  → Chunk FINAL: bytes {start}-{end}/{total} "
                    f"({len(chunk_bytes):,} bytes)")

        resp = httpx.put(
            location,
            content=chunk_bytes,
            headers={
                "Content-Type":  "text/csv",
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
            timeout=120,
        )

        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"GCS respondió {resp.status_code} al finalizar: {resp.text[:300]}"
            )

        logger.info(f"  ✅ Upload completo — {total:,} bytes en GCS.")
        return total
