# Zeptai — Plataforma de IA para Pymes

> Trabajo de Fin de Máster · IA Generativa aplicada a la gestión de pequeñas y medianas empresas

---

## Resumen

**Zeptai** es una PWA (Progressive Web App) que combina múltiples modelos de IA (GPT-4o, Whisper, Runway Gen-4) para automatizar la gestión diaria de una pyme: digitalización de gastos mediante OCR inteligente, búsqueda de subvenciones, análisis financiero proactivo, generación de contenido de marketing y atención al cliente con RAG.

---

## Arquitectura

```
┌─────────────────────────────────────────────────────────────┐
│                        CLIENTE (PWA)                        │
│          HTML + Tailwind CSS · Service Worker · Push        │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTPS
┌────────────────────────▼────────────────────────────────────┐
│                   FLASK (Gunicorn + gevent)                  │
│   routes/web.py    routes/api.py    mcp_server_sse.py       │
└──────┬────────────┬──────────────┬──────────────────────────┘
       │            │              │
┌──────▼──────┐ ┌───▼────────┐ ┌──▼───────────────────────┐
│  Módulos IA │ │ PostgreSQL │ │  MCP Server (FastMCP HTTP)│
│             │ │ + pgvector │ │  tools: stats, email, etc  │
│ • chatbot   │ └────────────┘ └──────────────────────────┘
│ • agents    │
│   - grant_hunter        ← APScheduler (09:00 diario)
│   - business_health     ← APScheduler (09:00 diario)
│   - networker           ← APScheduler (09:00 diario)
│   - post_sales
│   - admin_redactor
│   - marketing_agent     ← Runway Gen-4 Turbo
│ • council (multi-agent) ← SSE streaming
└─────────────┘

Observabilidad:
  Prometheus  → métricas LLM (coste, latencia, tokens)
  Grafana     → dashboards en tiempo real
  Jaeger      → trazas distribuidas (OpenTelemetry)

APIs Externas:
  OpenAI  → GPT-4o (chat, vision, análisis)
          → Whisper (transcripción de voz)
          → text-embedding-3-small (RAG)
  Runway  → Gen-4 Turbo (generación de vídeo)
```

---

## Stack Tecnológico

| Capa | Tecnología |
|------|-----------|
| Backend | Python 3.11 · Flask · SQLAlchemy · Gunicorn+gevent |
| Base de datos | PostgreSQL + pgvector (prod) · SQLite (tests) |
| IA | OpenAI GPT-4o · Whisper · gpt-image-1 · Runway Gen-4 |
| RAG | text-embedding-3-small · pgvector · chunking recursivo |
| Frontend | HTML5 · Tailwind CSS · PWA (manifest + SW) |
| Observabilidad | OpenTelemetry · Prometheus · Grafana · Jaeger |
| Tareas programadas | APScheduler |
| Herramientas IA | FastMCP (Model Context Protocol) |
| CI/CD | GitHub Actions (`.github/workflows/mlops.yml`) |
| Contenedores | Docker · Docker Compose (7 servicios) |

---

## Instalación local (5 minutos)

