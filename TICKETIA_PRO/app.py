import os
from dotenv import load_dotenv

# Cargar variables de entorno desde el directorio padre
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

from urllib.parse import urlparse
from flask import Flask, render_template, redirect, url_for, flash, session, request, abort
from flask_mail import Mail
from core.config import Config
from core.auth import is_admin
from core.limiter import limiter
from core.db_models import db, BusinessProfile, Ticket, ChatMessage, Grant, Appointment, SynergyMatch, LLMCall
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from core.logging_config import setup_logging
from core.telemetry import init_tracing, init_sqlalchemy_tracing

setup_logging()

app = Flask(__name__)
app.config.from_object(Config)

# Inicializar Base de Datos
db.init_app(app)
mail = Mail(app)

# Rate limiting
limiter.init_app(app)

# OpenTelemetry tracing (no-op si OTEL_EXPORTER_OTLP_ENDPOINT no está definido)
init_tracing(app)

# --- GLOBAL CONTEXT PROCESSOR (NOTIFICACIONES) ---
@app.context_processor
def inject_notifications():
    from core.db_models import Notification
    # is_admin decide que entradas del menu se pintan; la definicion vive en core/auth.py.
    admin = is_admin()
    if 'user_phone' in session:
        count = Notification.query.filter_by(
            user_phone=session['user_phone'],
            is_read=False
        ).count()
        return dict(unread_notifications_count=count, is_admin=admin)
    return dict(unread_notifications_count=0, is_admin=admin)


# --- CSRF: comprobacion de origen ---
# No hay tokens CSRF, asi que toda peticion que modifica estado debe venir de
# este mismo origen. Los navegadores envian Origin (o al menos Referer) en los
# POST, y una pagina de otro dominio no puede falsificarlos. Si no llega
# ninguna de las dos cabeceras no es un navegador (tests, curl, Prometheus) y
# no hay cookie que robar, asi que se deja pasar.
@app.before_request
def check_same_origin():
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    source = request.headers.get('Origin') or request.headers.get('Referer')
    if source and urlparse(source).netloc != request.host:
        abort(403)
    return None


# --- Ficheros privados ---
# Tickets, logos, documentos de conocimiento y documentos generados se guardan
# bajo /static, que Flask sirve sin autenticacion, y sus nombres derivan de un
# timestamp. Sin esta comprobacion cualquiera podia descargar los de otro negocio.
_PRIVATE_STATIC_PREFIXES = ('/static/uploads/', '/static/generated_docs/')


def _owns_static_file(phone: str, path: str) -> bool:
    from core.db_models import GeneratedDocument, Ticket, BusinessProfile

    parts = path.split('/')
    # /static/uploads/knowledge/<telefono>/<fichero>
    if path.startswith('/static/uploads/knowledge/'):
        return len(parts) > 4 and parts[4] == phone

    filename = parts[-1]
    if not filename:
        return False
    suffix = '/' + filename
    return bool(
        GeneratedDocument.query.filter(
            GeneratedDocument.user_phone == phone,
            GeneratedDocument.file_path.endswith(suffix, autoescape=True),
        ).first()
        or Ticket.query.filter(
            Ticket.user_phone == phone,
            Ticket.image_path.endswith(suffix, autoescape=True),
        ).first()
        or BusinessProfile.query.filter(
            BusinessProfile.user_phone == phone,
            BusinessProfile.logo_path.endswith(suffix, autoescape=True),
        ).first()
    )


@app.before_request
def protect_private_files():
    if not request.path.startswith(_PRIVATE_STATIC_PREFIXES):
        return None
    phone = session.get('user_phone')
    # 404 y no 403: no confirmar a un tercero que el fichero existe
    if not phone or not _owns_static_file(phone, request.path):
        abort(404)
    return None

