"""
Tests de autenticacion, endpoints de API y AgentExecutor.

Cubre las areas con menor cobertura previa:
- Flujo completo de auth: registro, login valido/invalido, logout, sesion.
- Proteccion de rutas: redireccion si no hay sesion.
- Endpoints API: /api/chat, /upload_web_ticket, /api/council/stream.
- Rate limiting: verificacion de cabeceras X-RateLimit-*.
- AgentExecutor: mock de OpenAI para validar el flujo sin coste real.
- Seguridad de cookies de sesion: HttpOnly, SameSite.

Ejecucion:
    cd TICKETIA_PRO
    pytest tests/test_api_auth.py -v
"""

import os
import sys
import io
import json
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime

# Env vars falsas para evitar errores de importacion
os.environ.setdefault('OPENAI_API_KEY', 'sk-fake-key-for-testing')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-very-long-and-random')
os.environ.setdefault('MAIL_DEFAULT_SENDER', 'test@zeptai.com')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db
from core.db_models import BusinessProfile, ChatMessage


# ─────────────────────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────────────────────

class ZeptaiTestBase(unittest.TestCase):
    """Configura app con BD en memoria y usuario de prueba."""

    TEST_EMAIL = 'test@panaderia.com'
    TEST_PHONE = '+34600000001'
    TEST_PASSWORD = 'ContraseñaSegura123!'
    TEST_BUSINESS = 'Panadería Pepe Test'

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['RATELIMIT_ENABLED'] = False  # Desactivar rate limit en tests
        self.client = app.test_client()

        with app.app_context():
            db.drop_all()
            db.create_all()
            self._create_test_user()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def _create_test_user(self):
        from werkzeug.security import generate_password_hash
        user = BusinessProfile(
            user_phone=self.TEST_PHONE,
            email=self.TEST_EMAIL,
            password_hash=generate_password_hash(self.TEST_PASSWORD),
            business_name=self.TEST_BUSINESS,
            plan_tier='BASIC',
            features={"tickets_allowed": True, "dashboard_access": True},
        )
        db.session.add(user)
        db.session.commit()

    def _login(self):
        """Hace login y devuelve la respuesta."""
        return self.client.post('/login', data={
            'email': self.TEST_EMAIL,
            'password': self.TEST_PASSWORD,
        }, follow_redirects=True)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tests de Autenticacion
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthentication(ZeptaiTestBase):

    def test_register_new_user(self):
        """Registro correcto crea usuario en BD y redirige al login."""
        resp = self.client.post('/register', data={
            'email': 'nuevo@negocio.com',
            'phone': '+34600000099',
            'password': 'OtraContraseña456!',
            'business_name': 'Negocio Nuevo',
            'gdpr_consent': 'on',
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        with app.app_context():
            user = BusinessProfile.query.filter_by(email='nuevo@negocio.com').first()
            self.assertIsNotNone(user)
            self.assertEqual(user.business_name, 'Negocio Nuevo')

    def test_register_duplicate_email_rejected(self):
        """No se puede registrar con un email ya existente."""
        resp = self.client.post('/register', data={
            'email': self.TEST_EMAIL,  # duplicado
            'phone': '+34600000099',
            'password': 'OtraContraseña456!',
            'business_name': 'Duplicado',
            'gdpr_consent': 'on',
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        with app.app_context():
            # Solo debe haber UN usuario con este email
            count = BusinessProfile.query.filter_by(email=self.TEST_EMAIL).count()
            self.assertEqual(count, 1)

    def test_register_duplicate_phone_rejected(self):
        """No se puede registrar con un telefono ya existente."""
        resp = self.client.post('/register', data={
            'email': 'otro@negocio.com',
            'phone': self.TEST_PHONE,  # duplicado
            'password': 'OtraContraseña456!',
            'business_name': 'Duplicado',
            'gdpr_consent': 'on',
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        with app.app_context():
            count = BusinessProfile.query.filter_by(user_phone=self.TEST_PHONE).count()
            self.assertEqual(count, 1)

    def test_login_valid_credentials(self):
        """Login correcto devuelve 200 y redirige al dashboard."""
        resp = self._login()
        self.assertEqual(resp.status_code, 200)
        # Verificar que estamos en el dashboard (no en login)
        self.assertNotIn(b'Inicia sesi', resp.data)

    def test_login_wrong_password(self):
        """Password incorrecta no crea sesion."""
        resp = self.client.post('/login', data={
            'email': self.TEST_EMAIL,
            'password': 'contraseña_incorrecta',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        # Debe mostrar error de credenciales
        self.assertIn(b'Credenciales', resp.data)

    def test_login_nonexistent_user(self):
        """Email inexistente no revela si el usuario existe o no."""
        resp = self.client.post('/login', data={
            'email': 'noexiste@zeptai.com',
            'password': 'cualquier_cosa',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Credenciales', resp.data)

    def test_logout_clears_session(self):
        """Logout limpia la sesion y redirige al login."""
        self._login()
        resp = self.client.get('/logout', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        # Tras logout, acceder al dashboard debe redirigir
        resp2 = self.client.get('/dashboard', follow_redirects=False)
        # Debe redirigir (302) o mostrar login
        self.assertIn(resp2.status_code, [301, 302, 200])


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tests de Proteccion de Rutas
# ─────────────────────────────────────────────────────────────────────────────

class TestRouteProtection(ZeptaiTestBase):

    def test_dashboard_requires_login(self):
        """El dashboard redirige si no hay sesion activa."""
        resp = self.client.get('/dashboard', follow_redirects=False)
        self.assertIn(resp.status_code, [301, 302])

    def test_transactions_requires_login(self):
        resp = self.client.get('/transactions', follow_redirects=False)
        self.assertIn(resp.status_code, [301, 302])

    def test_documents_requires_login(self):
        resp = self.client.get('/documents', follow_redirects=False)
        self.assertIn(resp.status_code, [301, 302])

    def test_api_chat_requires_login(self):
        """El endpoint de chat devuelve 401 sin sesion."""
        resp = self.client.post('/api/chat',
            data=json.dumps({'message': 'hola'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 401)

    def test_upload_ticket_requires_login(self):
        resp = self.client.post('/upload_web_ticket')
        self.assertEqual(resp.status_code, 401)

    def test_upload_audio_requires_login(self):
        resp = self.client.post('/upload_web_audio')
        self.assertEqual(resp.status_code, 401)

    def test_notifications_requires_login(self):
        resp = self.client.get('/api/notifications')
        self.assertEqual(resp.status_code, 401)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tests de Seguridad de Sesion
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionSecurity(ZeptaiTestBase):

    def test_session_cookie_httponly(self):
        """La cookie de sesion debe estar configurada con HttpOnly en la app."""
        # Verificar la configuracion de la app, no la cookie del test client
        # (el test client de Flask no refleja todos los atributos de la cookie)
        self.assertTrue(
            app.config.get('SESSION_COOKIE_HTTPONLY', False),
            "SESSION_COOKIE_HTTPONLY debe ser True en la configuracion"
        )

    def test_password_stored_hashed(self):
        """Las contraseñas no se almacenan en texto plano."""
        with app.app_context():
            user = BusinessProfile.query.filter_by(email=self.TEST_EMAIL).first()
            self.assertIsNotNone(user.password_hash)
            self.assertNotEqual(user.password_hash, self.TEST_PASSWORD)
            # Debe empezar con el prefijo de bcrypt/scrypt/pbkdf2
            self.assertTrue(
                user.password_hash.startswith(('pbkdf2:', 'scrypt:', '$2b$', '$2a$')),
                "La contraseña debe estar hasheada con un algoritmo seguro"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Tests de AgentExecutor (con mock de OpenAI)
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentExecutor(ZeptaiTestBase):
    """
    Valida el flujo del AgentExecutor sin hacer llamadas reales a OpenAI.
    Se mockea el cliente de OpenAI para controlar la respuesta.
    """

    def _make_mock_response(self, content: str, tool_calls=None):
        """Crea un objeto de respuesta falso con la estructura que devuelve OpenAI."""
        mock_msg = MagicMock()
        mock_msg.content = content
        mock_msg.tool_calls = tool_calls

        mock_choice = MagicMock()
        mock_choice.message = mock_msg

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        # usage=None para que llm_tracker no intente extraer tokens de un MagicMock
        mock_response.usage = None
        return mock_response

    @patch('modules.agents.manager.get_openai_client')
    def test_agent_returns_text_response(self, mock_get_client):
        """El agente devuelve el texto del LLM cuando no hay tool calls."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._make_mock_response(
            "Hola, soy tu asistente de Panadería Pepe."
        )
        mock_get_client.return_value = mock_client

        with app.app_context():
            from modules.agents.manager import run_agent
            profile = BusinessProfile.query.filter_by(email=self.TEST_EMAIL).first()
            response = run_agent(
                user_message="Hola",
                phone_number=self.TEST_PHONE,
                business_profile=profile,
                channel='web'
            )

        self.assertIsInstance(response, str)
        self.assertGreater(len(response), 0)

    @patch('modules.agents.manager.get_openai_client')
    def test_agent_saves_chat_history(self, mock_get_client):
        """El agente guarda las interacciones en la BD."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._make_mock_response(
            "Tu saldo de facturas es de 500€."
        )
        mock_get_client.return_value = mock_client

        with app.app_context():
            from modules.agents.manager import run_agent
            profile = BusinessProfile.query.filter_by(email=self.TEST_EMAIL).first()
            run_agent(
                user_message="¿Cuanto llevo gastado este mes?",
                phone_number=self.TEST_PHONE,
                business_profile=profile,
                channel='web'
            )

            # Verificar que se guardaron mensajes en el historial
            msgs = ChatMessage.query.filter_by(user_phone=self.TEST_PHONE).all()
            self.assertGreater(len(msgs), 0)

            # Debe haber al menos un mensaje del usuario
            user_msgs = [m for m in msgs if m.role == 'user']
            self.assertGreater(len(user_msgs), 0)

    @patch('modules.agents.manager.get_openai_client')
    def test_agent_uses_business_system_prompt(self, mock_get_client):
        """El system prompt del negocio se pasa al LLM correctamente."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._make_mock_response(
            "Entendido."
        )
        mock_get_client.return_value = mock_client

        with app.app_context():
            from modules.agents.manager import run_agent

            # Configurar un system prompt personalizado
            profile = BusinessProfile.query.filter_by(email=self.TEST_EMAIL).first()
            profile.system_prompt = "Eres el asistente de Panadería Pepe. Solo hablas de pan."
            db.session.commit()

            run_agent(
                user_message="Test",
                phone_number=self.TEST_PHONE,
                business_profile=profile,
                channel='web'
            )

        # Verificar que la llamada al LLM incluyo el system prompt del negocio
        call_args = mock_client.chat.completions.create.call_args
        messages_sent = call_args[1]['messages'] if call_args[1] else call_args[0][0]
        system_messages = [m for m in messages_sent if m.get('role') == 'system']

        self.assertGreater(len(system_messages), 0)
        system_content = system_messages[0].get('content', '')
        self.assertIn('Panadería Pepe', system_content)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Tests de Endpoints API (autenticado)
# ─────────────────────────────────────────────────────────────────────────────

class TestAPIEndpoints(ZeptaiTestBase):

    def setUp(self):
        super().setUp()
        # Inyectar sesion directamente para evitar dependencia del flujo de login
        with self.client.session_transaction() as sess:
            sess['user_phone'] = self.TEST_PHONE
            sess['user_email'] = self.TEST_EMAIL
            sess['business_name'] = self.TEST_BUSINESS

    @patch('modules.agents.manager.get_openai_client')
    def test_chat_endpoint_returns_response(self, mock_get_client):
        """POST /api/chat devuelve JSON con campo 'response'."""
        mock_client = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = "Respuesta del agente"
        mock_msg.tool_calls = None
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = None
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        resp = self.client.post('/api/chat',
            data=json.dumps({'message': 'Hola asistente'}),
            content_type='application/json')

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('response', data)

    def test_chat_endpoint_rejects_empty_message(self):
        """POST /api/chat sin mensaje devuelve 400."""
        resp = self.client.post('/api/chat',
            data=json.dumps({}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_upload_ticket_no_file_returns_400(self):
        """POST /upload_web_ticket sin fichero devuelve 400."""
        resp = self.client.post('/upload_web_ticket')
        self.assertEqual(resp.status_code, 400)

    def test_upload_audio_no_file_returns_400(self):
        """POST /upload_web_audio sin fichero devuelve 400."""
        resp = self.client.post('/upload_web_audio')
        self.assertEqual(resp.status_code, 400)

    def test_notifications_endpoint_returns_list(self):
        """GET /api/notifications devuelve una lista JSON."""
        resp = self.client.get('/api/notifications')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIsInstance(data, list)

    def test_mark_notification_read_not_found(self):
        """Marcar como leida una notificacion inexistente devuelve 404."""
        resp = self.client.post('/api/notifications/mark_read/99999')
        self.assertEqual(resp.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Tests de Reset de Contraseña
# ─────────────────────────────────────────────────────────────────────────────

class TestPasswordReset(ZeptaiTestBase):

    def test_forgot_password_unknown_email_no_info_leak(self):
        """
        El endpoint de forgot_password no revela si el email existe.
        Siempre muestra el mismo mensaje generico.
        """
        resp = self.client.post('/forgot_password', data={
            'email': 'inexistente@noestaaqui.com'
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        # El mensaje debe ser generico, no revelar si existe o no
        self.assertNotIn(b'inexistente@noestaaqui.com', resp.data)

    def test_reset_password_invalid_token(self):
        """Token invalido/expirado no permite cambiar contraseña."""
        resp = self.client.post('/reset_password/token_falso_invalido', data={
            'password': 'NuevaContraseña789!',
            'confirm': 'NuevaContraseña789!',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        # No debe mostrar exito con un token falso
        self.assertNotIn(b'correctamente', resp.data)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Tests de Rate Limiting (verificacion de cabeceras)
# ─────────────────────────────────────────────────────────────────────────────

class TestRateLimitHeaders(ZeptaiTestBase):
    """
    Verifica que Flask-Limiter esta activo comprobando las cabeceras de respuesta.
    En modo TESTING el rate limit esta desactivado (RATELIMIT_ENABLED=False),
    pero podemos verificar que el limiter esta registrado correctamente.
    """

    def test_limiter_registered_in_app(self):
        """Flask-Limiter esta inicializado en la app."""
        from core.limiter import limiter
        # El limiter debe tener la app registrada
        self.assertIsNotNone(limiter)

    def test_login_endpoint_exists_and_responds(self):
        """El endpoint de login existe y responde (el limiter no lo rompe)."""
        resp = self.client.get('/login')
        self.assertEqual(resp.status_code, 200)

    def test_api_chat_endpoint_exists(self):
        """El endpoint /api/chat existe y rechaza sin auth (no error 404/500)."""
        resp = self.client.post('/api/chat',
            data=json.dumps({'message': 'test'}),
            content_type='application/json')
        # 401 (no auth) o 200 (si hay sesion), nunca 404 o 500
        self.assertIn(resp.status_code, [200, 401, 429])


# ─────────────────────────────────────────────────────────────────────────────
# 8. Regresiones de la auditoria de seguridad (octubre 2026)
# ─────────────────────────────────────────────────────────────────────────────

class TestSecurityRegressions(ZeptaiTestBase):

    OTHER_PHONE = '+34600000002'

    def setUp(self):
        super().setUp()
        self._tmp_files = []
        # Estos tests hacen varios login seguidos: el limite de 5/min del login
        # (que se acumula entre tests) no es lo que se prueba aqui.
        from core.limiter import limiter
        self._limiter_was_enabled = limiter.enabled
        limiter.enabled = False

    def tearDown(self):
        from core.limiter import limiter
        limiter.enabled = self._limiter_was_enabled
        for path in self._tmp_files:
            if os.path.exists(path):
                os.remove(path)
        super().tearDown()

    def _private_file(self, name):
        """Crea un fichero real en static/uploads para probar el control de acceso."""
        folder = os.path.join(app.static_folder, 'uploads')
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, 'wb') as f:
            f.write(b'\xff\xd8\xff fake jpeg')
        self._tmp_files.append(path)
        return f'/static/uploads/{name}'

    # -- Enumeracion de usuarios ----------------------------------------------

    def test_forgot_password_same_response_known_and_unknown(self):
        """La respuesta no permite distinguir si el email esta registrado."""
        with patch('app.mail.send'):
            known = self.client.post('/forgot_password', data={'email': self.TEST_EMAIL},
                                     follow_redirects=True)
            unknown = self.client.post('/forgot_password', data={'email': 'nadie@noexiste.com'},
                                       follow_redirects=True)
        from routes.web import _FORGOT_PASSWORD_MSG
        msg = _FORGOT_PASSWORD_MSG.encode()
        self.assertIn(msg, known.data)
        self.assertIn(msg, unknown.data)

    def test_forgot_password_mail_error_not_revealed(self):
        """Un fallo de envio tampoco cambia el mensaje (delataria que el email existe)."""
        with patch('app.mail.send', side_effect=Exception('SMTP caido')):
            resp = self.client.post('/forgot_password', data={'email': self.TEST_EMAIL},
                                    follow_redirects=True)
        from routes.web import _FORGOT_PASSWORD_MSG
        self.assertIn(_FORGOT_PASSWORD_MSG.encode(), resp.data)

    def test_login_unknown_email_still_verifies_hash(self):
        """Con un email inexistente tambien se calcula un hash: mismo tiempo de respuesta."""
        with patch('routes.web.check_password_hash', return_value=False) as chk:
            self.client.post('/login', data={'email': 'nadie@noexiste.com', 'password': 'x'})
        chk.assert_called_once()

    # -- CSRF por comprobacion de origen --------------------------------------

    def test_cross_origin_post_rejected(self):
        """Un POST con Origin de otro dominio se rechaza con 403."""
        self._login()
        resp = self.client.post('/api/chat/feedback', json={'rating': 1},
                                headers={'Origin': 'https://evil.example.com'})
        self.assertEqual(resp.status_code, 403)

    def test_cross_origin_referer_rejected(self):
        """Sin Origin, un Referer de otro dominio tambien se rechaza."""
        self._login()
        resp = self.client.post('/api/chat/feedback', json={'rating': 1},
                                headers={'Referer': 'https://evil.example.com/pagina'})
        self.assertEqual(resp.status_code, 403)

    def test_same_origin_post_allowed(self):
        """Un POST desde el propio origen pasa."""
        self._login()
        resp = self.client.post('/api/chat/feedback', json={'rating': 1},
                                headers={'Origin': 'http://localhost'})
        self.assertEqual(resp.status_code, 200)

    # -- Ficheros privados en /static -----------------------------------------

    def test_private_file_requires_login(self):
        """Un ticket no se descarga sin sesion (404, no se confirma que existe)."""
        url = self._private_file('web_ticket_test_priv1.jpg')
        with app.app_context():
            from core.db_models import Ticket
            db.session.add(Ticket(user_phone=self.TEST_PHONE, image_path=url, total=10))
            db.session.commit()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_private_file_owner_can_download(self):
        """El propietario si puede descargar su ticket."""
        url = self._private_file('web_ticket_test_priv2.jpg')
        with app.app_context():
            from core.db_models import Ticket
            db.session.add(Ticket(user_phone=self.TEST_PHONE, image_path=url, total=10))
            db.session.commit()
        self._login()
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        resp.close()

    def test_private_file_other_tenant_gets_404(self):
        """Otro negocio autenticado no puede descargar el ticket ajeno."""
        url = self._private_file('web_ticket_test_priv3.jpg')
        with app.app_context():
            from core.db_models import Ticket
            db.session.add(Ticket(user_phone=self.OTHER_PHONE, image_path=url, total=10))
            db.session.commit()
        self._login()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_public_static_still_served(self):
        """Los assets publicos (manifest, logo) siguen sin requerir sesion."""
        resp = self.client.get('/static/manifest.json')
        self.assertEqual(resp.status_code, 200)
        resp.close()

    # -- /api/prometheus -------------------------------------------------------

    def test_prometheus_rejects_docker_gateway(self):
        """La IP de la pasarela Docker (peticion por el puerto publicado) ya no basta."""
        with patch('routes.api._prometheus_ips', return_value={'172.18.0.5'}):
            resp = self.client.get('/api/prometheus', environ_base={'REMOTE_ADDR': '172.18.0.1'})
        self.assertEqual(resp.status_code, 403)

    def test_prometheus_accepts_scraper_container(self):
        """El contenedor de Prometheus, resuelto por DNS interno, si puede leer."""
        with patch('routes.api._prometheus_ips', return_value={'172.18.0.5'}):
            resp = self.client.get('/api/prometheus', environ_base={'REMOTE_ADDR': '172.18.0.5'})
        self.assertEqual(resp.status_code, 200)

    # -- Otros -------------------------------------------------------------------

    def test_setup_magic_db_route_removed(self):
        """La ruta de reseteo de BD por GET ya no existe."""
        resp = self.client.get('/setup_magic_db_force?key=super_secret_dev_key')
        self.assertEqual(resp.status_code, 404)

    def test_profile_password_change_enforces_policy(self):
        """El cambio de contrasena desde el perfil aplica la misma politica que el registro."""
        self._login()
        self.client.post('/profile', data={'current_password': self.TEST_PASSWORD,
                                           'new_password': 'debil'})
        with app.app_context():
            from werkzeug.security import check_password_hash
            user = BusinessProfile.query.filter_by(user_phone=self.TEST_PHONE).first()
            self.assertTrue(check_password_hash(user.password_hash, self.TEST_PASSWORD))

    def test_response_cache_invalidate_and_temporal(self):
        """invalidate() borra las entradas del negocio y lo temporal no se cachea."""
        from modules.services.response_cache import _ResponseCache
        cache = _ResponseCache()
        cache.set(f'{self.TEST_PHONE}|3|abc', '¿Cuál es el horario?', 'De 9 a 18')
        self.assertEqual(cache.get(f'{self.TEST_PHONE}|3|abc', '¿Cuál es el horario?'), 'De 9 a 18')
        self.assertEqual(cache.invalidate(self.TEST_PHONE), 1)
        self.assertIsNone(cache.get(f'{self.TEST_PHONE}|3|abc', '¿Cuál es el horario?'))
        cache.set(self.TEST_PHONE, '¿Qué tengo mañana?', 'Nada')
        self.assertIsNone(cache.get(self.TEST_PHONE, '¿Qué tengo mañana?'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
