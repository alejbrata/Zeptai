import os
import re
import json
import logging
import asyncio
import queue as _queue
import threading as _threading
import time as _time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Blueprint, request, session, jsonify, Response, stream_with_context, current_app

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Europe/Madrid")


def _to_local(dt):
    """Convierte un datetime guardado en UTC a hora local para mostrar en la UI.
    SQLite no conserva tzinfo al leer (a diferencia de Postgres), así que si llega
    naive se asume UTC -- que es como se guarda en core.db_models._now()."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_LOCAL_TZ)
from core.db_models import BusinessProfile, db, Notification, GeneratedDocument, LLMCall, ChatFeedback, RagRetrieval
from modules.proactive.marketing_agent import MarketingAgent
from modules.agents.background_tasks import run_marketing_thread
from modules.tickets.logic import process_ticket_image
from modules.agents.manager import run_agent
from core.limiter import limiter
from flask_limiter.util import get_remote_address

api_bp = Blueprint('api', __name__)

def _chat_rate_key():
    """Clave de rate limiting por user_phone (sesión) o IP como fallback."""
    return session.get('user_phone') or get_remote_address()


def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_phone' not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def _sse_stream(worker_fn, event_timeout: int = 180, session_timeout: int | None = None):
    """Lanza worker_fn(queue) en un hilo daemon y devuelve un Response SSE."""
    eq = _queue.Queue()
    _DONE = object()
    _start = _time.time()

    def _target():
        try:
            worker_fn(eq)
        except Exception as e:
            logger.error("SSE worker error no capturado: %s", e)
            eq.put({"type": "error", "msg": "Error interno del servidor"})
        finally:
            eq.put(_DONE)

    _threading.Thread(target=_target, daemon=True).start()

    def generate():
        while True:
            try:
                event = eq.get(timeout=event_timeout)
            except _queue.Empty:
                if session_timeout and _time.time() - _start < session_timeout:
                    continue
                break
            if event is _DONE:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return Response(
        generate(), mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
            'X-Content-Type-Options': 'nosniff',
        }
    )

_NAV_SECTIONS = """
- /dashboard      → inicio, home, pantalla principal, volver al inicio
- /agenda         → ver agenda, mis citas, calendario, qué tengo hoy/mañana/esta semana, reuniones
- /transactions   → gastos, tickets, movimientos, facturas, mis compras
- /agents         → agentes, equipo de IA, mis bots
- /documents      → documentos, mis documentos, ver documentos, carpeta de documentos
- /knowledge      → base de conocimiento, conocimiento, archivos subidos, pdfs
- /marketing      → marketing, vídeos, redes sociales, publicidad, contenido
- /metrics        → métricas, estadísticas, análisis, rendimiento
- /chatbot-cliente→ chatbot, chat con cliente, asistente cliente, vista cliente
- /profile        → perfil, mi cuenta, contraseña, datos personales
- /council        → consejo, debate, asesores, the council
- /demo           → panel de demo, demostración, presentación
"""

_INTENT_PROMPT = """Eres el clasificador de intención de una app de gestión empresarial con IA.
El usuario ha hablado por micrófono. Debes decidir si quiere NAVEGAR a una sección o EJECUTAR una acción.

Secciones disponibles:{sections}

Comando del usuario: "{text}"

Reglas:
- NAVEGAR: el usuario quiere VER o IR a una sección ("enséñame la agenda", "quiero ver mis gastos", "llévame a métricas").
- ACCIÓN: el usuario quiere hacer algo ("agenda una reunión", "¿cuánto gasté?", "genera un presupuesto").
- Si hay duda, prefiere ACCIÓN para no interrumpir al asistente.

