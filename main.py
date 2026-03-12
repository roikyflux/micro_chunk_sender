"""
main.py — CSV Chunk Microservice v3 (Incremental GCS Resumable Upload)

Flujo de escritura incremental en GCS:
  ┌─────────────────────────────────────────────────────────────────┐
  │  POST /jobs                                                      │
  │    → Autentica con backend                                       │
  │    → Obtiene resumableUrl + objectName  (Paso 2)                │
  │    → Inicia sesión GCS → guarda Location  (Paso 3)              │
  │    → Escribe header CSV en GCS  (primer PUT parcial)            │
  ├─────────────────────────────────────────────────────────────────┤
  │  POST /jobs/{id}/chunks  (N veces, secuencial)                  │
  │    → Serializa rows a CSV bytes                                  │
  │    → PUT Location  Content-Range: bytes X-Y/*   → 308           │
  │    → Avanza byte_offset                                          │
  ├─────────────────────────────────────────────────────────────────┤
  │  POST /jobs/{id}/complete                                        │
  │    → Si hay datos pendientes: PUT Content-Range: bytes X-Y/T    │
  │    → Si no hay datos: PUT vacío con total conocido para cerrar  │
  │    → GCS responde 200/201 → archivo completo en bucket          │
  └─────────────────────────────────────────────────────────────────┘

NOTA IMPORTANTE sobre el tamaño mínimo de chunk de GCS:
  GCS exige que cada chunk intermedio sea múltiplo de 256 KiB (262.144 bytes)
  excepto el último. Con filas pequeñas (< 1.000 filas) puede que un chunk
  individual no alcance ese mínimo.
  SOLUCIÓN: el microservicio acumula un buffer interno hasta 256 KiB antes
  de enviar a GCS. El /complete vacía el buffer sin restricción de tamaño.
"""

import io
import csv
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

from job_store import JobStore, JobStatus
from uploader import PlatformUploader
import os
from dotenv import load_dotenv
load_dotenv()  # carga el archivo .env si existe (solo en local)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="CSV Chunk Microservice", version="3.0.0")

# ── Config ────────────────────────────────────────────────────────────────────
BACKEND_URL    = os.environ["BACKEND_URL"]
BACKEND_EMAIL  = os.environ["BACKEND_EMAIL"]
BACKEND_PASS   = os.environ["BACKEND_PASSWORD"]
API_SECRET_KEY = os.environ.get("API_SECRET_KEY")

# GCS exige chunks intermedios ≥ 256 KiB (excepto el último)
GCS_MIN_CHUNK_BYTES = 256 * 1024   # 262.144 bytes

store    = JobStore()
uploader = PlatformUploader(BACKEND_URL, BACKEND_EMAIL, BACKEND_PASS)

# ── Auth helper ───────────────────────────────────────────────────────────────
def verify_auth(x_api_key: Optional[str]):
    if API_SECRET_KEY and x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# ── Models ────────────────────────────────────────────────────────────────────
class CreateJobRequest(BaseModel):
    dataset_name: str
    headers: Optional[list[str]] = None

class ChunkRequest(BaseModel):
    chunk_index: int
    rows: list[dict | list]  # acepta JSON objects ({}) o arrays ([])

class CompleteRequest(BaseModel):
    total_rows: Optional[int] = None  # hint de validación, no requerido

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/jobs", status_code=201)
def create_job(req: CreateJobRequest, x_api_key: Optional[str] = Header(None)):
    """
    Crea el job e inicia la sesión resumable en GCS inmediatamente.
    Escribe la fila de cabeceras CSV como primer bloque en GCS.
    """
    verify_auth(x_api_key)

    job_id   = str(uuid.uuid4())
    filename = req.dataset_name if req.dataset_name.endswith(".csv") \
               else f"{req.dataset_name}.csv"

    store.create(job_id, filename=filename, headers=req.headers)

    try:
        # Paso 2: obtener URL firmada del backend
        signed = uploader.get_resumable_url(filename)

        # Paso 3: iniciar sesión resumable en GCS → obtener Location
        location = uploader.init_resumable_session(signed["resumableUrl"])

        store.set_session(job_id,
                          upload_location=location,
                          object_name=signed["objectName"],
                          resumable_url=signed["resumableUrl"])

        # Escribir la fila de headers en el buffer si se proporcionaron
        if req.headers:
            header_bytes = _rows_to_csv_bytes([req.headers])
            store.set_pending_buffer(job_id, header_bytes)
            logger.info(f"[{job_id}] Job creado — file={filename}, "
                        f"sesión GCS lista, header={len(header_bytes)} bytes en buffer")
        else:
            logger.info(f"[{job_id}] Job creado sin headers explícitos. "
                        f"Se deducirán del primer chunk recibido.")

    except Exception as e:
        store.set_status(job_id, JobStatus.FAILED, error=str(e))
        logger.error(f"[{job_id}] Error iniciando sesión GCS: {e}")
        raise HTTPException(status_code=500, detail=f"No se pudo iniciar sesión GCS: {e}")

    return {
        "job_id":  job_id,
        "status":  JobStatus.UPLOADING,
        "message": "Sesión GCS iniciada. Envía chunks con POST /jobs/{job_id}/chunks"
    }