# --- ADMIN PANEL CONFIGURATION ---
class SecureModelView(ModelView):
    """Clase base para proteger el panel de administración."""
    def is_accessible(self):
        # Solo el admin (ADMIN_EMAIL, ver core/auth.py)
        return is_admin()

    def inaccessible_callback(self, name, **kwargs):
        # Si no es admin, redirige al login
        flash('⚠️ Acceso restringido a administradores.', 'error')
        return redirect(url_for('web.login'))

# Inicializar el Panel
# Nota: template_mode='bootstrap4' puede requerir temas compatibles, si falla lo quitamos o usamos default
try:
    admin = Admin(app, name='Panel de Control Zeptai', template_mode='bootstrap4')
except TypeError:
    # Fallback si la versión instalada no soporta template_mode en init
    admin = Admin(app, name='Panel de Control Zeptai')

from flask_admin.menu import MenuLink
admin.add_link(MenuLink(name='🏠 Volver a Web', category='', url='/dashboard'))

# Añadir Vistas (Tablas visuales)
# 1. Gestión de Ayudas (Lo más importante para el Grant Hunter)
admin.add_view(SecureModelView(Grant, db.session, name='💰 Subvenciones'))

# 2. Gestión de Usuarios y Negocios
admin.add_view(SecureModelView(BusinessProfile, db.session, name='👥 Usuarios'))

# 3. Auditoría de Tickets
admin.add_view(SecureModelView(Ticket, db.session, name='🧾 Tickets'))

# 4. Logs del Chat y Citas
admin.add_view(SecureModelView(ChatMessage, db.session, name='💬 Chats'))
admin.add_view(SecureModelView(SynergyMatch, db.session, name='🤝 Matches'))
admin.add_view(SecureModelView(LLMCall, db.session, name='🧠 Métricas LLM'))
# admin.add_view(SecureModelView(Appointment, db.session, name='📅 Citas'))

with app.app_context():
    from sqlalchemy import text
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')

    if 'postgresql' in uri:
        with db.engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()

    db.create_all()
    init_sqlalchemy_tracing()

    # Migración automática: añadir end_time si no existe (sin perder datos)
    try:
        with db.engine.connect() as conn:
            if 'postgresql' in uri:
                conn.execute(text(
                    "ALTER TABLE appointment ADD COLUMN IF NOT EXISTS end_time VARCHAR(10)"
                ))
            else:
                cols = [r[1] for r in conn.execute(text("PRAGMA table_info(appointment)"))]
                if 'end_time' not in cols:
                    conn.execute(text(
                        "ALTER TABLE appointment ADD COLUMN end_time VARCHAR(10)"
                    ))
            conn.commit()
    except Exception:
        pass


# --- BLUEPRINTS REGISTRATION ---
from routes.web import web_bp
from routes.api import api_bp
from routes.knowledge import knowledge_bp

app.register_blueprint(web_bp)
app.register_blueprint(api_bp)
app.register_blueprint(knowledge_bp)

# ── Security headers ──────────────────────────────────────────────────────────
@app.after_request
def set_security_headers(response):
    response.headers['X-Frame-Options']        = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection']       = '1; mode=block'
    response.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy']     = 'geolocation=(), microphone=(self), camera=()'
    if os.environ.get('FLASK_ENV') == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    # CSP: permite Tailwind CDN, Google Fonts y embeds de observabilidad (Grafana/Jaeger)
    grafana_url    = os.environ.get('GRAFANA_URL',    'http://localhost:3000')
    jaeger_url     = os.environ.get('JAEGER_URL',     'http://localhost:16686')
    prometheus_url = os.environ.get('PROMETHEUS_URL', 'http://localhost:9090')
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com unpkg.com cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https://cdn.tailwindcss.com https://fonts.googleapis.com; "
        "media-src 'self' blob:; "
        f"frame-src {grafana_url} {jaeger_url} {prometheus_url}; "
        "frame-ancestors 'self';"
    )
    return response

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    return render_template('500.html'), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    app.run(host='0.0.0.0', port=port, debug=debug)
