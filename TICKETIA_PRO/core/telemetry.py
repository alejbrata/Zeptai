"""
Observabilidad — OpenTelemetry tracing + Prometheus custom metrics.

Tracing:  OTLP HTTP → Jaeger  (env: OTEL_EXPORTER_OTLP_ENDPOINT, ej. http://jaeger:4318)
Metrics:  prometheus_client   → expuesto en /api/prometheus (scrapeado por Prometheus)

Uso desde llm_tracker:
    from core.telemetry import record_llm
    record_llm(model, stage, success, prompt_tokens, completion_tokens, latency_ms, cost_usd)

Uso desde DeepEval:
    from core.telemetry import update_rag_score
    update_rag_score("faithfulness", 0.92)
"""
import os
import logging

logger = logging.getLogger(__name__)

# ── Prometheus metrics (singleton de módulo, creados una sola vez) ─────────────
from prometheus_client import Counter, Histogram, Gauge, Info

llm_requests = Counter(
    'zeptai_llm_requests_total',
    'Total de llamadas a modelos LLM',
    ['model', 'stage', 'success'],
)

llm_tokens = Counter(
    'zeptai_llm_tokens_total',
    'Total de tokens consumidos',
    ['model', 'stage', 'token_type'],  # token_type: prompt | completion
)

llm_latency = Histogram(
    'zeptai_llm_latency_seconds',
    'Latencia de llamadas LLM en segundos',
    ['model', 'stage'],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

llm_cost = Counter(
    'zeptai_llm_cost_usd_total',
    'Coste acumulado estimado en USD',
    ['model', 'stage'],
)

rag_score = Gauge(
    'zeptai_rag_score',
    'Puntuación de calidad RAG de DeepEval (0.0 – 1.0)',
    ['metric'],
)

app_info = Info('zeptai_app', 'Metainformación de la aplicación')
app_info.info({'version': '1.0', 'service': 'zeptai', 'env': os.environ.get('FLASK_ENV', 'development')})


# ── API pública ────────────────────────────────────────────────────────────────

def record_llm(
    model: str,
    stage: str,
    success: bool,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    cost_usd: float,
) -> None:
    """Actualiza todos los contadores Prometheus de una llamada LLM."""
    try:
        success_label = 'true' if success else 'false'
        llm_requests.labels(model=model, stage=stage, success=success_label).inc()
        if prompt_tokens:
            llm_tokens.labels(model=model, stage=stage, token_type='prompt').inc(prompt_tokens)
        if completion_tokens:
            llm_tokens.labels(model=model, stage=stage, token_type='completion').inc(completion_tokens)
        llm_latency.labels(model=model, stage=stage).observe(latency_ms / 1000.0)
        if cost_usd:
            llm_cost.labels(model=model, stage=stage).inc(cost_usd)
    except Exception as exc:
        logger.debug("telemetry.record_llm error: %s", exc)


def update_rag_score(metric: str, score: float) -> None:
    """Actualiza el gauge de calidad RAG; llamar desde los resultados de DeepEval."""
    try:
        rag_score.labels(metric=metric).set(score)
    except Exception as exc:
        logger.debug("telemetry.update_rag_score error: %s", exc)


# ── OpenTelemetry tracing ──────────────────────────────────────────────────────

def init_tracing(app) -> None:
    """
    Inicializa OTel si OTEL_EXPORTER_OTLP_ENDPOINT está definido.
    Instrumenta Flask automáticamente (span por cada request HTTP).
    """
    endpoint = os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT', '').rstrip('/')
    if not endpoint:
        logger.info("OTel: OTEL_EXPORTER_OTLP_ENDPOINT no definido — trazas desactivadas")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.instrumentation.flask import FlaskInstrumentor

        resource = Resource(attributes={SERVICE_NAME: 'zeptai'})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
        )
        trace.set_tracer_provider(provider)

        FlaskInstrumentor().instrument_app(app)
        logger.info("OTel: trazas activas → %s", endpoint)

    except ImportError as exc:
        logger.warning("OTel: paquetes no instalados (%s) — trazas desactivadas", exc)
    except Exception as exc:
        logger.error("OTel: error en init_tracing: %s", exc)


def init_sqlalchemy_tracing() -> None:
    """
    Instrumenta SQLAlchemy para emitir spans de queries DB.
    Llamar dentro de app_context, después de db.init_app().
    """
    if not os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT'):
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from core.db_models import db
        SQLAlchemyInstrumentor().instrument(engine=db.engine)
        logger.info("OTel: SQLAlchemy instrumentado")
    except Exception as exc:
        logger.warning("OTel: SQLAlchemy instrumentation failed: %s", exc)
