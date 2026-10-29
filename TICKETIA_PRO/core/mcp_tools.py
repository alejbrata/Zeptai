"""
Herramientas MCP compartidas entre mcp_server.py (stdio) y mcp_server_sse.py (Streamable HTTP).

Cada función es un tool MCP decorado con @mcp.tool() en los servidores que las importan.
Aquí se definen como funciones puras que reciben `app` y `mail` como contexto,
para evitar imports circulares y facilitar los tests.

Tools disponibles:
    get_financial_summary       — Resumen de gastos del usuario (tickets DB)
    get_appointments            — Lista de citas próximas del negocio
    search_web                  — Búsqueda DuckDuckGo en tiempo real
    schedule_appointment        — Agenda una cita (con detección de conflictos)
    send_email_notification     — Envía email en nombre del negocio
    get_business_stats          — Métricas clave del negocio (tickets + citas + LLM)
"""

import logging
import re
from datetime import datetime, timedelta, timezone, date as date_type

_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')

try:
    from duckduckgo_search import DDGS
    _DDGS_AVAILABLE = True
except ImportError:
    DDGS = None
    _DDGS_AVAILABLE = False

logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> date_type:
    """Convierte 'YYYY-MM-DD' a datetime.date. Lanza ValueError si el formato es incorrecto."""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


# ─── Tool implementations ─────────────────────────────────────────────────────

def tool_get_financial_summary(user_phone: str, app) -> str:
    """
    Devuelve un resumen de los gastos del usuario basado en sus tickets registrados.

    Args:
        user_phone: Número de teléfono exacto del usuario en la base de datos.
        app: Instancia Flask para el contexto de aplicación.
    """
    from core.db_models import Ticket

    try:
        with app.app_context():
            tickets = Ticket.query.filter_by(user_phone=user_phone).all()

            if not tickets:
                return f"No se han encontrado tickets para el usuario {user_phone}."

            total = 0.0
            lines = []
            for t in tickets:
                amount = t.total if t.total else 0.0
                total += amount
                date_str = t.date.strftime('%Y-%m-%d') if t.date else "Fecha desconocida"
                concept = t.concept if t.concept else "Gasto sin concepto"
                lines.append(f"- {date_str}: {concept} ({amount:.2f}€)")

            summary = f"Resumen Financiero para {user_phone}\n"
            summary += f"Total gastado: {total:.2f}€ en {len(tickets)} tickets.\n\n"
            summary += "Desglose reciente:\n"
            summary += "\n".join(lines[:5])
            if len(tickets) > 5:
                summary += f"\n... y {len(tickets) - 5} tickets más."
            return summary

    except Exception as e:
        logger.error("tool_get_financial_summary error: %s", e)
        return f"Error al consultar la base de datos: {e}"


def tool_get_appointments(owner_phone: str, app, days_ahead: int = 7) -> str:
    """
    Lista las próximas citas del negocio en los próximos N días.

    Args:
        owner_phone: Teléfono del propietario del negocio.
        app: Instancia Flask para el contexto de aplicación.
        days_ahead: Número de días hacia adelante a consultar (por defecto 7).
    """
    from core.db_models import Appointment

    try:
        with app.app_context():
            today = datetime.now().date()
            end_date = today + timedelta(days=days_ahead)

            appointments = Appointment.query.filter(
                Appointment.business_phone == owner_phone,
                Appointment.date >= today,
                Appointment.date <= end_date
            ).order_by(Appointment.date, Appointment.time).all()

            if not appointments:
                return f"No hay citas programadas en los próximos {days_ahead} días."

            lines = []
            for a in appointments:
                client = a.client_name or "Cliente desconocido"
                phone_info = f" ({a.client_phone})" if a.client_phone else ""
                lines.append(f"- {a.date} {a.time}: {client}{phone_info}")

            result = f"Citas próximas ({days_ahead} días):\n"
            result += "\n".join(lines)
            return result

    except Exception as e:
        logger.error("tool_get_appointments error: %s", e)
        return f"Error al consultar el calendario: {e}"


def tool_search_web(query: str, max_results: int = 5) -> str:
    """
    Busca información actualizada en la web. Útil para subvenciones (BOE), noticias o conocimiento general.

    Args:
        query: La consulta de búsqueda.
        max_results: Número máximo de resultados (por defecto 5).
    """
    if DDGS is None:
        return "Búsqueda web no disponible: duckduckgo-search no está instalado."
    try:
        with DDGS() as ddgs:
            search_results = list(ddgs.text(query, max_results=max_results))

        if not search_results:
            return f"No se encontraron resultados para: '{query}'."

        lines = []
        for i, r in enumerate(search_results, 1):
            lines.append(f"{i}. {r.get('title', 'Sin título')}")
            lines.append(f"   Enlace: {r.get('href', 'N/A')}")
            lines.append(f"   Resumen: {r.get('body', 'Sin resumen')}\n")

        return f"Resultados para '{query}':\n\n" + "\n".join(lines)

    except Exception as e:
        logger.error("tool_search_web error: %s", e)
        return f"Error al buscar en la web: {e}"


