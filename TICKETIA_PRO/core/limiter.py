"""
Rate limiter centralizado usando Flask-Limiter.

Se inicializa aqui como instancia global y se registra en app.py
con limiter.init_app(app). Los blueprints importan esta instancia
para aplicar @limiter.limit() en sus rutas.

Backend: memoria (valido para un solo proceso/worker).
En produccion con multiples workers usar:
    storage_uri="redis://localhost:6379"
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["300 per day", "60 per hour"],
    storage_uri="memory://",
)