@app.post("/jobs/{job_id}/chunks")
def upload_chunk(job_id: str, req: ChunkRequest,
                 x_api_key: Optional[str] = Header(None)):
    """
    Recibe un chunk de filas y lo escribe incrementalmente en GCS.

    El microservicio acumula bytes en un buffer interno hasta alcanzar
    el mínimo de GCS (256 KiB), momento en que hace el PUT parcial.
    Los bytes restantes quedan en buffer para el siguiente chunk.
    """
    verify_auth(x_api_key)

    job = _get_job_or_404(job_id)
    _assert_uploadable(job)

    if not req.rows:
        return {"job_id": job_id, "chunk_index": req.chunk_index,
                "status": "empty_chunk_ignored"}

    # Si no hay headers guardados y recibimos dicts, los deducimos ahora
    job_headers = job.get("headers")
    header_bytes = b""
    if not job_headers and isinstance(req.rows[0], dict):
        job_headers = list(req.rows[0].keys())
        store.set_headers(job_id, job_headers)
        header_bytes = _rows_to_csv_bytes([job_headers])
        logger.info(f"[{job_id}] Headers deducidos del chunk: {job_headers}")

    # Serializar las nuevas filas y añadir al buffer
    # Pasamos los headers del job para poder extraer dicts en orden correcto
    new_bytes = header_bytes + _rows_to_csv_bytes(req.rows, headers=job_headers)
    store.append_to_buffer(job_id, new_bytes)
    job = store.get(job_id)

    buffer      = job["pending_buffer"]
    byte_offset = job["byte_offset"]

    # Flush del buffer si supera el mínimo de GCS
    flushed_bytes = 0
    while len(buffer) >= GCS_MIN_CHUNK_BYTES:
        # Tomar exactamente GCS_MIN_CHUNK_BYTES (múltiplo de 256 KiB)
        to_send = buffer[:GCS_MIN_CHUNK_BYTES]
        buffer  = buffer[GCS_MIN_CHUNK_BYTES:]

        new_offset = uploader.upload_chunk(
            location=job["upload_location"],
            chunk_bytes=to_send,
            byte_offset=byte_offset,
        )
        flushed_bytes += len(to_send)
        byte_offset    = new_offset

    # Guardar el estado actualizado
    store.flush_buffer(job_id,
                       new_offset=byte_offset,
                       remaining_buffer=buffer,
                       rows_added=len(req.rows),
                       bytes_flushed=flushed_bytes)

    job = store.get(job_id)
    logger.info(f"[{job_id}] Chunk {req.chunk_index} — "
                f"{len(req.rows)} filas, {len(new_bytes)} bytes nuevos, "
                f"{flushed_bytes} bytes enviados a GCS, "
                f"{len(buffer)} bytes en buffer")

    return {
        "job_id":          job_id,
        "chunk_index":     req.chunk_index,
        "rows_in_chunk":   len(req.rows),
        "bytes_flushed":   flushed_bytes,
        "buffer_size":     len(buffer),
        "total_rows":      job["total_rows"],
        "byte_offset":     job["byte_offset"],
        "chunks_received": job["chunks_received"],
    }