def tool_schedule_appointment(
    owner_phone: str,
    date: str,
    time: str,
    client_name: str,
    client_phone: str,
    app,
) -> str:
    """
    Agenda una nueva cita en el calendario del negocio, comprobando conflictos.

    Args:
        owner_phone: Teléfono del propietario del negocio (dueño del calendario).
        date: Fecha de la cita en formato YYYY-MM-DD.
        time: Hora de la cita en formato HH:MM.
        client_name: Nombre del cliente.
        client_phone: Teléfono del cliente (puede estar vacío).
        app: Instancia Flask para el contexto de aplicación.
    """
    from core.db_models import db, Appointment

    try:
        parsed_date = _parse_date(date)
    except ValueError:
        return f"Formato de fecha incorrecto: '{date}'. Usa YYYY-MM-DD."

    try:
        with app.app_context():
            existing = Appointment.query.filter_by(
                business_phone=owner_phone,
                date=parsed_date,
                time=time
            ).first()

            if existing:
                return f"Ya existe una cita a las {time} el {date}."

            new_appt = Appointment(
                business_phone=owner_phone,
                date=parsed_date,
                time=time,
                client_name=client_name,
                client_phone=client_phone
            )
            db.session.add(new_appt)
            db.session.commit()
            logger.info("Cita agendada: %s el %s %s", client_name, date, time)
            return f"Cita agendada para {client_name} el {date} a las {time}."

    except Exception as e:
        logger.error("tool_schedule_appointment error: %s", e)
        return f"Error al agendar cita: {e}"


def tool_send_email_notification(
    owner_phone: str,
    to_email: str,
    subject: str,
    body: str,
    app,
    mail,
) -> str:
    """
    Envía un email en nombre del negocio. Requiere el teléfono del propietario
    para verificar que existe en el sistema y registrar el envío.

    Args:
        owner_phone: Teléfono del propietario del negocio que autoriza el envío.
        to_email: Dirección de correo del destinatario.
        subject: Asunto del email.
        body: Cuerpo del email en texto plano.
        app: Instancia Flask para el contexto de aplicación.
        mail: Instancia Flask-Mail.
    """
    from flask_mail import Message

    try:
        with app.app_context():
            from core.db_models import BusinessProfile, ActivityLog

            owner = BusinessProfile.query.filter_by(user_phone=owner_phone).first()
            if not owner:
                return f"Error: no existe ningún negocio registrado con el teléfono '{owner_phone}'."

            if not _EMAIL_RE.match((to_email or '').strip()):
                return f"Error: la dirección '{to_email}' no parece un email válido."

            if len(subject or '') > 200 or len(body or '') > 5000:
                return "Error: asunto o cuerpo del email demasiado largos."

            # Limite anti-abuso: evita que una inyeccion de prompt convierta esta
            # herramienta en un relay de spam/phishing en nombre del negocio.
            one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
            recent_emails = ActivityLog.query.filter(
                ActivityLog.user_phone == owner_phone,
                ActivityLog.agent_name == "Council (MCP)",
                ActivityLog.action.like("Email enviado a%"),
                ActivityLog.timestamp >= one_hour_ago,
            ).count()
            if recent_emails >= 5:
                return "Error: límite de emails por hora alcanzado para este negocio."

            msg = Message(
                subject=subject,
                sender=app.config.get('MAIL_DEFAULT_SENDER', 'zeptai.soporte@gmail.com'),
                recipients=[to_email],
                body=f"{body}\n\n---\nEnviado por {owner.business_name} a través de Zeptai."
            )
            mail.send(msg)

            ActivityLog.log(
                owner_phone,
                "Council (MCP)",
                f"Email enviado a {to_email} | Asunto: {subject[:50]}"
            )
            logger.info("Email enviado a %s | Asunto: %s", to_email, subject[:50])
            return f"Correo enviado a {to_email} con asunto '{subject}'."

    except Exception as e:
        logger.error("tool_send_email_notification error: %s", e)
        return f"Error al enviar el correo: {e}"


def tool_get_business_stats(owner_phone: str, app) -> str:
    """
    Devuelve métricas clave del negocio: tickets del mes, citas pendientes y uso LLM reciente.

    Args:
        owner_phone: Teléfono del propietario del negocio.
        app: Instancia Flask para el contexto de aplicación.
    """
    try:
        with app.app_context():
            from core.db_models import Ticket, Appointment, LLMCall
            from sqlalchemy import func

            now = datetime.now()
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            today = now.date()

            # Tickets del mes
            month_tickets = Ticket.query.filter(
                Ticket.user_phone == owner_phone,
                Ticket.date >= month_start
            ).all()
            month_total = sum(t.total or 0 for t in month_tickets)

            # Citas próximas (7 días)
            upcoming = Appointment.query.filter(
                Appointment.business_phone == owner_phone,
                Appointment.date >= today,
                Appointment.date <= today + timedelta(days=7)
            ).count()

            # Uso LLM últimos 7 días
            week_ago = now - timedelta(days=7)
            llm_calls = LLMCall.query.filter(
                LLMCall.user_phone == owner_phone,
                LLMCall.created_at >= week_ago
            ).all()
            llm_count = len(llm_calls)
            llm_cost = sum(c.cost_usd or 0 for c in llm_calls)

            lines = [
                f"Estadísticas del negocio ({owner_phone}):",
                f"",
                f"Tickets este mes: {len(month_tickets)} — Total: {month_total:.2f}€",
                f"Citas próximos 7 días: {upcoming}",
                f"Llamadas IA últimos 7 días: {llm_count} — Coste estimado: ${llm_cost:.4f}",
            ]
            return "\n".join(lines)

    except Exception as e:
        logger.error("tool_get_business_stats error: %s", e)
        return f"Error al obtener estadísticas: {e}"
