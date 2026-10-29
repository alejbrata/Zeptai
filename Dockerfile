# ─── Imagen base ──────────────────────────────────────────────────────────────
# python:3.11-slim es Debian Bookworm sin paquetes innecesarios.
# Usamos 3.11 (no 3.13) porque algunas dependencias pesadas (psycopg2, pandas)
# tienen wheels precompilados para 3.11, lo que acelera el build.
FROM python:3.11-slim

# ─── Variables de entorno globales ────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py

# ─── Dependencias del sistema ─────────────────────────────────────────────────
# gcc + libpq-dev: necesarios para compilar psycopg2-binary si no hay wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# ─── Directorio de trabajo raiz ───────────────────────────────────────────────
WORKDIR /app

# ─── Instalar dependencias Python ─────────────────────────────────────────────
# Se copia solo requirements.txt primero para aprovechar la cache de capas:
# si no cambian las dependencias, Docker no reinstala todo al rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── Copiar codigo fuente ─────────────────────────────────────────────────────
# run_scheduler.py esta en la raiz del repo (no dentro de TICKETIA_PRO),
# por eso necesita su propia instruccion COPY.
COPY run_scheduler.py /app/run_scheduler.py
COPY entrypoint.sh /app/entrypoint.sh
COPY ./TICKETIA_PRO /app/TICKETIA_PRO
RUN chmod +x /app/entrypoint.sh

# ─── Directorio de trabajo de la aplicacion Flask ─────────────────────────────
# Todos los imports internos (from core.xxx, from modules.xxx) asumen que
# el CWD es TICKETIA_PRO, por eso se cambia aqui.
WORKDIR /app/TICKETIA_PRO

# ─── Puerto expuesto ──────────────────────────────────────────────────────────
EXPOSE 5000

# ─── Comando por defecto: Gunicorn ────────────────────────────────────────────
# - 2 workers: suficiente para demo/testing (1 worker por CPU logica aprox.)
# - timeout 120s: algunas llamadas a GPT-4o pueden tardar >30s con tool calls
# - bind 0.0.0.0 para que Docker pueda redirigir el puerto
# Para sobreescribir en docker-compose usar la clave 'command:'
CMD ["/app/entrypoint.sh"]
