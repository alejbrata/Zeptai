"""
Servidor MCP remoto con transporte Streamable HTTP y autenticación por token.

El nombre del fichero es histórico: nació con el transporte HTTP+SSE de la
especificación de 2024, que la revisión 2025-03-26 sustituyó por Streamable HTTP
(un único endpoint, /mcp, que responde a cada POST con JSON o con un stream).

Ventaja frente al servidor stdio (mcp_server.py):
- El proceso se lanza UNA SOLA VEZ y permanece activo.
- El cliente MCP se conecta via HTTP, sin fork de proceso por cada llamada.
- Permite múltiples clientes concurrentes conectados al mismo servidor.

Seguridad: las herramientas leen y escriben datos de cualquier negocio a partir
del teléfono que reciben como argumento, así que el servidor no acepta ninguna
petición sin la cabecera "Authorization: Bearer <MCP_AUTH_TOKEN>" y no arranca
si esa variable no está definida. El token lo comparte solo la app web (el único
cliente legítimo). El aislamiento por negocio con un token por inquilino queda
como siguiente paso.

Arranque:
    MCP_AUTH_TOKEN=... python mcp_server_sse.py      (puerto 8001 por defecto)

El cliente (core/mcp_client.py) lo usa si MCP_URL está definida:
    MCP_URL=http://mcp:8001/mcp
"""

import os
import hmac
import logging
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

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

PORT = int(os.environ.get('MCP_SSE_PORT', 8001))

mcp = FastMCP("ZeptaiDataServer", host="0.0.0.0", port=PORT)


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


class BearerAuthMiddleware:
    """Middleware ASGI: rechaza con 401 toda petición HTTP sin el token correcto.

    Es ASGI puro (no BaseHTTPMiddleware) para no interferir con las respuestas en
    streaming del transporte. La comparación es de tiempo constante.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                from starlette.responses import JSONResponse
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if not token:
        raise SystemExit("MCP_AUTH_TOKEN no definida: el servidor MCP no arranca sin autenticación.")

    app_asgi = BearerAuthMiddleware(mcp.streamable_http_app(), token)
    logger.info("MCP (Streamable HTTP) arrancando en http://0.0.0.0:%s/mcp", PORT)
    uvicorn.run(app_asgi, host="0.0.0.0", port=PORT, log_level="info")
