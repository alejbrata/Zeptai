"""
Cliente MCP con soporte de dos transportes:

1. Streamable HTTP — PREFERIDO en producción
   - Requiere que mcp_server_sse.py esté corriendo como proceso separado
     (el nombre es histórico; hoy sirve Streamable HTTP en /mcp).
   - Se activa definiendo MCP_URL, p.ej. MCP_URL=http://mcp:8001/mcp
   - Envía "Authorization: Bearer <MCP_AUTH_TOKEN>"; sin token el servidor responde 401.
   - Ventaja: sin fork de proceso por llamada.

2. stdio — FALLBACK para desarrollo/testing
   - Lanza mcp_server.py como subprocess en cada llamada.
   - Sin dependencias de proceso externo, pero ~500 ms de overhead por fork.
   - Se usa automáticamente si MCP_URL no está definida.
"""

import asyncio
import os
import json
import logging
import time as _time
from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

logger = logging.getLogger(__name__)


class ZeptaiMCPClient:
    """
    Ejecuta un loop de agente OpenAI usando herramientas expuestas via MCP.
    Selecciona automáticamente el transporte HTTP o stdio según la configuración.
    """

    def __init__(self):
        self._openai = AsyncOpenAI()
        self._url = os.environ.get('MCP_URL')  # e.g. http://mcp:8001/mcp
        token = os.environ.get('MCP_AUTH_TOKEN', '').strip()
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._server_script = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "mcp_server.py"
        )

    async def execute_agent_loop(self, system_prompt: str, user_message: str) -> str:
        """
        Ejecuta un turno completo del agente: LLM → tool calls → respuesta final.
        Devuelve el texto de respuesta para enviar al usuario.
        """
        if self._url:
            return await self._run_with_http(system_prompt, user_message)
        else:
            return await self._run_with_stdio(system_prompt, user_message)

    # ── Streamable HTTP transport ─────────────────────────────────────────────

    async def _run_with_http(self, system_prompt: str, user_message: str) -> str:
        """Conecta al servidor MCP remoto ya en ejecución (sin fork de proceso)."""
        try:
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(self._url, headers=self._headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await self._agent_loop(session, system_prompt, user_message)

        except Exception as e:
            logger.warning("MCP-HTTP error conectando a %s: %s — cayendo a stdio", self._url, e)
            return await self._run_with_stdio(system_prompt, user_message)

    # ── stdio transport ───────────────────────────────────────────────────────

    async def _run_with_stdio(self, system_prompt: str, user_message: str) -> str:
        """Lanza mcp_server.py como subprocess y se comunica por stdio."""
        try:
            server_params = StdioServerParameters(
                command="python",
                args=[self._server_script],
                env=os.environ.copy()
            )

            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await self._agent_loop(session, system_prompt, user_message)

        except Exception as e:
            logger.error("MCP-stdio error: %s", e)
            return f"Error del sistema al acceder a las herramientas: {e}"

    # ── Loop común ────────────────────────────────────────────────────────────

    async def _agent_loop(
        self,
        session: ClientSession,
        system_prompt: str,
        user_message: str
    ) -> str:
        """
        Lógica compartida entre SSE y stdio:
        1. Obtener lista de tools del servidor MCP.
        2. Primera llamada al LLM con las tools disponibles.
        3. Si hay tool_calls: ejecutarlas y hacer segunda llamada para síntesis.
        4. Devolver texto final.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # Descubrir herramientas disponibles en el servidor MCP
        tools_response = await session.list_tools()
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.inputSchema,
                },
            }
            for t in tools_response.tools
        ]

        # Primera llamada al LLM
        _t0 = _time.time()
        response = await self._openai.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=openai_tools,
        )
        _latency = int((_time.time() - _t0) * 1000)
        self._track_call("gpt-4o", "council_mcp_main", response, _latency)

        response_msg = response.choices[0].message
        messages.append(response_msg)

        # Ejecutar tool calls si los hay
        if response_msg.tool_calls:
            for tool_call in response_msg.tool_calls:
                logger.info("MCP tool solicitada: %s", tool_call.function.name)
                args = json.loads(tool_call.function.arguments)

                try:
                    result = await session.call_tool(tool_call.function.name, arguments=args)
                    tool_result = result.content[0].text if result.content else "Éxito sin salida."
                except Exception as e:
                    logger.error("MCP tool %s error: %s", tool_call.function.name, e)
                    tool_result = f"Error ejecutando {tool_call.function.name}: {e}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": tool_result,
                })

            # Segunda llamada para sintetizar los resultados de las tools
            _t0 = _time.time()
            final_response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=messages,
            )
            _latency = int((_time.time() - _t0) * 1000)
            self._track_call("gpt-4o", "council_mcp_followup", final_response, _latency)
            return final_response.choices[0].message.content or ""

        return response_msg.content or ""

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _track_call(self, model: str, stage: str, response, latency_ms: int) -> None:
        """Registra la llamada LLM en la BD de métricas (best-effort, sin romper el flujo)."""
        try:
            from core.llm_tracker import track
            track(None, model, stage, response, latency_ms)
        except Exception as e:
            logger.debug("MCP tracking error (non-critical): %s", e)


# ── Singleton ─────────────────────────────────────────────────────────────────

_mcp_client: ZeptaiMCPClient | None = None


def get_mcp_client() -> ZeptaiMCPClient:
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = ZeptaiMCPClient()
    return _mcp_client
