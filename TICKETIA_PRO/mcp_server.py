"""
Servidor MCP con transporte stdio (fallback / desarrollo).

Lanzado como subprocess por core/mcp_client.py cuando MCP_URL no está definida.
Para producción/Docker usar mcp_server_sse.py (proceso persistente, menor latencia).

Arranque manual (sólo para debug):
    python mcp_server.py
"""

import logging
from mcp.server.fastmcp import FastMCP
from app import app, mail
from core.mcp_tools import (
    tool_get_financial_summary,
    tool_get_appointments,
    tool_search_web,
    tool_schedule_appointment,
    tool_send_email_notification,
    tool_get_business_stats,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("ZeptaiDataServer")


@mcp.tool()
def get_financial_summary(user_phone: str) -> str:
    """
    Devuelve un resumen de los gastos del usuario basado en sus tickets registrados.

    Args:
        user_phone: Número de teléfono exacto del usuario en la base de datos.
    """
    return tool_get_financial_summary(user_phone, app)


@mcp.tool()
def get_appointments(owner_phone: str, days_ahead: int = 7) -> str:
    """
    Lista las próximas citas del negocio en los próximos N días.

    Args:
        owner_phone: Teléfono del propietario del negocio.
        days_ahead: Número de días hacia adelante a consultar (por defecto 7).
    """
    return tool_get_appointments(owner_phone, app, days_ahead)


@mcp.tool()
def search_web(query: str, max_results: int = 5) -> str:
    """
    Busca información actualizada en la web. Útil para subvenciones (BOE), noticias o conocimiento general.

    Args:
        query: La consulta de búsqueda.
        max_results: Número máximo de resultados (por defecto 5).
    """
    return tool_search_web(query, max_results)


@mcp.tool()
def schedule_appointment(
    owner_phone: str,
    date: str,
    time: str,
    client_name: str,
    client_phone: str = ""
) -> str:
    """
    Agenda una nueva cita en el calendario del negocio, comprobando conflictos.

    Args:
        owner_phone: Teléfono del propietario del negocio (dueño del calendario).
        date: Fecha de la cita en formato YYYY-MM-DD.
        time: Hora de la cita en formato HH:MM.
        client_name: Nombre del cliente.
        client_phone: Teléfono del cliente (opcional).
    """
    return tool_schedule_appointment(owner_phone, date, time, client_name, client_phone, app)


@mcp.tool()
def send_email_notification(owner_phone: str, to_email: str, subject: str, body: str) -> str:
    """
    Envía un email en nombre del negocio. Requiere el teléfono del propietario
    para verificar que existe en el sistema y registrar el envío.

    Args:
        owner_phone: Teléfono del propietario del negocio que autoriza el envío.
        to_email: Dirección de correo del destinatario.
        subject: Asunto del email.
        body: Cuerpo del email en texto plano.
    """
    return tool_send_email_notification(owner_phone, to_email, subject, body, app, mail)


@mcp.tool()
def get_business_stats(owner_phone: str) -> str:
    """
    Devuelve métricas clave del negocio: tickets del mes, citas pendientes y uso LLM reciente.

    Args:
        owner_phone: Teléfono del propietario del negocio.
    """
    return tool_get_business_stats(owner_phone, app)


if __name__ == "__main__":
    # stdio: FastMCP usa sys.stdout para el protocolo — no imprimir nada a stdout
    mcp.run()
