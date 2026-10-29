"""
LLM Tracker — registra métricas de cada llamada a modelos de lenguaje y generación.

Uso básico:
    import time
    from core.llm_tracker import track

    start = time.time()
    response = openai.chat.completions.create(model="gpt-4o", ...)
    track(user_phone, "gpt-4o", "chat_main", response, int((time.time()-start)*1000))

Para Runway (sin tokens, solo latencia):
    track(user_phone, "gen4_turbo", "runway_video", latency_ms=ms, extra={"duration_s": 5})
"""

import logging
import time as _time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ── Precios por millón de tokens (USD) · Abril 2025 ────────────────────────
# Fuente: platform.openai.com/docs/models + runwayml.com/pricing
PRICING: dict = {
    "gpt-4o": {
        "input_per_1m": 2.50,
        "output_per_1m": 10.00,
    },
    "gpt-4o-mini": {
        "input_per_1m": 0.15,
        "output_per_1m": 0.60,
    },
    "dall-e-3": {
        "per_image": 0.040,          # 1024×1024 standard (retirado de la API el 12/05/2026)
    },
    "gpt-image-1": {
        "per_image": 0.167,          # 1024×1024 quality="high", la que usa el agente de marketing
    },
    "text-embedding-3-small": {
        "input_per_1m": 0.02,
        "output_per_1m": 0.0,
    },
    "whisper-1": {
        "per_minute": 0.006,
    },
    "gen3a_turbo": {
        "per_second_video": 0.05,    # ~$0.05/s · vídeo de 5s ≈ $0.25
    },
    "gen4_turbo": {
        # Runway factura en creditos, no en tokens: gen4_turbo consume 5 creditos
        # por segundo y el credito cuesta 0,01 EUR (paquete de 1000 por 10 EUR).
        # Un clip de 5s = 25 creditos = 0,25 EUR. Se anota en la columna cost_usd
        # sin convertir divisa, igual que el resto de la tabla: el panel mide
        # ordenes de magnitud de gasto, no es contabilidad.
        "per_second_video": 0.05,
    },
}


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, extra: dict) -> float:
    p = PRICING.get(model, {})
    if "input_per_1m" in p:
        return round(
            (prompt_tokens * p["input_per_1m"] + completion_tokens * p["output_per_1m"]) / 1_000_000,
            6,
        )
    if "per_image" in p:
        return p["per_image"]
    if "per_minute" in p:
        return round(extra.get("duration_min", 0) * p["per_minute"], 6)
    if "per_second_video" in p:
        return round(extra.get("duration_s", 5) * p["per_second_video"], 4)
    return 0.0


def track(
    user_phone: str | None,
    model: str,
    stage: str,
    response=None,
    latency_ms: int = 0,
    success: bool = True,
    error: str | None = None,
    extra: dict | None = None,
) -> None:
    """
    Guarda una métrica de llamada LLM en la base de datos.

    :param user_phone: Teléfono del usuario que desencadenó la llamada (None para tareas del sistema).
    :param model:      ID del modelo (gpt-4o, gen3a_turbo, dall-e-3, whisper-1, …).
    :param stage:      Nombre descriptivo del paso (video_stage1, chat_main, runway_video, …).
    :param response:   Objeto de respuesta de OpenAI (para extraer usage.tokens). Puede ser None.
    :param latency_ms: Latencia medida externamente en milisegundos.
    :param success:    True si la llamada tuvo éxito.
    :param error:      Mensaje de error si falló.
    :param extra:      Datos adicionales según el modelo (p.ej. {"duration_s": 5} para Runway).
    """
    extra = extra or {}

    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    if response is not None and hasattr(response, "usage") and response.usage:
        prompt_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(response.usage, "completion_tokens", 0) or 0
        total_tokens = getattr(response.usage, "total_tokens", 0) or 0

    cost = _estimate_cost(model, prompt_tokens, completion_tokens, extra)

    try:
        from core.db_models import db, LLMCall

        call = LLMCall(
            user_phone=user_phone,
            model=model,
            stage=stage,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            success=success,
            error_message=error,
        )
        db.session.add(call)
        db.session.commit()
        logger.debug(
            "LLMTracker [%s/%s] – %dms · %d tokens · $%.5f",
            model, stage, latency_ms, total_tokens, cost,
        )
    except Exception as e:
        logger.error("LLMTracker: error guardando métrica: %s", e)
        try:
            from core.db_models import db
            db.session.rollback()
        except Exception:
            pass

    # Emitir métricas Prometheus en paralelo (no bloquea si falla)
    try:
        from core.telemetry import record_llm
        record_llm(
            model=model,
            stage=stage,
            success=success,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
        )
    except Exception:
        pass


@contextmanager
def timed_track(user_phone: str | None, model: str, stage: str, extra: dict | None = None):
    """
    Context manager que mide el tiempo automáticamente.

    Uso:
        with timed_track(user_phone, "gpt-4o", "chat_main") as t:
            response = openai.chat.completions.create(...)
            t["response"] = response   # pasar la respuesta para extraer tokens
    """
    tracker: dict = {"response": None, "success": True, "error": None}
    start = _time.time()
    try:
        yield tracker
    except Exception as exc:
        tracker["success"] = False
        tracker["error"] = str(exc)
        raise
    finally:
        latency_ms = int((_time.time() - start) * 1000)
        track(
            user_phone=user_phone,
            model=model,
            stage=stage,
            response=tracker.get("response"),
            latency_ms=latency_ms,
            success=tracker["success"],
            error=tracker.get("error"),
            extra=extra,
        )
