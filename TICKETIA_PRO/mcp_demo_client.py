"""
Demostración del servidor MCP de Zeptai desde línea de comandos.

    docker compose exec web python mcp_demo_client.py
        → se autentica con MCP_AUTH_TOKEN, lista las 6 herramientas y llama a
          get_business_stats para el negocio demo. Sin coste de API.

    docker compose exec web python mcp_demo_client.py --sin-token
        → la misma petición sin token: el servidor responde 401.

    docker compose exec web python mcp_demo_client.py --consejo "tema"
        → ejecuta una sesión del Consejo con use_mcp=True: en la ronda 1 cada
          agente recibe las herramientas MCP y puede decidir usarlas (por ejemplo
          search_web). La interfaz web lo tiene desactivado (ver P290); esto
          permite enseñarlo de forma controlada. Consume API (≈ 9-12 llamadas).
"""
import asyncio
import os
import sys

DEMO_PHONE = os.environ.get("DEMO_PHONE", "+34600000001")


async def list_and_call(with_token: bool = True):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ.get("MCP_URL", "http://localhost:8001/mcp")
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if with_token and token else {}
    print(f"Conectando a {url} {'con' if headers else 'SIN'} token...")

    try:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print("Herramientas:", ", ".join(t.name for t in tools.tools))
                result = await session.call_tool("get_business_stats", {"owner_phone": DEMO_PHONE})
                print("\nget_business_stats →\n" + result.content[0].text)
    except Exception as e:
        # Sin token el servidor corta con 401 antes del handshake MCP
        print(f"Conexión rechazada: {e!r}")


async def council(topic: str):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import app
    from core.db_models import BusinessProfile
    from modules.council.orchestrator import CouncilManager

    with app.app_context():
        user = BusinessProfile.query.filter_by(user_phone=DEMO_PHONE).first()
        context = f"Negocio: {user.business_name}, Sector: {user.static_knowledge_dict.get('sector', 'General')}" \
            if user else "Negocio Genérico"
        async for event in CouncilManager().run_session(topic, context, use_mcp=True, owner_phone=DEMO_PHONE):
            if event.get("type") in ("message", "plan"):
                who = event.get("name", "Secretario")
                print(f"\n[{who}] {event['text']}")
            elif event.get("type") == "divider":
                print(f"\n--- {event['text']} ---")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--consejo":
        topic = " ".join(args[1:]) or "¿Hay ayudas públicas vigentes para digitalizar una consultora de IA en España?"
        asyncio.run(council(topic))
    else:
        asyncio.run(list_and_call(with_token="--sin-token" not in args))
