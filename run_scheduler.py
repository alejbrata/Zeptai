"""
Scheduler persistente con APScheduler + SQLAlchemy JobStore.

Mejora respecto al scheduler anterior (libreria 'schedule'):
- Los jobs se almacenan en la base de datos: sobreviven a reinicios del servidor.
- Si el servidor cae antes de ejecutar un job, APScheduler lo ejecuta al reiniciar
  (coalesce=True evita ejecuciones multiples acumuladas).
- ThreadPoolExecutor con max_workers=4 permite ejecucion concurrente de agentes
  para distintos usuarios sin bloquearse entre si.

Uso:
    python run_scheduler.py          (desde la raiz del repositorio)

En produccion (Docker/Kubernetes) se puede desplegar como un servicio separado
junto a la app Flask principal, apuntando a la misma base de datos.
"""

import os
import sys
from datetime import datetime

# Permitir imports relativos a TICKETIA_PRO (app.py, modules/, etc.)
_APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'TICKETIA_PRO')
sys.path.insert(0, _APP_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from app import app
from modules.proactive.scheduler import run_daily_tasks

# ── Configuracion ─────────────────────────────────────────────────────────────
_DB_URI = os.environ.get('DATABASE_URL') or \
    'sqlite:///' + os.path.join(_APP_DIR, 'instance', 'zeptai.db')

# Corregir prefijo postgres:// → postgresql:// (Heroku legacy)
if _DB_URI.startswith("postgres://"):
    _DB_URI = _DB_URI.replace("postgres://", "postgresql://", 1)

jobstores = {
    # Los jobs se persisten en la misma BD de la aplicacion, tabla apscheduler_jobs
    'default': SQLAlchemyJobStore(url=_DB_URI)
}

executors = {
    # Hasta 4 agentes proactivos corriendo en paralelo (un hilo por usuario)
    'default': ThreadPoolExecutor(max_workers=4)
}

job_defaults = {
    'coalesce': True,           # Si se acumulan ejecuciones perdidas, ejecutar solo una vez
    'max_instances': 1,         # Nunca dos instancias del mismo job al mismo tiempo
    'misfire_grace_time': 3600, # Tolerar hasta 1h de retraso antes de descartar el job
}

scheduler = BlockingScheduler(
    jobstores=jobstores,
    executors=executors,
    job_defaults=job_defaults,
)


def run_tasks_with_context():
    """Wrapper que ejecuta las tareas diarias dentro del contexto Flask."""
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Ejecutando tareas proactivas...")
    with app.app_context():
        run_daily_tasks()
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Tareas completadas.")


def on_job_executed(event):
    print(f"[APScheduler] Job '{event.job_id}' OK.")


def on_job_error(event):
    print(f"[APScheduler] ERROR en job '{event.job_id}': {event.exception}")


if __name__ == "__main__":
    scheduler.add_listener(on_job_executed, EVENT_JOB_EXECUTED)
    scheduler.add_listener(on_job_error, EVENT_JOB_ERROR)

    # replace_existing=True: si ya existe el job en la BD tras reinicio,
    # lo actualiza en lugar de crear un duplicado.
    scheduler.add_job(
        func=run_tasks_with_context,
        trigger='cron',
        hour=9,
        minute=0,
        id='daily_proactive_agents',
        name='Agentes Proactivos Diarios (09:00)',
        replace_existing=True,
    )

    print("=" * 60)
    print("  Zeptai Scheduler — APScheduler con persistencia SQL")
    print("=" * 60)
    print(f"  BD de jobs : {_DB_URI.split('///')[-1]}")
    print(f"  Ejecucion  : diaria a las 09:00")
    print(f"  Workers    : 4 hilos concurrentes")
    print("  Ctrl+C para detener.")
    print("=" * 60)

    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\nScheduler detenido.")