@app.post("/jobs/{job_id}/complete")
def complete_job(job_id: str, req: CompleteRequest = CompleteRequest(),
                 x_api_key: Optional[str] = Header(None)):
    """
    Cierra la sesión resumable en GCS enviando el buffer restante
    con Content-Range: bytes {start}-{end}/{total_conocido_ahora}.

    Llamar esto cuando N8N haya enviado todos los chunks.
    """
    verify_auth(x_api_key)

    job = _get_job_or_404(job_id)

    if job["status"] == JobStatus.COMPLETED:
        return _completed_response(job_id, job)

    _assert_uploadable(job)
    store.set_status(job_id, JobStatus.COMPLETING)

    try:
        buffer      = job["pending_buffer"]
        byte_offset = job["byte_offset"]

        if len(buffer) == 0:
            # No debería ocurrir normalmente (siempre hay al menos el header),
            # pero si ocurre enviamos un PUT vacío con el total real para cerrar.
            raise RuntimeError(
                "Buffer vacío en /complete. Esto indica que todos los bytes "
                "ya fueron enviados con Content-Range parcial, lo que no debería "
                "ocurrir. Verifica que /chunks no haya enviado el último fragmento "
                "con tamaño exactamente múltiplo de 256 KiB."
            )

        # Enviar el buffer restante como chunk FINAL (conocemos el total ahora)
        total_bytes = uploader.finalize_upload(
            location=job["upload_location"],
            chunk_bytes=buffer,
            byte_offset=byte_offset,
        )

        total_rows = job["total_rows"]
        store.set_completed(job_id, total_bytes=total_bytes, total_rows=total_rows)

        logger.info(f"[{job_id}] ✅ Completado — "
                    f"{total_rows:,} filas, {total_bytes:,} bytes → {job['object_name']}")

        return _completed_response(job_id, store.get(job_id))

    except Exception as e:
        store.set_status(job_id, JobStatus.FAILED, error=str(e))
        logger.error(f"[{job_id}] ❌ Error en /complete: {e}")
        raise HTTPException(status_code=500, detail=f"Error al finalizar: {e}")


@app.get("/jobs/{job_id}/status")
def job_status(job_id: str, x_api_key: Optional[str] = Header(None)):
    """Polling de estado para N8N."""
    verify_auth(x_api_key)

    job = _get_job_or_404(job_id)
    return {
        "job_id":          job_id,
        "status":          job["status"],
        "filename":        job["filename"],
        "object_name":     job.get("object_name"),
        "total_rows":      job["total_rows"],
        "total_bytes":     job["total_bytes"],
        "byte_offset":     job["byte_offset"],
        "buffer_size":     len(job.get("pending_buffer", b"")),
        "chunks_received": job["chunks_received"],
        "error":           job.get("error"),
        "created_at":      job["created_at"],
        "updated_at":      job["updated_at"],
    }


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_auth(x_api_key)
    _get_job_or_404(job_id)
    store.delete(job_id)
    return {"job_id": job_id, "deleted": True}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rows_to_csv_bytes(rows: list, headers: list[str] | None = None) -> bytes:
    """
    Serializa una lista de filas a bytes CSV (sin BOM, UTF-8).

    Acepta dos formatos en 'rows':
      - list[list]  → cada fila es un array de valores (formato original)
      - list[dict]  → cada fila es un objeto JSON. Requiere 'headers' para
                      extraer los valores en el orden correcto.
                      Si un campo no existe en el dict, se escribe vacío.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    for row in rows:
        if isinstance(row, dict):
            if not headers:
                raise ValueError(
                    "Se recibieron filas como dict pero no hay headers definidos en el job. "
                    "Asegúrate de enviar 'headers' en el POST /jobs."
                )
            writer.writerow([row.get(h, "") for h in headers])
        else:
            writer.writerow(row)
    return buf.getvalue().encode("utf-8")

def _get_job_or_404(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job

def _assert_uploadable(job: dict):
    if job["status"] == JobStatus.FAILED:
        raise HTTPException(status_code=409,
                            detail="Job fallido — crea un nuevo job para reintentar")
    if job["status"] == JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Job ya completado")

def _completed_response(job_id: str, job: dict) -> dict:
    return {
        "job_id":      job_id,
        "status":      JobStatus.COMPLETED,
        "object_name": job["object_name"],
        "filename":    job["filename"],
        "total_rows":  job["total_rows"],
        "total_bytes": job["total_bytes"],
    }
