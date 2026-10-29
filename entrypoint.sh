#!/bin/sh
# Ejecuta db.create_all() una sola vez antes de iniciar gunicorn
# Evita la race condition cuando múltiples workers arrancan a la vez

set -e

echo "==> Iniciando migracion de base de datos..."
python -c "
import sys
sys.path.insert(0, '/app/TICKETIA_PRO')
from app import app, db
with app.app_context():
    db.create_all()
    print('==> Tablas listas.')
"

echo "==> Arrancando gunicorn..."
exec gunicorn -w 2 -b 0.0.0.0:5000 --timeout 300 app:app
