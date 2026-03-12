Aquí tienes el texto **limpio, organizado y estructurado en Markdown**, eliminando ruido de la conversación, repeticiones y dejando claro el flujo técnico. Está listo para guardarse como `conversation_microservicio_csv.md`.

---

# Arquitectura para Generación de CSV por Chunks con N8N y Google Cloud

## Contexto

Estoy implementando un flujo en **N8N** que recupera datasets desde diferentes orígenes (por ejemplo CRMs). Estos datasets pueden ser muy grandes, llegando a **más de 3 millones de registros**.

El problema es que construir el **CSV completo dentro de N8N** usando paginación en llamadas HTTP **no es eficiente ni escalable**.

Por esta razón se necesita una arquitectura que permita:

* Procesar los datasets **por lotes (chunks)**.
* Que cada chunk sea procesado externamente.
* Construir el **CSV de forma incremental**.
* Subir el archivo final al **bucket de Google Cloud Storage (GCS)**.

En este modelo:

* **N8N solo orquesta el proceso**
* Un **microservicio externo en Python** maneja la construcción del CSV y la subida incremental a GCS.

---

# Arquitectura Propuesta

## Flujo general

```
N8N Flow
  │
  ├─ POST /jobs                → crea un job y devuelve job_id
  ├─ POST /jobs/{id}/chunks    → envía chunks de datos
  ├─ POST /jobs/{id}/complete  → finaliza el CSV
  └─ GET  /jobs/{id}/status    → consulta estado
```

El microservicio:

1. Recibe chunks
2. Maneja el buffer
3. Construye el CSV
4. Lo sube a GCS mediante **Resumable Upload**

---

# Componentes del Proyecto

Archivos principales generados:

| Archivo            | Descripción                                 |
| ------------------ | ------------------------------------------- |
| `main.py`          | API FastAPI con endpoints del microservicio |
| `job_store.py`     | Almacenamiento de jobs en memoria           |
| `uploader.py`      | Maneja la subida incremental a GCS          |
| `Dockerfile`       | Contenedor para Cloud Run                   |
| `deploy.sh`        | Script de despliegue                        |
| `requirements.txt` | Dependencias Python                         |
| `N8N_GUIDE.md`     | Guía para integrar N8N                      |

---

# Endpoints del Microservicio

## Crear Job

```
POST /jobs
```

Respuesta:

```json
{
  "job_id": "abc123"
}
```

---

## Enviar Chunk

```
POST /jobs/{job_id}/chunks
```

Body:

```json
{
  "chunk_index": 0,
  "rows": [...]
}
```

Características:

* Los chunks se deduplican usando `chunk_index`
* Permite **reintentos seguros desde N8N**

---

## Finalizar CSV

```
POST /jobs/{job_id}/complete
```

Acciones:

* Ensambla el CSV
* Cierra la sesión de upload
* Sube el archivo final a GCS

---

## Consultar Estado

```
GET /jobs/{job_id}/status
```

Respuesta:

```json
{
  "status": "completed"
}
```

Estados posibles:

* `running`
* `completed`
* `failed`

---

# Flujo de Datos para Dataset de 3M Registros

Ejemplo:

```
N8N paginando API origen (50k registros por página)

→ 60 llamadas POST /chunks
→ POST /complete
→ GET /status (polling)

Resultado final:
gs://bucket/datasets/nombre_20260309_xxxxxx.csv
```

---

# Integración con Backend Existente

La plataforma ya tiene un procedimiento para subir archivos a GCS mediante un backend.

El flujo es:

## Paso 1 — Login

```
POST /login
Header: App-Identifier: dedomena
```

Devuelve:

```
Authorization Token
```

---

## Paso 2 — Obtener URL Resumable

```
POST /datasets/resumable-url
```

Body:

```json
{
  "fileNames": ["archivo.csv"]
}
```

Respuesta:

```json
{
  "objectName": "...",
  "resumableUrl": "..."
}
```

---

## Paso 3 — Iniciar Sesión Resumable

```
POST resumableUrl
Header: x-goog-resumable: start
Content-Length: 0
```

Respuesta:

```
Location header → URL real de subida
```

---