Responde ÚNICAMENTE con JSON válido, sin texto adicional:
{{"type":"navigate","route":"/agenda"}} o {{"type":"action"}}"""


_ALLOWED_NAV_ROUTES = {
    '/dashboard', '/agenda', '/transactions', '/agents', '/documents',
    '/knowledge', '/marketing', '/metrics', '/chatbot-cliente',
    '/profile', '/council', '/demo',
}

def _classify_voice_intent(text: str, openai_client) -> dict | None:
    """
    Usa GPT-4o-mini para clasificar si el comando de voz es navegación o acción.
    Devuelve {'type': 'navigate', 'route': '...'} o {'type': 'action'}.
    Devuelve None si falla, para que el flujo continúe con el agente normal.
    """
    try:
        prompt = _INTENT_PROMPT.format(sections=_NAV_SECTIONS, text=text)
        _t0 = _time.time()
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=30,
            temperature=0,
        )
        from core.llm_tracker import track
        track(session.get('user_phone'), "gpt-4o-mini", "voice_intent", resp,
              int((_time.time() - _t0) * 1000))
        raw = resp.choices[0].message.content.strip()
        result = json.loads(raw)
        # Validar que la ruta devuelta por el LLM esté en la whitelist
        # (previene XSS tipo javascript:... o redirecciones a rutas internas)
        if result.get('type') == 'navigate':
            if result.get('route') not in _ALLOWED_NAV_ROUTES:
                logger.warning("Voice intent devolvió ruta no permitida: %r", result.get('route'))
                return None
        logger.info("Voice intent clasificado: %r → %s", text[:60], result)
        return result
    except Exception as e:
        logger.warning("Voice intent classification falló, usando agente: %s", e)
        return None

# ---------------------------------------------------------------------------
# Marketing / Video
# ---------------------------------------------------------------------------

@api_bp.route('/generate_video_from_image', methods=['POST'])
@limiter.limit("5 per hour")
@_login_required
def generate_video_from_image():
    user_phone = session['user_phone']
    profile = BusinessProfile.query.filter_by(user_phone=user_phone).first()

    if 'image' not in request.files:
        return jsonify({"success": False, "error": "No image provided"}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"success": False, "error": "Empty filename"}), 400

    try:
        from werkzeug.utils import secure_filename
        filename = secure_filename(f"video_input_{int(datetime.now().timestamp())}_{file.filename}")
        upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'temp')
        os.makedirs(upload_dir, exist_ok=True)
        local_path = os.path.join(upload_dir, filename)
        file.save(local_path)

        # El brief es opcional: si no viene, el prompt se construye solo con el
        # analisis de la imagen y el contexto de marca del perfil.
        brief = (request.form.get('brief') or "").strip()[:300]

        # Lanzar en background para no bloquear la petición HTTP (Runway tarda 1-2 min)
        run_marketing_thread(
            user_phone=user_phone,
            prompt=brief or "Reel de producto, dinamico y acorde a la marca",
            format_type="video",
            host_url=os.environ.get('PUBLIC_URL', 'http://localhost:5000'),
            p_business_name=profile.business_name,
            p_logo_path=local_path,
        )
        return jsonify({"success": True, "processing": True,
                        "message": "¡En marcha! Te avisaremos cuando el vídeo esté listo en Documentos."})

    except Exception as e:
        logger.error("Error iniciando generación de vídeo: %s", e)
        return jsonify({"success": False, "error": "Error iniciando la generación de vídeo."}), 500


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

_OUTPUT_FILTER_PATTERN = re.compile(
    r'(SECRET_KEY|PASSWORD_HASH|DATABASE_URL|API_KEY|OPENAI_API_KEY'
    r'|VAPID_PRIVATE|MAIL_PASSWORD|system_prompt\s*[:=])',
    re.IGNORECASE
)

@api_bp.route('/api/chat', methods=['POST'])
@limiter.limit("30 per minute", key_func=_chat_rate_key)
@_login_required
def chat_api():
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "JSON inválido"}), 400

    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({"error": "Mensaje vacío"}), 400
    if not isinstance(user_message, str) or len(user_message) > 4000:
        return jsonify({"error": "Mensaje demasiado largo"}), 400

    user_phone = session['user_phone']
    profile = BusinessProfile.query.filter_by(user_phone=user_phone).first()
    if not profile:
        return jsonify({"error": "Perfil no encontrado"}), 404

    try:
        response_text = run_agent(
            user_message=user_message,
            phone_number=user_phone,
            business_profile=profile,
        )
        # Output filtering: bloquear respuestas que puedan filtrar datos sensibles
        if response_text and _OUTPUT_FILTER_PATTERN.search(response_text):
            logger.warning("Output filtering activado para %s", user_phone)
            response_text = "Lo siento, no puedo responder a esa consulta."
        return jsonify({"response": response_text or ""})
    except Exception as e:
        logger.error("Error en chat_api para %s: %s", user_phone, e, exc_info=True)
        return jsonify({"error": "Error interno. Inténtalo de nuevo."}), 500


@api_bp.route('/api/chat/feedback', methods=['POST'])
@limiter.limit("60 per minute")
@_login_required
def chat_feedback():
    data = request.get_json() or {}
    rating = data.get('rating')
    if rating not in (1, -1):
        return jsonify({"error": "rating must be 1 or -1"}), 400
    preview = str(data.get('message_preview', ''))[:200]
    db.session.add(ChatFeedback(
        user_phone=session['user_phone'],
        rating=rating,
        message_preview=preview,
    ))
    db.session.commit()
    return jsonify({"ok": True})


@api_bp.route('/api/metrics/rag', methods=['GET'])
@limiter.limit("30 per hour")
@_login_required
def rag_metrics():
    from sqlalchemy import func
    phone = session['user_phone']

    # Score medio RAG (últimas 100 consultas del usuario)
    rag_stats = db.session.query(
        func.count(RagRetrieval.id).label('total_queries'),
        func.avg(RagRetrieval.avg_score).label('avg_score'),
        func.avg(RagRetrieval.chunks_returned).label('avg_chunks'),
    ).filter(RagRetrieval.user_phone == phone).first()

    # Feedback del chatbot
    feedback_stats = db.session.query(
        func.count(ChatFeedback.id).label('total'),
        func.sum(db.case((ChatFeedback.rating == 1, 1), else_=0)).label('ok'),
        func.sum(db.case((ChatFeedback.rating == -1, 1), else_=0)).label('ko'),
    ).filter(ChatFeedback.user_phone == phone).first()

    total_fb = feedback_stats.total or 0
    ok = int(feedback_stats.ok or 0)
    ko = int(feedback_stats.ko or 0)

    return jsonify({
        "rag": {
            "total_queries": int(rag_stats.total_queries or 0),
            "avg_score": round(float(rag_stats.avg_score), 3) if rag_stats.avg_score else None,
            "avg_chunks": round(float(rag_stats.avg_chunks), 1) if rag_stats.avg_chunks else None,
            "relevance_pct": round((1 - float(rag_stats.avg_score)) * 100, 1) if rag_stats.avg_score else None,
        },
        "feedback": {
            "total": total_fb,
            "ok": ok,
            "ko": ko,
            "ok_rate": round(ok / total_fb * 100, 1) if total_fb else None,
        },
    })


# ---------------------------------------------------------------------------
# Tickets & Audio
# ---------------------------------------------------------------------------

@api_bp.route('/upload_web_ticket', methods=['POST'])
@limiter.limit("20 per hour")
@_login_required
def upload_web_ticket():
    if 'ticket' not in request.files:
        return jsonify({'error': 'No file'}), 400

    file = request.files['ticket']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    # Validar MIME por contenido real, no por extensión declarada
    _TICKET_ALLOWED_MIME = {'image/jpeg', 'image/png', 'image/webp', 'application/pdf'}
    try:
        import magic
        mime = magic.from_buffer(file.read(2048), mime=True)
        file.seek(0)
        if mime not in _TICKET_ALLOWED_MIME:
            return jsonify({'error': 'Tipo de fichero no permitido'}), 400
    except ImportError:
        file.seek(0)  # python-magic no disponible, continuar sin validación MIME

    filename = f"web_ticket_{int(datetime.now().timestamp())}.jpg"
    upload_folder = os.path.join(current_app.root_path, 'static', 'uploads')
    os.makedirs(upload_folder, exist_ok=True)
    filepath = os.path.join(upload_folder, filename)
    file.save(filepath)

    user_phone = session['user_phone']
    try:
        result_text = process_ticket_image(filepath, user_phone)
        if result_text.startswith("Error"):
            return jsonify({'error': result_text}), 500
        return jsonify({'success': True, 'message': result_text})
    except Exception as e:
        logger.error("Error procesando ticket imagen: %s", e)
        return jsonify({'error': 'No se pudo procesar la imagen'}), 500


@api_bp.route('/upload_web_audio', methods=['POST'])
@limiter.limit("20 per hour")
@_login_required
def upload_web_audio():
    if 'audio' not in request.files:
        return jsonify({'error': 'No audio'}), 400

    file = request.files['audio']

    # Guardar con extensión adecuada (soporte Safari iOS = mp4/m4a)
    original_name = file.filename or 'audio.webm'
    ext = os.path.splitext(original_name)[1] or '.webm'
    filename = f"web_audio_{int(datetime.now().timestamp())}{ext}"
    upload_folder = os.path.join(current_app.root_path, 'static', 'uploads')
    os.makedirs(upload_folder, exist_ok=True)
    filepath = os.path.join(upload_folder, filename)
    file.save(filepath)

    file_size = os.path.getsize(filepath)
    logger.info("Audio recibido: %s | tamaño: %d bytes", filename, file_size)

    if file_size < 1000:
        logger.warning("Audio demasiado pequeño (%d bytes), rechazando antes de llamar a Whisper", file_size)
        return jsonify({'error': f'Audio demasiado corto ({file_size} bytes). Habla durante al menos 1 segundo.'}), 400

    from core.clients import get_openai_client
    client = get_openai_client()

    try:
        # verbose_json devuelve la duracion del audio, que es lo que factura Whisper
        _t0 = _time.time()
        with open(filepath, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="verbose_json",
            )
        from core.llm_tracker import track
        duration_s = getattr(transcript, 'duration', None) or 0
        track(session['user_phone'], "whisper-1", "audio_transcription",
              latency_ms=int((_time.time() - _t0) * 1000),
              extra={"duration_min": float(duration_s) / 60})
        user_text = transcript.text
        logger.info("Web Audio transcrito: %s...", user_text[:50])

        # ── Clasificación de intención por LLM ───────────────────────────────
        intent = _classify_voice_intent(user_text, client)
        if intent and intent.get('type') == 'navigate':
            return jsonify({'success': True, 'navigate': intent['route']})

        user_profile = BusinessProfile.query.filter_by(user_phone=session['user_phone']).first()
        bot_response = run_agent(user_text, session['user_phone'], user_profile)

        return jsonify({'success': True, 'response': bot_response})
    except Exception as e:
        logger.error("Error procesando audio web: %s", e, exc_info=True)
        return jsonify({'error': 'No se pudo procesar el audio. Inténtalo de nuevo.'}), 500


# ---------------------------------------------------------------------------
# Council (Consejo de Asesores) — SSE streaming
# ---------------------------------------------------------------------------

@api_bp.route('/api/council/stream', methods=['POST'])
@limiter.limit("10 per hour")
@_login_required
def council_stream():
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "JSON inválido"}), 400

    topic = (data.get('topic') or '').strip()
    if not topic or len(topic) < 3:
        return jsonify({"error": "El tema es demasiado corto"}), 400
    if len(topic) > 500:
        return jsonify({"error": "El tema es demasiado largo"}), 400

    user_phone = session['user_phone']
    user = BusinessProfile.query.filter_by(user_phone=user_phone).first()
    user_context = (
        f"Negocio: {user.business_name}, Sector: {user.static_knowledge_dict.get('sector', 'General')}"
        if user else "Negocio Genérico"
    )
    _SESSION_TIMEOUT = 180

    def _worker(eq):
        _start = _time.time()

        async def _produce():
            from modules.council.orchestrator import CouncilManager
            manager = CouncilManager()
            async for event in manager.run_session(topic, user_context, use_mcp=False, owner_phone=user_phone):
                if _time.time() - _start > _SESSION_TIMEOUT:
                    logger.warning("Council session timeout para %s", user_phone)
                    break
                eq.put(event)
                logger.debug("Council event queued: %s", event.get('type'))

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_produce())
        except Exception as e:
            logger.error("Council session error: %s", e, exc_info=True)
            eq.put({"type": "error", "text": "Error interno en el Consejo. Inténtalo de nuevo."})
        finally:
            loop.close()

    return _sse_stream(_worker, event_timeout=10, session_timeout=_SESSION_TIMEOUT)


# ---------------------------------------------------------------------------
# DeepEval — evaluación de calidad LLM en tiempo real (SSE)
# ---------------------------------------------------------------------------

@api_bp.route('/api/eval/stream', methods=['POST'])
@limiter.limit("10 per hour")
@_login_required
def eval_stream():
    """SSE stream que ejecuta DeepEval case a case y emite resultados en tiempo real."""
    user_phone = session['user_phone']
    app = current_app._get_current_object()
    req_data = request.get_json(silent=True) or {}
    mode = req_data.get("mode", "auto")

    def _worker(eq):
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            from tests.test_deepeval_rag import _build_test_cases_with_rag
            from deepeval.metrics import (
                FaithfulnessMetric, AnswerRelevancyMetric,
                ContextualPrecisionMetric, ContextualRecallMetric,
            )
            from deepeval import evaluate
            from deepeval.evaluate.configs import AsyncConfig, DisplayConfig

            eq.put({"type": "status", "msg": f"Seleccionando casos (modo: {mode})..."})
            with app.app_context():
                built = _build_test_cases_with_rag(user_phone, None, mode=mode)
            eq.put({"type": "status", "msg": f"Preparados {len(built)} casos de prueba..."})

            for i, b in enumerate(built):
                eq.put({"type": "progress", "idx": i, "id": b["meta"]["id"],
                        "category": b["meta"]["categoria"],
                        "question": b["meta"]["input"],
                        "latency_ms": b["latency_ms"],
                        "chunks": b["chunks_count"],
                        "answer": (b["actual_output"] or "")[:120]})

            eq.put({"type": "status", "msg": "Ejecutando métricas DeepEval (GPT como juez)..."})
            metrics = [
                FaithfulnessMetric(threshold=0.7),
                AnswerRelevancyMetric(threshold=0.7),
                ContextualPrecisionMetric(threshold=0.5),
                ContextualRecallMetric(threshold=0.5),
            ]
            results = evaluate(
                [b["test_case"] for b in built], metrics,
                async_config=AsyncConfig(run_async=True, max_concurrent=5),
                display_config=DisplayConfig(show_indicator=False, print_results=False),
            )

            all_scores = {"faithfulness": [], "relevancy": [], "precision": [], "recall": []}
            metric_map = {
                "Faithfulness": "faithfulness", "Answer Relevancy": "relevancy",
                "Contextual Precision": "precision", "Contextual Recall": "recall",
            }
            # El coste del juez vive en cada metrica, no en el objeto de resultados:
            # EvaluationResult solo trae test_results, confident_link y test_run_id.
            cost = 0.0
            for b, tr in zip(built, results.test_results):
                cost += sum(m.evaluation_cost or 0 for m in tr.metrics_data)
                scores = {metric_map.get(m.name, m.name): round(m.score or 0, 3)
                          for m in tr.metrics_data if m.name in metric_map}
                for k, v in scores.items():
                    all_scores[k].append(v)
                passed = all(v >= (0.7 if k in ("faithfulness", "relevancy") else 0.5)
                             for k, v in scores.items())
                eq.put({"type": "result", "id": b["meta"]["id"], "passed": passed, **scores})

            avgs = {k: round(sum(v) / len(v), 3) if v else 0 for k, v in all_scores.items()}

            # Publicar las medias en el gauge zeptai_rag_score para que Grafana
            # muestre la evolucion de la calidad entre ejecuciones.
            from core.telemetry import update_rag_score
            gauge_names = {"faithfulness": "faithfulness", "relevancy": "answer_relevancy",
                           "precision": "contextual_precision", "recall": "contextual_recall"}
            for k, v in all_scores.items():
                if v:
                    update_rag_score(gauge_names[k], avgs[k])

            eq.put({"type": "summary", **avgs, "cost_usd": round(cost, 4)})

        except Exception as e:
            logger.error("DeepEval stream error: %s", e, exc_info=True)
            eq.put({"type": "error", "msg": str(e)[:200]})

    return _sse_stream(_worker)


# ---------------------------------------------------------------------------
# Web Push — suscripción PWA
# ---------------------------------------------------------------------------

@api_bp.route('/api/push/vapid-public-key', methods=['GET'])
def push_vapid_key():
    """Devuelve la clave pública VAPID para que el frontend pueda suscribirse."""
    key = os.environ.get('VAPID_PUBLIC_KEY', '')
    return jsonify({"publicKey": key})


@api_bp.route('/api/push/subscribe', methods=['POST'])
@_login_required
def push_subscribe():
    """Guarda la PushSubscription del navegador en el perfil del usuario."""
    subscription = request.get_json()
    if not subscription or 'endpoint' not in subscription:
        return jsonify({"error": "Invalid subscription object"}), 400

    user = BusinessProfile.query.filter_by(user_phone=session['user_phone']).first()
    if not user:
        return jsonify({"error": "User not found"}), 404

    user.push_subscription = json.dumps(subscription)
    db.session.commit()

    logger.info("Push subscription guardada para %s", session['user_phone'])
    return jsonify({"success": True})


@api_bp.route('/api/push/unsubscribe', methods=['POST'])
@_login_required
def push_unsubscribe():
    """Elimina la suscripción push del usuario."""
    user = BusinessProfile.query.filter_by(user_phone=session['user_phone']).first()
    if user:
        user.push_subscription = None
        db.session.commit()

    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# LLM Metrics
# ---------------------------------------------------------------------------

@api_bp.route('/api/metrics/llm', methods=['GET'])
@limiter.limit("30 per hour")
@_login_required
def llm_metrics():
    """
    Devuelve métricas agregadas de llamadas LLM para el usuario en sesión.
    Si es admin (ADMIN_EMAIL) devuelve datos globales.
    """
    from sqlalchemy import func as sqlfunc
    from datetime import timedelta
    from core.auth import is_admin as _is_admin

    is_admin = _is_admin()
    base_q = LLMCall.query if is_admin else LLMCall.query.filter_by(user_phone=session['user_phone'])

    # ── Hero stats ────────────────────────────────────────────────────────────
    totals = base_q.with_entities(
        sqlfunc.count(LLMCall.id),
        sqlfunc.coalesce(sqlfunc.sum(LLMCall.total_tokens), 0),
        sqlfunc.coalesce(sqlfunc.sum(LLMCall.cost_usd), 0.0),
        sqlfunc.coalesce(sqlfunc.avg(LLMCall.latency_ms), 0),
        sqlfunc.sum(sqlfunc.cast(LLMCall.success == False, db.Integer)),
    ).one()
    total_calls, total_tokens, total_cost, avg_latency, failed_calls = totals
    success_rate = round((1 - (failed_calls or 0) / max(total_calls, 1)) * 100, 1)

    # ── Por modelo ────────────────────────────────────────────────────────────
    by_model_rows = base_q.with_entities(
        LLMCall.model,
        sqlfunc.count(LLMCall.id),
        sqlfunc.coalesce(sqlfunc.sum(LLMCall.total_tokens), 0),
        sqlfunc.coalesce(sqlfunc.sum(LLMCall.cost_usd), 0.0),
        sqlfunc.coalesce(sqlfunc.avg(LLMCall.latency_ms), 0),
    ).group_by(LLMCall.model).all()

    by_model = [
        {"model": r[0], "calls": r[1], "tokens": r[2],
         "cost_usd": round(r[3], 5), "avg_latency_ms": round(r[4])}
        for r in by_model_rows
    ]

    # ── Por stage ─────────────────────────────────────────────────────────────
    by_stage_rows = base_q.with_entities(
        LLMCall.stage,
        sqlfunc.count(LLMCall.id),
        sqlfunc.coalesce(sqlfunc.avg(LLMCall.latency_ms), 0),
        sqlfunc.coalesce(sqlfunc.sum(LLMCall.cost_usd), 0.0),
    ).group_by(LLMCall.stage).order_by(sqlfunc.count(LLMCall.id).desc()).all()

    by_stage = [
        {"stage": r[0], "calls": r[1],
         "avg_latency_ms": round(r[2]), "cost_usd": round(r[3], 5)}
        for r in by_stage_rows
    ]

    # ── Llamadas por día (últimos 14 días) ───────────────────────────────────
    since = datetime.now(timezone.utc) - timedelta(days=14)
    daily_rows = base_q.filter(LLMCall.created_at >= since).with_entities(
        sqlfunc.date(LLMCall.created_at),
        sqlfunc.count(LLMCall.id),
        sqlfunc.coalesce(sqlfunc.sum(LLMCall.cost_usd), 0.0),
    ).group_by(sqlfunc.date(LLMCall.created_at)).order_by(sqlfunc.date(LLMCall.created_at)).all()

    daily = [
        {"date": str(r[0]), "calls": r[1], "cost_usd": round(r[2], 5)}
        for r in daily_rows
    ]

    return jsonify({
        "is_admin": is_admin,
        "total_calls": total_calls,
        "total_tokens": int(total_tokens),
        "total_cost_usd": round(float(total_cost), 4),
        "avg_latency_ms": round(float(avg_latency)),
        "success_rate": success_rate,
        "by_model": by_model,
        "by_stage": by_stage,
        "daily": daily,
    })


def _prometheus_ips() -> set:
    """IPs del contenedor de Prometheus, resueltas por el DNS interno de Docker.

    Se resuelve en cada scrape (cada 15 s) en lugar de cachear, para seguir
    funcionando si el contenedor se recrea con otra IP.
    """
    import socket
    host = os.environ.get('PROMETHEUS_SCRAPER_HOST', 'prometheus')
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return set()


@api_bp.route('/api/prometheus')
@limiter.exempt
def prometheus_metrics():
    """
    Endpoint de scraping para Prometheus.
    Acceso permitido a: admin autenticado, el contenedor de Prometheus y
    peticiones desde dentro del propio contenedor (loopback).

    Antes se aceptaba cualquier IP 172.x/10.x, pero las peticiones que entran por
    el puerto publicado de Docker llegan con la IP de la pasarela de la red
    (172.x.0.1), asi que cualquiera con acceso al puerto 5000 leia las metricas.
    remote_addr es el extremo TCP real (no hay proxy delante), no se puede falsificar.
    """
    from core.auth import is_admin
    remote = request.remote_addr or ''
    allowed = remote in ('127.0.0.1', '::1') or remote in _prometheus_ips()
    if not is_admin() and not allowed:
        return jsonify({"error": "Forbidden"}), 403

    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}


# ---------------------------------------------------------------------------
# Ciclo de vida de facturas
# ---------------------------------------------------------------------------

_VALID_STATUS_TRANSITIONS = {
    'draft': {'sent', 'paid'},
    'sent':  {'paid'},
    'paid':  set(),
}

@api_bp.route('/api/documents/<int:doc_id>/to-invoice', methods=['POST'])
@limiter.limit("20 per hour")
@_login_required
def convert_to_invoice(doc_id):
    """Convierte un presupuesto (proposal) en factura numerada."""
    user_phone = session['user_phone']
    doc = db.session.get(GeneratedDocument, doc_id)
    if not doc:
        return jsonify({"error": "Documento no encontrado"}), 404
    if doc.user_phone != user_phone:
        return jsonify({"error": "Forbidden"}), 403
    if doc.doc_type != 'proposal':
        return jsonify({"error": "Solo se pueden convertir presupuestos"}), 400
    if not doc.doc_data:
        return jsonify({"error": "Este presupuesto no tiene datos estructurados para generar factura"}), 400

    body = request.get_json(silent=True) or {}
    client_nif = (body.get('client_nif') or '').strip()[:20]

    year = datetime.now().year
    invoice_number = GeneratedDocument.next_invoice_number(user_phone, year)

    profile = BusinessProfile.query.filter_by(user_phone=user_phone).first()
    user_context = profile.to_agent_context() if profile else {
        "business_name": "", "phone": user_phone, "email": "", "sector": "Servicios", "extra_info": {}
    }

    invoice_data = dict(doc.doc_data)
    invoice_data['invoice_number'] = invoice_number
    if client_nif:
        invoice_data['client_nif'] = client_nif

    from modules.proactive.admin_redactor import AdminAssistantAgent
    file_path, subtotal, vat_amount, total_amount = AdminAssistantAgent().generate_invoice_pdf(
        invoice_data, user_context
    )
    if not file_path:
        return jsonify({"error": "Error generando el PDF de factura"}), 500

    try:
        new_inv = GeneratedDocument(
            user_phone=user_phone,
            file_path=file_path,
            doc_type='invoice',
            client_name=doc.doc_data.get('client_name') or doc.client_name,
            invoice_number=invoice_number,
            client_nif=client_nif or None,
            subtotal=subtotal,
            vat_amount=vat_amount,
            total_amount=total_amount,
            invoice_status='draft',
            doc_data=doc.doc_data,
        )
        db.session.add(new_inv)
        db.session.commit()
        from core.db_models import ActivityLog
        ActivityLog.log(user_phone, "Facturación", f"Factura generada: {invoice_number}")
        return jsonify({
            "invoice_id":     new_inv.id,
            "invoice_number": invoice_number,
            "file_path":      file_path,
        })
    except Exception as e:
        db.session.rollback()
        logger.error("Error guardando factura: %s", e)
        return jsonify({"error": "Error guardando la factura"}), 500


@api_bp.route('/api/invoices/<int:doc_id>/status', methods=['PATCH'])
@limiter.limit("60 per hour")
@_login_required
def update_invoice_status(doc_id):
    """Actualiza el estado de una factura: draft → sent → paid."""
    user_phone = session['user_phone']
    doc = db.session.get(GeneratedDocument, doc_id)
    if not doc:
        return jsonify({"error": "Factura no encontrada"}), 404
    if doc.user_phone != user_phone:
        return jsonify({"error": "Forbidden"}), 403
    if doc.doc_type != 'invoice':
        return jsonify({"error": "No es una factura"}), 400

    body = request.get_json(silent=True) or {}
    new_status = (body.get('status') or '').strip()
    current = doc.invoice_status or 'draft'
    allowed = _VALID_STATUS_TRANSITIONS.get(current, set())

    if new_status not in allowed:
        return jsonify({"error": f"Transición no válida: {current} → {new_status}"}), 400

    from core.db_models import ActivityLog
    try:
        doc.invoice_status = new_status
        db.session.commit()
        ActivityLog.log(user_phone, "Facturación", f"{doc.invoice_number}: estado → {new_status}")
        return jsonify({"success": True, "status": new_status})
    except Exception as e:
        db.session.rollback()
        logger.error("Error actualizando estado factura: %s", e)
        return jsonify({"error": "Error actualizando estado"}), 500


# ---------------------------------------------------------------------------
# Notificaciones
# ---------------------------------------------------------------------------

@api_bp.route('/api/notifications')
@limiter.limit("120 per minute", key_func=_chat_rate_key)
def get_notifications():
    if 'user_phone' not in session:
        return jsonify([]), 401
    notifs = (Notification.query
              .filter_by(user_phone=session['user_phone'])
              .order_by(Notification.created_at.desc())
              .limit(20).all())
    return jsonify([{
        "id": n.id,
        "title": n.title,
        "message": n.message,
        "type": n.type,
        "link": n.link,
        "is_read": n.is_read,
        "date": _to_local(n.created_at).strftime('%d/%m %H:%M'),
    } for n in notifs])


@api_bp.route('/api/notifications/mark_read/<int:notif_id>', methods=['POST'])
@limiter.limit("60 per minute", key_func=_chat_rate_key)
def mark_notification_read(notif_id):
    if 'user_phone' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    n = db.session.get(Notification, notif_id)
    if n and n.user_phone == session['user_phone']:
        n.is_read = True
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404


@api_bp.route('/api/notifications/mark_all_read', methods=['POST'])
@limiter.limit("30 per minute", key_func=_chat_rate_key)
def mark_all_notifications_read():
    if 'user_phone' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    Notification.query.filter_by(
        user_phone=session['user_phone'], is_read=False
    ).update({Notification.is_read: True})
    db.session.commit()
    return jsonify({"success": True})


@api_bp.route('/api/notifications/unread_count')
@limiter.limit("120 per minute", key_func=_chat_rate_key)
def notifications_unread_count():
    if 'user_phone' not in session:
        return jsonify({"count": 0}), 401
    count = Notification.query.filter_by(
        user_phone=session['user_phone'], is_read=False
    ).count()
    return jsonify({"count": count})
