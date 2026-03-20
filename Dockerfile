# Dockerfile para microservicio_CRM_integration
# Usamos una imagen base ligera de Python 3.11
FROM python:3.11-slim

# Establecemos el directorio de trabajo tras la copia
WORKDIR /app

# Evitamos que Python genere archivos .pyc y habilitamos el volcado de logs sin búfer
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalamos dependencias del sistema si fueran necesarias (httpx no requiere extras de compilación)
# RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# Copiamos el archivo de requerimientos primero para aprovechar la caché de capas de Docker
COPY requirements.txt .

# Instalamos las dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el resto del código del microservicio
# El archivo .dockerignore se encargará de excluir .venv, datasets grandes, etc.
COPY . .

# Exponemos el puerto en el que corre FastAPI
EXPOSE 8000

# Comando para arrancar la aplicación
# Usamos uvicorn directamente
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