## Paso 4 — Subir Archivo

```
PUT location
Content-Range: bytes X-Y/TOTAL
```

---

# Problema del Content-Range

El header:

```
Content-Range: bytes X-Y/TOTAL
```

requiere conocer **el tamaño total del archivo**.

Pero cuando el CSV se genera por chunks:

* El tamaño total **no se conoce hasta el final**.

---

# Solución: Resumable Upload Incremental

GCS permite usar un tamaño desconocido con `*`.

Ejemplo:

## Chunk intermedio

```
Content-Range: bytes 0-262143/*
```

## Chunk final

```
Content-Range: bytes 262144-389541/389542
```

---

# Restricción de GCS

Los chunks intermedios deben ser:

```
≥ 256 KB
```

Con datasets paginados de **1000 filas**, los chunks suelen ser:

```
20KB – 80KB
```

Esto provoca errores.

---

# Solución Implementada

El microservicio mantiene un **buffer interno**.

Ejemplo:

```
Chunk 1 → 20 KB
buffer = 20 KB

Chunk 2 → 20 KB
buffer = 40 KB

...

Chunk 14 → 280 KB
→ flush 256 KB a GCS
→ quedan 24 KB en buffer
```

Cuando se llama `/complete`:

```
PUT final → buffer restante
Content-Range X-Y/TOTAL
```

---

# Problemas Encontrados en N8N

## Error HTTP 308

GCS devuelve:

```
308 Resume Incomplete
```

Esto **no es un error**.

Significa:

```
chunk recibido correctamente
envía el siguiente
```

Pero N8N lo interpreta como error.

---

## Timeouts en N8N

Los nodos `Create CSV` y `Calculate` generaban timeouts porque:

* Intentaban **construir el CSV completo en memoria**
* Manipulaban **binarios grandes**
* N8N tiene timeout de **300s**

---

# Problema de Duplicación de Headers

El nodo de creación de CSV generaba:

```
chunk 1
id,name,email
1,Ana,...

chunk 2
id,name,email
1001,Bob,...
```

Esto provoca headers repetidos en el CSV final.

---

# Flujo Recomendado en N8N

## Fase 1 — Inicialización

```
Login Backend
↓
Get Resumable URL
↓
POST /jobs
```

---

## Fase 2 — Loop de Paginación

```
Set Pagination
↓
Get Dataset (limit=1000)
↓
Format Dataset
↓
POST /jobs/{id}/chunks
↓
Check hasMore
```

Si `hasMore = true`:

```
volver a Get Dataset
```

---

## Fase 3 — Cierre

```
POST /jobs/{id}/complete
↓
GET /jobs/{id}/status (polling)
↓
completed
```

---

# URL del Microservicio en Cloud Run

Formato general:

```
https://SERVICE_NAME-HASH-REGION.a.run.app
```

Ejemplo:

```
https://csv-chunk-service-587494419013-ew.a.run.app
```

---

## Endpoints completos

| Acción       | Método | URL                   |
| ------------ | ------ | --------------------- |
| Crear job    | POST   | `/jobs`               |
| Enviar chunk | POST   | `/jobs/{id}/chunks`   |
| Finalizar    | POST   | `/jobs/{id}/complete` |
| Estado       | GET    | `/jobs/{id}/status`   |
| Health       | GET    | `/health`             |

---

# Conclusión

Intentar manejar la subida incremental **directamente desde N8N** no es viable para datasets grandes debido a:

| Problema                 | N8N | Microservicio |
| ------------------------ | --- | ------------- |
| Cursor de bytes          | ❌   | ✅             |
| Chunks <256KB            | ❌   | ✅             |
| Tamaño total desconocido | ❌   | ✅             |
| Timeouts binarios        | ❌   | ✅             |

El **microservicio en Python** resuelve estos problemas:

* Maneja offsets de bytes
* Implementa buffering
* Controla el protocolo resumable de GCS
* Evita que N8N procese archivos grandes

---

Si quieres, en el próximo paso también puedo darte una **versión mucho más profesional tipo documentación técnica (nivel arquitectura SaaS / ADR)** que quedaría perfecta para guardar en tu repositorio o compartir con tu equipo.
