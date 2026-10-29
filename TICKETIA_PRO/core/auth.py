"""
Utilidades de autorizacion compartidas por app.py y los blueprints.

Antes cada modulo comparaba el email de la sesion con su propia constante y
acabaron divergiendo: unas rutas leian ADMIN_EMAIL y otras tenian el email
fijo, de modo que el admin del entorno de demo veia /observabilidad pero no
las metricas globales. Una sola definicion evita esa clase de error.
"""
import os

from flask import session


def admin_email() -> str:
    return os.environ.get('ADMIN_EMAIL', 'admin@zeptai.com')


def is_admin() -> bool:
    return bool(session.get('user_email')) and session.get('user_email') == admin_email()