### Requisitos previos

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) instalado y en ejecución
- API key de OpenAI con acceso a GPT-4o ([obtener aquí](https://platform.openai.com/api-keys))

### Pasos

```bash
# 1. Clonar el repositorio
git clone https://github.com/alejbrata/Zeptai.git
cd Zeptai

# 2. Crear el fichero de entorno
cp .env.example .env
```

Abre `.env` y rellena al menos estas dos variables:

```
SECRET_KEY=<cadena aleatoria larga>
OPENAI_API_KEY=sk-...
```

Las demás son opcionales (ver tabla de variables más abajo).

```bash
# 3. Levantar todos los servicios
docker compose up -d

# 4. Cargar los datos de demo (usuario + configuración IA + 15 tickets)
docker compose exec web python seed_all.py

# 5. Abrir la aplicación
# http://localhost:5000   →  Usuario: admin@demo.com  |  Contraseña: demo1234
```

El seed crea: usuario demo con plan PRO_FULL, configuración del asistente IA (sector, FAQ, garantías) con 7 chunks indexados en pgvector, y 15 tickets de historial de gastos.

El PDF de base de conocimiento **no se precarga** — se sube en directo desde `/documents` para demostrar el flujo de ingesta RAG en tiempo real.

Para resetear la demo en cualquier momento:

```bash
docker compose exec web python seed_all.py
```

### Servicios disponibles tras `docker compose up`

| Servicio | URL | Descripción |
|---|---|---|
| Aplicación | http://localhost:5000 | Flask app principal |
| Grafana | http://localhost:3000 | Dashboards de métricas LLM (admin: `zeptai`) |
| Prometheus | http://localhost:9090 | Series temporales y PromQL |
| Jaeger | http://localhost:16686 | Trazas distribuidas OpenTelemetry |

### Variables de entorno

| Variable | Obligatoria | Descripción |
|----------|:-----------:|-------------|
| `SECRET_KEY` | Sí | Clave Flask — genera con `python -c "import secrets; print(secrets.token_hex(32))"` |
| `OPENAI_API_KEY` | Sí | API key de OpenAI ([platform.openai.com/api-keys](https://platform.openai.com/api-keys)) |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | No | Gmail + App Password para envío de correos |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | No | Push notifications PWA |
| `RUNWAYML_API_SECRET` | No | Generación de vídeo con Runway Gen-4 |
| `PUBLIC_URL` | No | URL pública para links en correos (por defecto `http://localhost:5000`) |

---

## Tests

```bash
# Desde el directorio TICKETIA_PRO
cd TICKETIA_PRO
python -m pytest tests/ -v --ignore=tests/test_deepeval_rag.py --ignore=tests/test_observability.py
```

Los tests usan SQLite en memoria y mocks de OpenAI — no requieren red ni coste de API.

- `test_observability.py` requiere `docker compose up` (Prometheus + Grafana + Jaeger)
- `test_deepeval_rag.py` requiere `OPENAI_API_KEY` real y usuario demo configurado

**Cobertura:** 143 tests — autenticación, agentes proactivos, MCP tools, métricas LLM, admin redactor.

---

## Pipeline CI/CD

El workflow `.github/workflows/mlops.yml` se ejecuta en cada push a `main`, `dev` y `feature/*`:

1. **Install dependencies** — `pip install -r requirements.txt`
2. **Unit tests** — `pytest tests/` (ignora los que requieren Docker o API real)
3. **Agent evaluations** — `python mlops/eval_agents.py` (validación de schema sin llamadas LLM)
4. **RAG evaluations** — `python mlops/eval_rag.py` (solo si `OPENAI_API_KEY` está configurado en Secrets)

---

## Módulos principales

### Agentes proactivos (background — APScheduler)
- **`business_health.py`** — Analiza gastos diarios, proyecta fin de mes, alerta si hay desviación >20%
- **`grant_hunter.py`** — Escanea subvenciones (BOE/CDTI/Kit Digital), filtra por sector con GPT-4o-mini
- **`networker.py`** — Detecta sinergias entre usuarios de la plataforma
- **`post_sales.py`** — Detecta quejas e incidencias en mensajes de clientes

### Módulo Council (multi-agente con SSE streaming)
3 agentes debaten en tiempo real:
- **El Socio** — perspectiva comercial y ventas
- **El Gestor** — perspectiva fiscal y legal
- **El Coach** — estrategia y productividad

### Observabilidad LLMOps
- `core/telemetry.py` — métricas Prometheus custom: coste por llamada, latencia p50/p95, tokens, puntuación RAG
- Dashboard Grafana provisionado automáticamente (`grafana/provisioning/`)
- Trazas OpenTelemetry → Jaeger: span por request HTTP + sub-spans de queries BD
- `tests/test_observability.py` — 18 tests end-to-end del stack

### MLOps
- `mlops/eval_agents.py` — evaluaciones de schema de outputs sin llamadas LLM reales
- `mlops/eval_rag.py` — evaluación RAG con DeepEval (Faithfulness, Answer Relevancy, Context Precision)

---

## Autor

**Alejandro Bravo** — TFM Máster en Ingeniería y Desarrollo de Soluciones de IA Generativa · 2026

---

## Licencia

Copyright (C) 2026 Alejandro Bravo

Este proyecto se distribuye bajo la licencia **GNU Affero General Public License v3.0** (AGPL-3.0). Consulta el fichero [LICENSE](LICENSE).

Se usa AGPL-3.0 porque la ingesta de documentos depende de [pymupdf4llm](https://github.com/pymupdf/pymupdf4llm), que se distribuye bajo esa misma licencia. Puedes usar, modificar y redistribuir el código, siempre que publiques tus cambios bajo la misma licencia, también si ofreces la aplicación como servicio a través de una red.
