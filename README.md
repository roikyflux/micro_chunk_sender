# Chunk Manager — CSV Upload Microservice

Microservicio en Python/FastAPI que recibe datos en chunks desde N8N, los ensambla como CSV y los sube incrementalmente a Google Cloud Storage mediante Resumable Upload.

---

## Requisitos del VPS

- Ubuntu 22.04 / 24.04 (o Debian equivalente)
- Python 3.11 o superior
- Acceso SSH con usuario `sudo`

Verificar la versión de Python:
```bash
python3 --version
```

---

## 1. Copiar el proyecto al VPS

Desde tu máquina local, copia los archivos al servidor:

```bash
scp -r /ruta/local/microservicio_CRM_integration usuario@IP_VPS:/opt/chunk_manager
```

O si el proyecto está en Git:

```bash
ssh usuario@IP_VPS
sudo mkdir -p /opt/chunk_manager
sudo chown $USER:$USER /opt/chunk_manager
git clone https://URL_DE_TU_REPO.git /opt/chunk_manager
```

---

## 2. Crear el entorno virtual e instalar dependencias

Conéctate al VPS y ejecuta:

```bash
cd /opt/chunk_manager

# Crear entorno virtual
python3 -m venv .venv

# Instalar dependencias
.venv/bin/pip install -r requirements.txt
```

---

## 3. Crear el archivo de configuración `.env`

```bash
nano /opt/chunk_manager/.env
```

Pega el siguiente contenido y rellena con tus valores reales:

```env
BACKEND_URL=https://tu-backend.example.com
BACKEND_EMAIL=usuario@tudominio.com
BACKEND_PASSWORD=tu_password_segura
API_SECRET_KEY=clave-secreta-muy-larga-que-solo-n8n-conoce
```

> **⚠️ Importante:** este archivo contiene credenciales. Restringe sus permisos:
> ```bash
> chmod 600 /opt/chunk_manager/.env
> ```

---

## 4. Probar que el servicio arranca correctamente

Antes de configurarlo como servicio permanente, verifica que funciona:

```bash
cd /opt/chunk_manager
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

Abre otro terminal y comprueba:

```bash
curl http://localhost:8000/health
```

Deberías ver:
```json
{"status": "ok", "timestamp": "2026-..."}
```

Si funciona, detén el proceso con `Ctrl+C` y continúa al paso 5.

---

## 5. Configurar como servicio systemd (arranque automático)

Esto hace que el servicio se inicie automáticamente con el servidor y se reinicie si falla.

### 5.1 Crear el archivo de servicio

```bash
sudo nano /etc/systemd/system/chunk-manager.service
```

Pega el siguiente contenido. **Reemplaza `tu_usuario` por tu usuario real del VPS**:

```ini
[Unit]
Description=Chunk Manager - CSV Upload Microservice
After=network.target

[Service]
Type=simple
User=tu_usuario
WorkingDirectory=/opt/chunk_manager
EnvironmentFile=/opt/chunk_manager/.env
ExecStart=/opt/chunk_manager/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 5.2 Habilitar e iniciar el servicio

```bash
# Recargar systemd para que detecte el nuevo servicio
sudo systemctl daemon-reload

# Habilitar el servicio para que arranque con el sistema
sudo systemctl enable chunk-manager

# Iniciar el servicio ahora
sudo systemctl start chunk-manager
```

### 5.3 Verificar que está corriendo

```bash
sudo systemctl status chunk-manager
```

Deberías ver `Active: active (running)`.

---

## 6. Ver los logs en tiempo real

```bash
# Últimas 100 líneas
sudo journalctl -u chunk-manager -n 100

# Seguimiento en tiempo real (como tail -f)
sudo journalctl -u chunk-manager -f
```

---

## 7. Exponer el servicio al exterior (opcional pero recomendado)

Por defecto, el servicio corre en el puerto `8000`. Si quieres accederlo desde N8N con HTTPS, configura **Nginx como proxy inverso**.

### 7.1 Instalar Nginx

```bash
sudo apt update && sudo apt install nginx -y
```

### 7.2 Crear la configuración del sitio

```bash
sudo nano /etc/nginx/sites-available/chunk-manager
```

```nginx
server {
    listen 80;
    server_name TU_DOMINIO_O_IP;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

### 7.3 Activar el sitio

```bash
sudo ln -s /etc/nginx/sites-available/chunk-manager /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

> Si quieres HTTPS, usa Certbot:
> ```bash
> sudo apt install certbot python3-certbot-nginx -y
> sudo certbot --nginx -d TU_DOMINIO
> ```

---

## 7. Despliegue con Docker (Recomendado)

Si prefieres usar contenedores, puedes levantar el servicio fácilmente.

### 7.1 Construir y ejecutar con Docker Compose

Asegúrate de tener un archivo `.env` configurado. Luego ejecuta:

```bash
docker compose up -d --build
```

Esto construirá la imagen y levantará el contenedor en segundo plano, exponiendo el puerto `8000`.

### 7.2 Comandos útiles de Docker

| Acción | Comando |
|---|---|
| Ver logs | `docker compose logs -f` |
| Detener servicio | `docker compose down` |
| Reiniciar | `docker compose restart` |
| Ver estado | `docker ps` |

---

## 8. Comandos útiles del día a día

| Acción | Comando |
|---|---|
| Ver estado | `sudo systemctl status chunk-manager` |
| Reiniciar servicio | `sudo systemctl restart chunk-manager` |
| Detener servicio | `sudo systemctl stop chunk-manager` |
| Ver logs en vivo | `sudo journalctl -u chunk-manager -f` |
| Ver logs de hoy | `sudo journalctl -u chunk-manager --since today` |

---

## 9. Actualizar el código

Cuando hagas cambios en el código y los quieras subir al VPS:

```bash
# En el VPS
cd /opt/chunk_manager
git pull origin main

# Reiniciar el servicio para aplicar cambios
sudo systemctl restart chunk-manager
```

---

## 10. Endpoints disponibles

La URL base del servicio es `http://IP_VPS:8000` (o tu dominio si configuraste Nginx).

| Acción | Método | Endpoint |
|---|---|---|
| Health check | `GET` | `/health` |
| Crear job | `POST` | `/jobs` |
| Enviar chunk | `POST` | `/jobs/{id}/chunks` |
| Finalizar CSV | `POST` | `/jobs/{id}/complete` |
| Consultar estado | `GET` | `/jobs/{id}/status` |
| Eliminar job | `DELETE` | `/jobs/{id}` |
| Documentación | `GET` | `/docs` |

> Todos los endpoints (excepto `/health`) requieren el header:
> ```
> x-api-key: TU_API_SECRET_KEY
> ```

---

## 11. Estructura del proyecto

```
/opt/chunk_manager/
├── main.py          # API FastAPI con todos los endpoints
├── job_store.py     # Almacenamiento de trabajos en memoria
├── uploader.py      # Lógica de subida resumable a GCS
├── requirements.txt # Dependencias Python
├── .env             # Credenciales (NO subir a Git)
└── .venv/           # Entorno virtual Python (NO subir a Git)
```
