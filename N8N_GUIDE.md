# N8N Integration Guide — CSV Chunk Microservice v2

## Flujo completo en N8N

```
[Trigger]
   │
   ▼
[1. Create Job]  ──────────────────────────────────────────────────────────────
   │  POST /jobs → devuelve job_id
   ▼
[2. Loop: Fetch page N from source API → POST /jobs/{id}/chunks]  (N veces)
   │  chunk_index = $runIndex (0, 1, 2 ...)
   ▼
[3. Complete Job]
   │  POST /jobs/{id}/complete → microservicio sube a GCS
   ▼
[4. Poll Status]  (Wait 3s → GET /jobs/{id}/status → IF completed / failed / retry)
   │
   ▼
[5. (opcional) DELETE /jobs/{id}]  ← libera memoria
```

---

## Variables de entorno en N8N
`Settings > Variables`:

```
CSV_SERVICE_URL = https://csv-chunk-service-xxxx.europe-southwest1.run.app
CSV_SERVICE_KEY = tu-api-secret-key
```

---

## Nodo 1 — Create Job
**Type:** HTTP Request

| Campo       | Valor                                   |
|-------------|-----------------------------------------|
| Method      | POST                                    |
| URL         | `{{ $env.CSV_SERVICE_URL }}/jobs`       |
| Header      | `X-Api-Key: {{ $env.CSV_SERVICE_KEY }}` |

**Body (JSON):**
```json
{
  "dataset_name": "{{ $json.dataset_name }}",
  "headers": ["id", "nombre", "email", "fecha"],
  "total_chunks": {{ $json.total_pages }}
}
```

**Guardar en variable:** `{{ $json.job_id }}`

---

## Nodo 2 — Loop + Send Chunk
Usa **SplitInBatches** con tu paginación.  
En cada iteración llama a tu API origen y luego:

**Type:** HTTP Request

| Campo  | Valor                                                         |
|--------|---------------------------------------------------------------|
| Method | POST                                                          |
| URL    | `{{ $env.CSV_SERVICE_URL }}/jobs/{{ $vars.job_id }}/chunks`   |
| Header | `X-Api-Key: {{ $env.CSV_SERVICE_KEY }}`                       |

**Body (JSON):**
```json
{
  "chunk_index": {{ $runIndex }},
  "rows": {{ $json.data }}
}
```

> ⚠️ `rows` debe ser array de arrays: `[["val1","val2"], ["val3","val4"]]`  
> Si tu API devuelve array de objetos, usa un nodo **Code** para transformarlo:
> ```js
> const keys = ["id", "nombre", "email", "fecha"];
> return items.map(item => ({
>   json: { rows: [keys.map(k => item.json[k])] }
> }));
> ```

---

## Nodo 3 — Complete Job

**Type:** HTTP Request

| Campo  | Valor                                                            |
|--------|------------------------------------------------------------------|
| Method | POST                                                             |
| URL    | `{{ $env.CSV_SERVICE_URL }}/jobs/{{ $vars.job_id }}/complete`    |
| Header | `X-Api-Key: {{ $env.CSV_SERVICE_KEY }}`                          |

**Body (JSON):** `{}` (o con `"total_rows"` si quieres validación)

---

## Nodo 4 — Poll Status
Combina **Wait** (3s) + **HTTP Request** + **IF**:

**HTTP Request:**

| Campo  | Valor                                                           |
|--------|-----------------------------------------------------------------|
| Method | GET                                                             |
| URL    | `{{ $env.CSV_SERVICE_URL }}/jobs/{{ $vars.job_id }}/status`     |
| Header | `X-Api-Key: {{ $env.CSV_SERVICE_KEY }}`                         |

**IF node:**
- `{{ $json.status }} === "completed"` → ✅ sigue el flujo (tienes `object_name` en el response)
- `{{ $json.status }} === "failed"`    → ❌ rama de error
- Cualquier otro valor (`processing`)  → vuelve al nodo Wait

**Response cuando completed:**
```json
{
  "job_id":      "abc-123",
  "status":      "completed",
  "total_rows":  3000000,
  "file_size":   52428800,
  "object_name": "reporte_ventas._2026-03-09_101236.csv",
  "filename":    "reporte_ventas.csv"
}
```

---

## Configuración recomendada por nodo
- **Retry On Fail:** activado en todos los HTTP Request (3 intentos, 2s delay)
- Los chunks son idempotentes por `chunk_index` — los retries son seguros
- Si un job queda en `failed`, crea uno nuevo con `POST /jobs` y reenvía desde el chunk 0
