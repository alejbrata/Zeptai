"""
Tests para las herramientas MCP (core/mcp_tools.py).

Cubre las 6 tools con BD SQLite en memoria:
- tool_get_financial_summary   — tickets existentes, usuario sin tickets
- tool_get_appointments        — citas próximas, sin citas, fuera de rango
- tool_search_web              — resultados, sin resultados, error DuckDuckGo
- tool_schedule_appointment    — agendar OK, conflicto, formato fecha incorrecto
- tool_send_email_notification — envío OK, owner no existe, email inválido
- tool_get_business_stats      — agrega tickets + citas + LLMCall del mes/semana

Ejecución:
    cd TICKETIA_PRO
    pytest tests/test_mcp_tools.py -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

os.environ.setdefault('OPENAI_API_KEY', 'sk-fake-key-for-testing')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-very-long-and-random')
os.environ.setdefault('MAIL_DEFAULT_SENDER', 'test@zeptai.com')
os.environ.setdefault('RUNWAY_API_KEY', 'rw-fake-key-for-testing')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db
from core.db_models import BusinessProfile, Ticket, Appointment, LLMCall
from core.mcp_tools import (
    tool_get_financial_summary,
    tool_get_appointments,
    tool_search_web,
    tool_schedule_appointment,
    tool_send_email_notification,
    tool_get_business_stats,
)


# ─────────────────────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────────────────────

class MCPToolsBase(unittest.TestCase):
    PHONE = '+34600000010'
    EMAIL = 'mcp@test.com'

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        app.config['RATELIMIT_ENABLED'] = False

        with app.app_context():
            db.drop_all()
            db.create_all()
            from werkzeug.security import generate_password_hash
            user = BusinessProfile(
                user_phone=self.PHONE,
                email=self.EMAIL,
                password_hash=generate_password_hash('Test1234!'),
                business_name='MCP Test Biz',
                plan_tier='BASIC',
                features={},
            )
            db.session.add(user)
            db.session.commit()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def _add_ticket(self, concept, total, date=None):
        with app.app_context():
            t = Ticket(
                user_phone=self.PHONE,
                concept=concept,
                total=total,
                date=date or datetime.now(),
            )
            db.session.add(t)
            db.session.commit()

    def _add_appointment(self, date, time, client_name, client_phone=''):
        with app.app_context():
            a = Appointment(
                business_phone=self.PHONE,
                date=date,
                time=time,
                client_name=client_name,
                client_phone=client_phone,
            )
            db.session.add(a)
            db.session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 1. tool_get_financial_summary
# ─────────────────────────────────────────────────────────────────────────────

class TestGetFinancialSummary(MCPToolsBase):

    def test_returns_no_tickets_message_when_empty(self):
        result = tool_get_financial_summary(self.PHONE, app)
        self.assertIn('No se han encontrado', result)
        self.assertIn(self.PHONE, result)

    def test_returns_summary_with_tickets(self):
        self._add_ticket('Alquiler oficina', 800.0)
        self._add_ticket('Material', 45.50)

        result = tool_get_financial_summary(self.PHONE, app)

        self.assertIn('845.50', result)
        self.assertIn('2 tickets', result)
        self.assertIn('Alquiler oficina', result)
        self.assertIn('Material', result)

    def test_shows_at_most_5_tickets_in_breakdown(self):
        for i in range(8):
            self._add_ticket(f'Gasto {i}', 10.0)

        result = tool_get_financial_summary(self.PHONE, app)

        self.assertIn('8 tickets', result)
        self.assertIn('3 tickets más', result)

    def test_handles_ticket_with_none_total(self):
        """Tickets sin importe (total=None) no rompen el cálculo."""
        self._add_ticket('Sin importe', None)

        result = tool_get_financial_summary(self.PHONE, app)
        self.assertIn('1 tickets', result)
        self.assertIn('0.00', result)

    def test_unknown_user_returns_no_tickets_message(self):
        result = tool_get_financial_summary('+34999999999', app)
        self.assertIn('No se han encontrado', result)

    def test_returns_string_on_db_error(self):
        """Ante un error de BD devuelve string de error, no excepción."""
        with app.app_context():
            with patch('core.db_models.Ticket.query') as mock_q:
                mock_q.filter_by.side_effect = Exception('DB down')
                result = tool_get_financial_summary(self.PHONE, app)

        self.assertIsInstance(result, str)
        self.assertIn('Error', result)


# ─────────────────────────────────────────────────────────────────────────────
# 2. tool_get_appointments
# ─────────────────────────────────────────────────────────────────────────────

class TestGetAppointments(MCPToolsBase):

    def test_returns_no_appointments_when_empty(self):
        result = tool_get_appointments(self.PHONE, app)
        self.assertIn('No hay citas', result)

    def test_returns_upcoming_appointments(self):
        tomorrow = (datetime.now() + timedelta(days=1)).date()
        self._add_appointment(tomorrow, '10:00', 'Ana García', '+34600111222')

        result = tool_get_appointments(self.PHONE, app)

        self.assertIn('Ana García', result)
        self.assertIn('10:00', result)
        self.assertIn('+34600111222', result)

    def test_excludes_past_appointments(self):
        yesterday = (datetime.now() - timedelta(days=1)).date()
        self._add_appointment(yesterday, '09:00', 'Cliente Pasado')

        result = tool_get_appointments(self.PHONE, app)
        self.assertNotIn('Cliente Pasado', result)

    def test_excludes_appointments_beyond_days_ahead(self):
        far_future = (datetime.now() + timedelta(days=30)).date()
        self._add_appointment(far_future, '11:00', 'Cliente Lejano')

        result = tool_get_appointments(self.PHONE, app, days_ahead=7)
        self.assertNotIn('Cliente Lejano', result)

    def test_includes_appointment_exactly_at_boundary(self):
        """Cita exactamente en el día límite (hoy + days_ahead) debe incluirse."""
        boundary = (datetime.now() + timedelta(days=7)).date()
        self._add_appointment(boundary, '17:00', 'Cliente Límite')

        result = tool_get_appointments(self.PHONE, app, days_ahead=7)
        self.assertIn('Cliente Límite', result)

    def test_multiple_appointments_sorted_by_date_time(self):
        d1 = (datetime.now() + timedelta(days=2)).date()
        d2 = (datetime.now() + timedelta(days=1)).date()
        self._add_appointment(d1, '14:00', 'Cliente B')
        self._add_appointment(d2, '09:00', 'Cliente A')

        result = tool_get_appointments(self.PHONE, app)

        pos_a = result.index('Cliente A')
        pos_b = result.index('Cliente B')
        self.assertLess(pos_a, pos_b, "Cliente A (más pronto) debe aparecer primero")

    def test_custom_days_ahead_parameter(self):
        in_15_days = (datetime.now() + timedelta(days=15)).date()
        self._add_appointment(in_15_days, '10:00', 'Cliente Futuro')

        result_7 = tool_get_appointments(self.PHONE, app, days_ahead=7)
        result_30 = tool_get_appointments(self.PHONE, app, days_ahead=30)

        self.assertNotIn('Cliente Futuro', result_7)
        self.assertIn('Cliente Futuro', result_30)


# ─────────────────────────────────────────────────────────────────────────────
# 3. tool_search_web
# ─────────────────────────────────────────────────────────────────────────────

class TestSearchWeb(MCPToolsBase):

    def _mock_ddgs_results(self, results):
        """Helper: mockea DDGS.text() devolviendo results."""
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = lambda s: mock_ddgs
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = iter(results)
        return mock_ddgs

    def test_returns_formatted_results(self):
        fake_results = [
            {'title': 'Subvención PYME 2026', 'href': 'https://boe.es/123', 'body': 'Ayudas para pymes...'},
            {'title': 'Otro resultado', 'href': 'https://example.com', 'body': 'Más info...'},
        ]
        with patch('core.mcp_tools.DDGS', return_value=self._mock_ddgs_results(fake_results)):
            result = tool_search_web('subvenciones pymes 2026')

        self.assertIn('Subvención PYME 2026', result)
        self.assertIn('boe.es', result)
        self.assertIn('Ayudas para pymes', result)
        self.assertIn('1.', result)
        self.assertIn('2.', result)

    def test_returns_no_results_message_when_empty(self):
        with patch('core.mcp_tools.DDGS', return_value=self._mock_ddgs_results([])):
            result = tool_search_web('consulta sin resultados')

        self.assertIn('No se encontraron resultados', result)

    def test_respects_max_results_parameter(self):
        fake_results = [
            {'title': f'Resultado {i}', 'href': f'https://example.com/{i}', 'body': 'Texto'}
            for i in range(10)
        ]
        captured_call = {}

        def mock_text(query, max_results=5):
            captured_call['max_results'] = max_results
            return iter(fake_results[:max_results])

        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = lambda s: mock_ddgs
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.side_effect = mock_text

        with patch('core.mcp_tools.DDGS', return_value=mock_ddgs):
            tool_search_web('query', max_results=3)

        self.assertEqual(captured_call.get('max_results'), 3)

    def test_returns_error_string_on_exception(self):
        with patch('core.mcp_tools.DDGS', side_effect=Exception('Network error')):
            result = tool_search_web('test query')

        self.assertIsInstance(result, str)
        self.assertIn('Error', result)

    def test_handles_missing_fields_gracefully(self):
        """Resultados sin 'title' o 'body' no rompen el formato."""
        fake_results = [{'href': 'https://example.com'}]  # sin title ni body
        with patch('core.mcp_tools.DDGS', return_value=self._mock_ddgs_results(fake_results)):
            result = tool_search_web('test')

        self.assertIsInstance(result, str)
        self.assertIn('example.com', result)


# ─────────────────────────────────────────────────────────────────────────────
# 4. tool_schedule_appointment
# ─────────────────────────────────────────────────────────────────────────────

class TestScheduleAppointment(MCPToolsBase):

    def test_schedules_new_appointment_successfully(self):
        result = tool_schedule_appointment(
            self.PHONE, '2026-05-15', '10:00', 'María López', '+34600333444', app
        )

        self.assertIn('agendada', result.lower())
        self.assertIn('María López', result)
        self.assertIn('2026-05-15', result)
        self.assertIn('10:00', result)

    def test_appointment_persisted_in_db(self):
        tool_schedule_appointment(
            self.PHONE, '2026-05-20', '11:00', 'Pedro Sánchez', '', app
        )

        with app.app_context():
            from datetime import date
            appt = Appointment.query.filter_by(
                business_phone=self.PHONE,
                time='11:00',
            ).first()

        self.assertIsNotNone(appt)
        self.assertEqual(appt.client_name, 'Pedro Sánchez')
        self.assertEqual(appt.date, date(2026, 5, 20))

    def test_rejects_conflicting_appointment(self):
        tool_schedule_appointment(
            self.PHONE, '2026-06-01', '09:00', 'Cliente A', '', app
        )
        result = tool_schedule_appointment(
            self.PHONE, '2026-06-01', '09:00', 'Cliente B', '', app
        )

        self.assertIn('Ya existe', result)

        # Verificar que sólo hay UNA cita a esa hora
        with app.app_context():
            count = Appointment.query.filter_by(
                business_phone=self.PHONE,
                time='09:00',
            ).count()
        self.assertEqual(count, 1)

    def test_same_time_different_date_allowed(self):
        tool_schedule_appointment(
            self.PHONE, '2026-07-01', '10:00', 'Cliente A', '', app
        )
        result = tool_schedule_appointment(
            self.PHONE, '2026-07-02', '10:00', 'Cliente B', '', app
        )

        self.assertIn('agendada', result.lower())

    def test_rejects_invalid_date_format(self):
        result = tool_schedule_appointment(
            self.PHONE, '01/05/2026', '10:00', 'Cliente', '', app
        )

        self.assertIn('Formato de fecha incorrecto', result)
        self.assertIn('YYYY-MM-DD', result)

    def test_optional_client_phone(self):
        """client_phone vacío no impide agendar."""
        result = tool_schedule_appointment(
            self.PHONE, '2026-08-10', '16:00', 'Sin Teléfono', '', app
        )

        self.assertIn('agendada', result.lower())

    def test_different_owners_can_book_same_slot(self):
        """Dos negocios distintos pueden tener cita a la misma hora."""
        tool_schedule_appointment(
            self.PHONE, '2026-09-01', '10:00', 'Cliente A', '', app
        )
        result = tool_schedule_appointment(
            '+34699999999', '2026-09-01', '10:00', 'Cliente B', '', app
        )
        # El segundo negocio no existe en BD pero la cita se crea igual
        self.assertNotIn('Ya existe', result)


# ─────────────────────────────────────────────────────────────────────────────
# 5. tool_send_email_notification
# ─────────────────────────────────────────────────────────────────────────────

class TestSendEmailNotification(MCPToolsBase):

    def _mock_mail(self):
        m = MagicMock()
        m.send = MagicMock()
        return m

    def test_sends_email_to_valid_recipient(self):
        mock_mail = self._mock_mail()
        result = tool_send_email_notification(
            self.PHONE, 'cliente@example.com',
            'Presupuesto adjunto', 'Hola, le enviamos el presupuesto.',
            app, mock_mail
        )

        self.assertIn('Correo enviado', result)
        self.assertIn('cliente@example.com', result)
        mock_mail.send.assert_called_once()

    def test_rejects_unknown_owner(self):
        mock_mail = self._mock_mail()
        result = tool_send_email_notification(
            '+34000000000', 'dest@example.com',
            'Asunto', 'Cuerpo',
            app, mock_mail
        )

        self.assertIn('no existe', result.lower())
        mock_mail.send.assert_not_called()

    def test_rejects_invalid_email_no_at(self):
        mock_mail = self._mock_mail()
        result = tool_send_email_notification(
            self.PHONE, 'correo-sin-arroba',
            'Asunto', 'Cuerpo',
            app, mock_mail
        )

        self.assertIn('no parece un email válido', result)
        mock_mail.send.assert_not_called()

    def test_rejects_invalid_email_no_dot_in_domain(self):
        mock_mail = self._mock_mail()
        result = tool_send_email_notification(
            self.PHONE, 'usuario@dominio',
            'Asunto', 'Cuerpo',
            app, mock_mail
        )

        self.assertIn('no parece un email válido', result)
        mock_mail.send.assert_not_called()

    def test_email_body_includes_business_name(self):
        """El cuerpo del email enviado incluye el nombre del negocio."""
        mock_mail = self._mock_mail()
        tool_send_email_notification(
            self.PHONE, 'dest@example.com',
            'Test', 'Contenido',
            app, mock_mail
        )

        call_args = mock_mail.send.call_args
        sent_message = call_args[0][0]
        self.assertIn('MCP Test Biz', sent_message.body)

    def test_returns_error_string_on_mail_exception(self):
        mock_mail = self._mock_mail()
        mock_mail.send.side_effect = Exception('SMTP error')

        result = tool_send_email_notification(
            self.PHONE, 'dest@example.com',
            'Asunto', 'Cuerpo',
            app, mock_mail
        )

        self.assertIsInstance(result, str)
        self.assertIn('Error', result)

    def test_activity_log_recorded_on_success(self):
        """Se registra un ActivityLog al enviar el email."""
        from core.db_models import ActivityLog

        mock_mail = self._mock_mail()
        tool_send_email_notification(
            self.PHONE, 'dest@example.com',
            'Presupuesto', 'Texto',
            app, mock_mail
        )

        with app.app_context():
            log = ActivityLog.query.filter_by(
                user_phone=self.PHONE,
                agent_name='Council (MCP)'
            ).first()

        self.assertIsNotNone(log)
        self.assertIn('dest@example.com', log.action)


# ─────────────────────────────────────────────────────────────────────────────
# 6. tool_get_business_stats
# ─────────────────────────────────────────────────────────────────────────────

class TestGetBusinessStats(MCPToolsBase):

    def test_returns_string_with_stats_sections(self):
        result = tool_get_business_stats(self.PHONE, app)

        self.assertIn('Estadísticas', result)
        self.assertIn('Tickets', result)
        self.assertIn('Citas', result)
        self.assertIn('Llamadas IA', result)

    def test_counts_tickets_from_current_month(self):
        self._add_ticket('Gasto mes', 200.0)
        self._add_ticket('Otro gasto', 50.0)

        result = tool_get_business_stats(self.PHONE, app)

        self.assertIn('2', result)
        self.assertIn('250.00', result)

    def test_excludes_tickets_from_previous_month(self):
        """Tickets del mes anterior no cuentan en el resumen del mes."""
        prev_month = datetime.now().replace(day=1) - timedelta(days=1)
        self._add_ticket('Gasto viejo', 999.0, date=prev_month)

        result = tool_get_business_stats(self.PHONE, app)

        self.assertIn('0 tickets este mes', result.replace('Tickets este mes: 0', '0 tickets este mes')
                      if 'Tickets este mes: 0' in result else result)
        self.assertNotIn('999', result)

    def test_counts_upcoming_appointments(self):
        tomorrow = (datetime.now() + timedelta(days=1)).date()
        self._add_appointment(tomorrow, '10:00', 'Cliente 1')
        self._add_appointment(tomorrow, '12:00', 'Cliente 2')

        result = tool_get_business_stats(self.PHONE, app)

        self.assertIn('2', result)

    def test_counts_llm_calls_from_last_7_days(self):
        with app.app_context():
            db.session.add(LLMCall(
                user_phone=self.PHONE,
                model='gpt-4o',
                stage='chat_main',
                total_tokens=150,
                latency_ms=500,
                cost_usd=0.002,
                success=True,
                created_at=datetime.now(),
            ))
            db.session.add(LLMCall(
                user_phone=self.PHONE,
                model='gen3a_turbo',
                stage='runway',
                total_tokens=0,
                latency_ms=20000,
                cost_usd=0.25,
                success=True,
                created_at=datetime.now(),
            ))
            db.session.commit()

        result = tool_get_business_stats(self.PHONE, app)

        self.assertIn('2', result)
        self.assertIn('0.2520', result)  # 0.002 + 0.25 = 0.252

    def test_excludes_llm_calls_older_than_7_days(self):
        with app.app_context():
            db.session.add(LLMCall(
                user_phone=self.PHONE,
                model='gpt-4o',
                stage='old_call',
                total_tokens=100,
                latency_ms=400,
                cost_usd=99.99,
                success=True,
                created_at=datetime.now() - timedelta(days=10),
            ))
            db.session.commit()

        result = tool_get_business_stats(self.PHONE, app)
        self.assertNotIn('99.99', result)

    def test_returns_error_string_on_exception(self):
        with app.app_context():
            with patch('core.db_models.Ticket.query') as mock_q:
                mock_q.filter.side_effect = Exception('DB down')
                result = tool_get_business_stats(self.PHONE, app)

        self.assertIsInstance(result, str)
        self.assertIn('Error', result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
