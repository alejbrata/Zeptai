web: gunicorn --worker-class gevent --workers 1 --timeout 120 wsgi:app
worker: python TICKETIA_PRO/run_scheduler.py
release: python -c "import sys; sys.path.insert(0,'TICKETIA_PRO'); from app import app; from core.db_models import db; app.app_context().__enter__(); db.create_all()"
