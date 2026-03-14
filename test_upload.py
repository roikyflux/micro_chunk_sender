"""
test_upload.py — Test completo de subida paginada a GCS vía el microservicio.

Simula exactamente lo que haría N8N:
  1. POST /jobs          → crea el job
  2. POST /chunks x N    → envía páginas del CSV (como JSON objects, igual que un CRM)
  3. POST /complete      → cierra el archivo en GCS
  4. GET  /status        → confirma el estado final

Uso:
  python test_upload.py --csv test_data.csv --page-size 1000
  python test_upload.py --csv test_data.csv --page-size 1000 --format list   # usa arrays
  python test_upload.py --csv test_data.csv --page-size 1000 --dataset-name mi_prueba
"""

import csv
import sys
import json
import time
import argparse
import requests
from datetime import datetime
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ─────────────────────────────────────────────────────────────
SERVICE_URL = "http://72.62.22.15:8000"
API_KEY     = "csv-service-local-2026"
HEADERS     = {
    "Content-Type": "application/json",
    "x-api-key": API_KEY,
}

# ── Colores para la terminal ──────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def log(level: str, msg: str, data: dict = None):
    """Log con timestamp, nivel y datos opcionales."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    color = {
        "INFO":    BLUE,
        "OK":      GREEN,
        "WARN":    YELLOW,
        "ERROR":   RED,
        "STEP":    CYAN,
        "SUMMARY": BOLD,
    }.get(level, RESET)

    print(f"{color}[{ts}] [{level:7s}]{RESET} {msg}")
    if data:
        for k, v in data.items():
            print(f"           {CYAN}↳ {k}:{RESET} {v}")

def separator(title: str = ""):
    line = "─" * 60
    if title:
        print(f"\n{BOLD}{CYAN}{'─'*20} {title} {'─'*20}{RESET}\n")
    else:
        print(f"{CYAN}{line}{RESET}")

# ── Leer CSV ──────────────────────────────────────────────────────────────────
def load_csv(filepath: str) -> tuple[list[str], list[list]]:
    """Lee el CSV y devuelve (headers, rows como listas)."""
    path = Path(filepath)
    if not path.exists():
        log("ERROR", f"Archivo no encontrado: {filepath}")
        sys.exit(1)

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows    = [row for row in reader]

    log("INFO", f"CSV cargado", {
        "archivo":  filepath,
        "columnas": len(headers),
        "headers":  ", ".join(headers),
        "filas":    len(rows),
    })
    return headers, rows


def load_json(filepath: str) -> tuple[list[str], list[dict]]:
    """Lee un JSON array de objetos y devuelve (headers, rows como dicts)."""
    path = Path(filepath)
    if not path.exists():
        log("ERROR", f"Archivo no encontrado: {filepath}")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list) or not data:
        log("ERROR", "El JSON debe ser un array de objetos no vacío")
        sys.exit(1)

    if not isinstance(data[0], dict):
        log("ERROR", "El JSON debe ser un array de objetos (dicts), no de arrays")
        sys.exit(1)

    headers = list(data[0].keys())

    log("INFO", "JSON cargado", {
        "archivo":          filepath,
        "columnas":         len(headers),
        "headers":          ", ".join(headers),
        "filas":            len(data),
        "muestra_registro": json.dumps(data[0], ensure_ascii=False)[:120] + "...",
    })
    return headers, data


def rows_to_dicts(headers: list[str], rows: list[list]) -> list[dict]:
    """Convierte lista de listas a lista de dicts (simula la respuesta JSON de un CRM)."""
    return [dict(zip(headers, row)) for row in rows]

# ── Paginar ───────────────────────────────────────────────────────────────────
def paginate(rows: list[list], page_size: int) -> list[list[list]]:
    """Divide rows en páginas de page_size filas."""
    pages = [rows[i:i + page_size] for i in range(0, len(rows), page_size)]
    log("INFO", f"Paginación calculada", {
        "page_size":   page_size,
        "total_pages": len(pages),
        "total_rows":  len(rows),
    })
    return pages

# ── PASO 1: Crear Job ─────────────────────────────────────────────────────────
def create_job(dataset_name: str, email: str, password: str, headers: list[str] = None) -> str:
    separator("PASO 1: Crear Job")
    log("STEP", f"POST {SERVICE_URL}/jobs")
    
    payload = {
        "dataset_name": dataset_name,
        "email": email,
        "password": password
    }
    if headers:
        payload["headers"] = headers
        
    log("INFO", "Body enviado", {k: (v if k != "password" else "***") for k, v in payload.items()})

    t0   = time.time()
    resp = requests.post(
        f"{SERVICE_URL}/jobs",
        headers=HEADERS,
        json=payload,
        timeout=60,
    )
    elapsed = time.time() - t0

    if resp.status_code != 201:
        log("ERROR", f"Falló la creación del job", {
            "status_code": resp.status_code,
            "body":        resp.text[:500],
        })
        sys.exit(1)

    data   = resp.json()
    job_id = data["job_id"]

    log("OK", f"Job creado en {elapsed:.2f}s", {
        "job_id":  job_id,
        "status":  data["status"],
        "mensaje": data["message"],
    })
    return job_id

# ── PASO 2: Enviar Chunks ─────────────────────────────────────────────────────
def send_chunks(job_id: str, pages: list, row_format: str) -> dict:
    separator("PASO 2: Enviar Chunks")
    log("INFO", f"Formato de filas: {BOLD}'{row_format}'{RESET} "
        f"({'dict/JSON como CRM real' if row_format == 'json' else 'list/array'})")
    total_pages    = len(pages)
    total_flushed  = 0
    total_buffered = 0
    total_rows     = 0

    for idx, page in enumerate(pages):
        log("STEP", f"Chunk {idx + 1}/{total_pages} — {len(page)} filas "
            f"(muestra: {str(page[0])[:80]}...)")

        t0   = time.time()
        resp = requests.post(
            f"{SERVICE_URL}/jobs/{job_id}/chunks",
            headers=HEADERS,
            json={"chunk_index": idx, "rows": page},
            timeout=60,
        )
        elapsed = time.time() - t0

        if resp.status_code != 200:
            log("ERROR", f"Error en chunk {idx}", {
                "status_code": resp.status_code,
                "body":        resp.text[:500],
            })
            sys.exit(1)

        data = resp.json()
        total_flushed  += data["bytes_flushed"]
        total_buffered  = data["buffer_size"]
        total_rows     += data["rows_in_chunk"]

        gcs_action = "→ Enviado a GCS" if data["bytes_flushed"] > 0 else "→ En buffer (esperando 256 KB)"

        log("OK", f"Chunk {idx + 1} recibido en {elapsed:.2f}s", {
            "filas_en_chunk":     data["rows_in_chunk"],
            "bytes_a_GCS":        f"{data['bytes_flushed']:,} bytes",
            "en_buffer_ahora":    f"{data['buffer_size']:,} bytes",
            "total_rows_hasta":   data["total_rows"],
            "byte_offset_gcs":    f"{data['byte_offset']:,}",
            "chunks_procesados":  data["chunks_received"],
            "acción":             gcs_action,
        })

        # Pequeña pausa para no saturar el servicio
        if idx < total_pages - 1:
            time.sleep(0.1)

    separator()
    log("INFO", "Resumen de chunks", {
        "total_chunks":        total_pages,
        "total_filas_enviadas": total_rows,
        "bytes_enviados_GCS":  f"{total_flushed:,}",
        "bytes_en_buffer":     f"{total_buffered:,}",
    })
    return {"total_flushed": total_flushed, "total_buffered": total_buffered}

# ── PASO 3: Completar Job ─────────────────────────────────────────────────────
def complete_job(job_id: str) -> dict:
    separator("PASO 3: Completar Job (flush final → GCS)")
    log("STEP", f"POST {SERVICE_URL}/jobs/{job_id}/complete")
    log("INFO", "Enviando el buffer restante a GCS con el tamaño total...")

    t0   = time.time()
    resp = requests.post(
        f"{SERVICE_URL}/jobs/{job_id}/complete",
        headers=HEADERS,
        json={},
        timeout=60,
    )
    elapsed = time.time() - t0

    if resp.status_code != 200:
        log("ERROR", "Error al completar el job", {
            "status_code": resp.status_code,
            "body":        resp.text[:500],
        })
        sys.exit(1)

    data = resp.json()
    log("OK", f"¡Job completado en {elapsed:.2f}s!", {
        "status":       data["status"],
        "object_name":  data["object_name"],
        "filename":     data["filename"],
        "total_rows":   f"{data['total_rows']:,}",
        "total_bytes":  f"{data['total_bytes']:,} bytes ({data['total_bytes'] / 1024:.1f} KB)",
    })
    return data

# ── PASO 4: Verificar Estado ──────────────────────────────────────────────────
def check_status(job_id: str):
    separator("PASO 4: Verificar Estado Final")
    log("STEP", f"GET {SERVICE_URL}/jobs/{job_id}/status")

    resp = requests.get(
        f"{SERVICE_URL}/jobs/{job_id}/status",
        headers=HEADERS,
        timeout=30,
    )
    data = resp.json()

    log("INFO", "Estado del job", {
        "job_id":          data["job_id"],
        "status":          data["status"],
        "filename":        data["filename"],
        "object_name":     data.get("object_name", "N/A"),
        "total_rows":      f"{data['total_rows']:,}",
        "total_bytes":     f"{data['total_bytes']:,} bytes",
        "chunks_recibidos": data["chunks_received"],
        "byte_offset_GCS": f"{data['byte_offset']:,}",
        "buffer_restante": f"{data['buffer_size']} bytes",
        "creado_en":       data["created_at"],
        "actualizado_en":  data["updated_at"],
        "error":           data.get("error") or "ninguno",
    })

    if data["status"] == "completed":
        log("OK", "✅ El archivo está disponible en GCS.")
    else:
        log("WARN", f"Estado inesperado: {data['status']}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Test completo de subida CSV al microservicio")
    parser.add_argument("input",           nargs="?", default="test_data.csv", help="Archivo de entrada (.csv o .json)")
    parser.add_argument("--page-size",    type=int, default=1000,     help="Filas por chunk")
    parser.add_argument("--dataset-name", default="",                 help="Nombre del dataset en GCS (sin extensión)")
    parser.add_argument("--format",       default="json",             help="Solo para CSV: 'json' (dicts) o 'list' (arrays)")
    parser.add_argument("--email",        default=os.environ.get("BACKEND_EMAIL", ""),    help="Email para el backend")
    parser.add_argument("--password",     default=os.environ.get("BACKEND_PASSWORD", ""), help="Password para el backend")
    args = parser.parse_args()
    
    if not args.email or not args.password:
        log("ERROR", "Se requieren parámetros --email y --password para el test.")
        sys.exit(1)

    dataset_name = args.dataset_name or f"test_crm4_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    separator("INICIO DEL TEST")
    log("INFO", "Configuración", {
        "service_url":  SERVICE_URL,
        "input_file":   args.input,
        "page_size":    args.page_size,
        "dataset_name": dataset_name,
        "email":        args.email or "(desde .env del servicio)",
    })

    t_start = time.time()

    # Cargar datos según el tipo de archivo
    file_ext = Path(args.input).suffix.lower()

    if file_ext == ".json":
        log("INFO", "Archivo JSON detectado — cargando dicts directamente (sin conversión)")
        headers, rows = load_json(args.input)
        pages         = paginate(rows, args.page_size)
        row_format    = "json"
    else:
        log("INFO", "Archivo CSV detectado")
        headers, rows = load_csv(args.input)
        pages_raw     = paginate(rows, args.page_size)
        if args.format == "json":
            log("INFO", "Convirtiendo filas a dicts — simula respuesta JSON de un CRM")
            pages = [rows_to_dicts(headers, page) for page in pages_raw]
        else:
            pages = pages_raw
        row_format = args.format

    # Ejecutar flujo completo
    # Solo enviamos cabeceras iniciales si el formato es 'list'.
    # Si es 'json', dejamos que el microservicio las deduzca automáticamente.
    headers_for_job = None if row_format == "json" else headers
    job_id = create_job(dataset_name, email=args.email, password=args.password, headers=headers_for_job)
    chunk_stats = send_chunks(job_id, pages, args.format)
    final = complete_job(job_id)
    check_status(job_id)

    # Resumen final
    t_total = time.time() - t_start
    separator("RESUMEN FINAL")
    log("SUMMARY", "Test completado con éxito", {
        "tiempo_total":     f"{t_total:.2f}s",
        "job_id":           job_id,
        "dataset_name":     dataset_name,
        "total_filas":      f"{len(rows):,}",
        "total_chunks":     len(pages),
        "filas_por_chunk":  args.page_size,
        "bytes_en_GCS":     f"{final['total_bytes']:,} ({final['total_bytes'] / 1024:.1f} KB)",
        "object_name_GCS":  final["object_name"],
    })
    print(f"\n{GREEN}{BOLD}🎉 ¡Subida exitosa!{RESET}\n")

if __name__ == "__main__":
    main()
