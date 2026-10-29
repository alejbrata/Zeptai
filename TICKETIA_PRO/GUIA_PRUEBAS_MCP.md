# Guía de pruebas: MCP (Model Context Protocol) en Zeptai

Zeptai implementa MCP en los dos roles:

- **Servidor**: `mcp_server_sse.py` expone 6 herramientas de negocio por **Streamable HTTP** en `http://mcp:8001/mcp` (red Docker) y `http://localhost:8001/mcp` (solo desde la propia máquina). El nombre del fichero es histórico: nació con el transporte HTTP+SSE de la especificación de 2024, sustituido en la revisión 2025-03-26.
- **Cliente**: `core/mcp_client.py` descubre las herramientas del servidor, las traduce al formato de tools de OpenAI y ejecuta el ciclo LLM → herramientas → síntesis. Si `MCP_URL` no está definida, lanza `mcp_server.py` por stdio.

Las herramientas (implementadas una sola vez en `core/mcp_tools.py`): `get_financial_summary`, `get_appointments`, `search_web` (DuckDuckGo), `schedule_appointment`, `send_email_notification` (máx. 5/hora por negocio) y `get_business_stats`.

## Seguridad

Las herramientas reciben el teléfono del negocio como argumento, así que el servidor **no acepta ninguna petición sin `Authorization: Bearer <MCP_AUTH_TOKEN>`** y no arranca si la variable no está definida. El puerto solo se publica en `127.0.0.1`. El token lo comparte únicamente la app web. Pendiente: un token por negocio para que el inquilino salga del token y no del argumento.

## 1. Servidor: listar y llamar herramientas (sin coste de API)

```bash
docker compose exec web python mcp_demo_client.py
```

Resultado esperado: la lista de las 6 herramientas y la salida de `get_business_stats` para el negocio demo.

```bash
docker compose exec web python mcp_demo_client.py --sin-token
```

Resultado esperado: `401 Unauthorized`.

## 2. Cliente: el Consejo usando herramientas MCP

En la interfaz web el Consejo se ejecuta con `use_mcp=False` (latencia, coste y herramientas con efectos como el envío de emails). Para enseñarlo de forma controlada:

```bash
docker compose exec web python mcp_demo_client.py --consejo "¿Hay ayudas públicas vigentes para digitalizar una consultora de IA?"
```

Resultado esperado: en los logs aparece `MCP tool solicitada: search_web` cuando cada agente decide buscar en la web, y su opinión de la ronda 1 se basa en esos resultados; después siguen las réplicas y el plan del Secretario. Consume unas 9-12 llamadas a GPT-4o.

## 3. Cliente externo (Claude Desktop u otro)

Cualquier cliente MCP compatible con Streamable HTTP puede conectarse a `http://localhost:8001/mcp` enviando la cabecera `Authorization: Bearer <MCP_AUTH_TOKEN>`. Ojo: con el token de servidor actual, ese cliente vería los datos de todos los negocios; es una demostración local, no un despliegue.

## 4. Tests

```bash
docker compose exec web python -m pytest tests/test_mcp_tools.py -v
```
