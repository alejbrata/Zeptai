import os
import sys
from datetime import timedelta

class Config:
    # La clave firma las cookies de sesion: quien la conozca puede fabricar una
    # sesion de cualquier usuario. En produccion no se admite un valor de reserva.
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        if os.environ.get('FLASK_ENV') == 'production':
            raise RuntimeError("SECRET_KEY es obligatoria con FLASK_ENV=production")
        SECRET_KEY = 'dev_key_super_secret_123'

    # Database Config
    uri = os.environ.get('DATABASE_URL')
    if uri and uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)

    basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    _instance_dir = os.path.join(basedir, 'instance')
    os.makedirs(_instance_dir, exist_ok=True)
    # Bajo pytest se ignora DATABASE_URL a proposito. Los tests hacen drop_all() en
    # tearDown y, si el engine queda enlazado a la BD real, borran su esquema entero.
    if 'pytest' in sys.modules:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    else:
        SQLALCHEMY_DATABASE_URI = uri or 'sqlite:///' + os.path.join(_instance_dir, 'zeptai.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Session security
    SESSION_PERMANENT = True
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = os.environ.get('FLASK_ENV') == 'production'
    
    # NUEVO: URL Pública para webhooks y medios (ngrok o dominio real)
    # Elimina la barra final si el usuario la pone
    PUBLIC_URL = os.environ.get('PUBLIC_URL', 'https://stepless-janel-bashfully.ngrok-free.dev').rstrip('/')

    # Mail Config
    MAIL_SERVER = os.environ.get('MAIL_SERVER') or 'smtp.gmail.com'
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS') == 'True'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER')
    
    # RunwayML Config
    RUNWAYML_API_SECRET = os.environ.get('RUNWAYML_API_SECRET')
