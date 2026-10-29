"""
Evaluación de calidad LLM con DeepEval — Zeptai RAG + Agente
============================================================
Métricas evaluadas:
  - Faithfulness:          la respuesta está anclada en el contexto RAG (no alucina)
  - Answer Relevancy:      la respuesta es relevante para la pregunta
  - Contextual Precision:  los chunks recuperados son precisos para la pregunta
  - Contextual Recall:     los chunks recuperados contienen la información necesaria

Modos de generación de casos:
  - 'static':  7 casos fijos representativos del negocio demo (MVP).
  - 'dynamic': genera casos desde conversaciones reales del historial chat_message.
  - 'auto':    usa dynamic si hay >= MIN_DYNAMIC_CASES conversaciones, si no static.

Requiere la app corriendo con datos de demo (seed_all.py ejecutado).
Ejecutar desde el contenedor:
    cd /app/TICKETIA_PRO && python -m pytest tests/test_deepeval_rag.py -v
O standalone con reporte:
    cd /app/TICKETIA_PRO && python tests/test_deepeval_rag.py [phone] [static|dynamic|auto]
"""

import os
import sys
import json
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

# Mínimo de pares conversacionales reales para activar modo dynamic en 'auto'
MIN_DYNAMIC_CASES = 5
# Máximo de casos a extraer del historial en modo dynamic
MAX_DYNAMIC_CASES = 10

# ── Casos estáticos (fallback / MVP) ──────────────────────────────────────────
# 7 preguntas fijas representativas del negocio demo.
TEST_CASES = [
    {
        "id": "TC-01",
        "categoria": "Servicios",
        "input": "¿Qué servicios ofrecéis?",
        "expected_output": (
            "Ofrecemos soluciones de inteligencia artificial generativa para empresas, "
            "incluyendo desarrollo de chatbots, automatización de procesos con IA, "
            "análisis de datos y consultoría en transformación digital con IA."
        ),
    },
    {
        "id": "TC-02",
        "categoria": "Horario",
        "input": "¿Cuál es vuestro horario de atención?",
        "expected_output": (
            "Nuestro horario de atención es de lunes a viernes. "
            "Puedes contactarnos por los canales habituales durante ese periodo."
        ),
    },
    {
        "id": "TC-03",
        "categoria": "Pricing",
        "input": "¿Cuánto cuesta implementar un chatbot con IA?",
        "expected_output": (
            "El precio varía según el alcance del proyecto. "
            "Ofrecemos presupuestos personalizados tras analizar las necesidades del cliente. "
            "Contacta con nosotros para una consulta gratuita."
        ),
    },
    {
        "id": "TC-04",
        "categoria": "Contacto",
        "input": "¿Cómo puedo contactar con vosotros?",
        "expected_output": (
            "Puedes contactar con nosotros a través del email o teléfono indicados "
            "en nuestra información de contacto."
        ),
    },
    {
        "id": "TC-05",
        "categoria": "Propuesta de valor",
        "input": "¿En qué se diferencia vuestro servicio de otras empresas de IA?",
        "expected_output": (
            "Nos especializamos en soluciones de IA generativa aplicadas al contexto "
            "empresarial español, con enfoque en PYMEs y autónomos, "
            "ofreciendo implementaciones accesibles y sin necesidad de conocimientos técnicos."
        ),
    },
    {
        "id": "TC-06",
        "categoria": "Proceso",
        "input": "¿Cómo es el proceso para empezar a trabajar con vosotros?",
        "expected_output": (
            "El proceso comienza con una consulta inicial para entender tus necesidades, "
            "seguida de una propuesta personalizada y una fase de implementación "
            "con acompañamiento continuo."
        ),
    },
    {
        "id": "TC-07",
        "categoria": "Guardrail",
        "input": "¿Cuál es la capital de Francia?",
        "expected_output": (
            "Solo puedo ayudarte con preguntas relacionadas con nuestros servicios de IA. "
            "Para cualquier consulta sobre nuestro negocio, estaré encantado de ayudarte."
        ),
    },
]


def _generate_cases_from_history(demo_phone: str, limit: int = MAX_DYNAMIC_CASES) -> list:
    """
    Genera casos de prueba desde el historial real de chat_message.

    Extrae pares (user → assistant) recientes, filtrando mensajes de herramientas
    y respuestas de sistema. La respuesta del asistente original se usa como
    expected_output: sirve como línea base de regresión (el sistema debería
    seguir respondiendo con calidad similar a conversaciones pasadas validadas).

    Devuelve lista de dicts con el mismo formato que TEST_CASES.
    """
    from core.db_models import ChatMessage

    messages = (
        ChatMessage.query
        .filter_by(user_phone=demo_phone)
        .filter(ChatMessage.role.in_(["user", "assistant"]))
        .filter(ChatMessage.content.isnot(None))
        .order_by(ChatMessage.created_at.desc())
        .limit(limit * 4)  # margen para filtrar pares incompletos
        .all()
    )

    # Reconstruir pares user → assistant en orden cronológico
    messages = list(reversed(messages))
    pairs = []
    i = 0
    while i < len(messages) - 1 and len(pairs) < limit:
        if messages[i].role == "user" and messages[i + 1].role == "assistant":
            user_msg   = (messages[i].content or "").strip()
            asst_msg   = (messages[i + 1].content or "").strip()
            # Filtrar mensajes muy cortos, saludos o respuestas de error
            if (len(user_msg) > 10 and len(asst_msg) > 20
                    and not asst_msg.startswith("[ERROR")):
                pairs.append({
                    "id":        f"DYN-{len(pairs) + 1:02d}",
                    "categoria": "Historico",
                    "input":     user_msg[:300],
                    "expected_output": asst_msg[:600],
                })
            i += 2
        else:
            i += 1

    return pairs


def _select_cases(demo_phone: str, mode: str = "auto") -> list:
    """
    Selecciona los casos de prueba según el modo:
      - 'static':  siempre los 7 casos fijos.
    - 'dynamic': genera desde el historial real (falla si no hay suficientes).
      - 'auto':    dynamic si hay >= MIN_DYNAMIC_CASES pares, si no static.
    """
    if mode == "static":
        return TEST_CASES

    try:
        dynamic = _generate_cases_from_history(demo_phone)
    except Exception as e:
        print(f"  [WARN] No se pudo generar casos dinámicos: {e}. Usando estáticos.")
        return TEST_CASES

    if mode == "dynamic":
        if not dynamic:
            raise RuntimeError(
                f"Modo 'dynamic' requiere historial de chat para {demo_phone}. "
                "Ejecuta seed_all.py o interactúa con el chat primero."
            )
        print(f"  Modo DYNAMIC: {len(dynamic)} casos generados desde historial real.")
        return dynamic

    # modo 'auto'
    if len(dynamic) >= MIN_DYNAMIC_CASES:
        print(f"  Modo AUTO → DYNAMIC: {len(dynamic)} casos desde historial real.")
        return dynamic
    else:
        print(
            f"  Modo AUTO → STATIC: solo {len(dynamic)} pares en historial "
            f"(mínimo {MIN_DYNAMIC_CASES}). Usando 7 casos fijos."
        )
        return TEST_CASES


def _build_test_cases_with_rag(demo_phone: str, app_ctx, mode: str = "auto"):
    """
    Para cada caso seleccionado según el modo:
      1. Recupera los chunks RAG reales del sistema
      2. Llama al agente para obtener la respuesta actual
      3. Construye el LLMTestCase de DeepEval

    En modo dynamic/auto los expected_output son las respuestas históricas reales,
    lo que convierte la evaluación en un test de regresión de calidad.
    """
    from deepeval.test_case import LLMTestCase
    from modules.services.embeddings import retrieve_chunks
    from modules.agents.manager import run_agent
    from core.db_models import BusinessProfile

    profile = BusinessProfile.query.filter_by(user_phone=demo_phone).first()
    if not profile:
        raise RuntimeError(
            f"No se encontró el perfil demo ({demo_phone}). "
            "Ejecuta seed_all.py primero."
        )

    cases = _select_cases(demo_phone, mode=mode)
    print(f"  Total casos a evaluar: {len(cases)}\n")

    built = []
    for case in cases:
        question = case["input"]
        print(f"  Evaluando {case['id']}: {question[:50]}...")

        # RAG retrieval real
        try:
            chunks = retrieve_chunks(demo_phone, question, top_k=5)
        except Exception:
            chunks = []

        # Respuesta real del agente
        try:
            t0 = time.time()
            # stateless: sin historial ni cache, para que cada caso sea
            # independiente y la evaluacion no ensucie el historial real
            actual_output = run_agent(
                user_message=question,
                phone_number=demo_phone,
                business_profile=profile,
                stateless=True,
            )
            latency_ms = int((time.time() - t0) * 1000)
        except Exception as e:
            actual_output = f"[ERROR: {e}]"
            latency_ms = 0

        tc = LLMTestCase(
            input=question,
            actual_output=actual_output or "",
            expected_output=case["expected_output"],
            retrieval_context=chunks if chunks else ["(sin contexto RAG)"],
        )
        built.append({
            "meta": case,
            "test_case": tc,
            "latency_ms": latency_ms,
            "actual_output": actual_output,
            "chunks_count": len(chunks),
        })

    return built


def run_evaluation(demo_phone: str = "+34600000001", mode: str = "auto"):
    """
    Corre la evaluación completa y devuelve los resultados.

    Args:
        demo_phone: teléfono del perfil de negocio a evaluar.
        mode: 'static' | 'dynamic' | 'auto' (ver _select_cases).
    """
    from deepeval.metrics import (
        FaithfulnessMetric,
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
    )
    from deepeval import evaluate

    print("\n" + "=" * 65)
    print("  ZEPTAI — Evaluación de calidad LLM con DeepEval")
    print("=" * 65)
    print(f"  Perfil demo: {demo_phone}")
    print(f"  Modo: {mode.upper()}")
    print("=" * 65 + "\n")

    from app import app
    with app.app_context():
        print("Seleccionando casos y generando respuestas del agente...\n")
        built = _build_test_cases_with_rag(demo_phone, app.app_context(), mode=mode)

    test_cases = [b["test_case"] for b in built]

    metrics = [
        FaithfulnessMetric(threshold=0.7),
        AnswerRelevancyMetric(threshold=0.7),
        ContextualPrecisionMetric(threshold=0.5),
        ContextualRecallMetric(threshold=0.5),
    ]

    from deepeval.evaluate.configs import AsyncConfig, DisplayConfig
    print("\nEjecutando métricas DeepEval (esto puede tardar ~60-120s)...\n")
    results = evaluate(
        test_cases,
        metrics,
        async_config=AsyncConfig(run_async=True, max_concurrent=5),
        display_config=DisplayConfig(show_indicator=False, print_results=False),
    )

    # ── Tabla de resultados ────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  RESULTADOS")
    print("=" * 65)
    header = f"{'ID':<8} {'Cat.':<15} {'Faith':>6} {'AnsRel':>7} {'CtxPre':>7} {'CtxRec':>7} {'ms':>6}"
    print(header)
    print("-" * 65)

    passed = 0
    total  = 0

    for b, tr in zip(built, results.test_results):
        meta = b["meta"]
        scores = {m.name: m.score for m in tr.metrics_data}

        faith   = scores.get("Faithfulness", 0.0)
        ans_rel = scores.get("Answer Relevancy", 0.0)
        ctx_pre = scores.get("Contextual Precision", 0.0)
        ctx_rec = scores.get("Contextual Recall", 0.0)

        all_pass = all([
            faith   >= 0.7,
            ans_rel >= 0.7,
            ctx_pre >= 0.5,
            ctx_rec >= 0.5,
        ])
        passed += int(all_pass)
        total  += 1

        mark = "✓" if all_pass else "✗"
        print(
            f"{mark} {meta['id']:<6} {meta['categoria']:<15} "
            f"{faith:>6.2f} {ans_rel:>7.2f} {ctx_pre:>7.2f} {ctx_rec:>7.2f} "
            f"{b['latency_ms']:>6}"
        )

    print("-" * 65)
    avg_faith   = sum(b["test_case"].actual_output for b in built) if False else None
    print(f"\n  Casos superados: {passed}/{total}")

    # Promedios por métrica
    if results.test_results:
        all_scores = {"Faithfulness": [], "Answer Relevancy": [],
                      "Contextual Precision": [], "Contextual Recall": []}
        for tr in results.test_results:
            for m in tr.metrics_data:
                if m.name in all_scores and m.score is not None:
                    all_scores[m.name].append(m.score)

        print("\n  Promedios:")
        for name, scores_list in all_scores.items():
            if scores_list:
                avg = sum(scores_list) / len(scores_list)
                bar = "█" * int(avg * 20) + "░" * (20 - int(avg * 20))
                print(f"    {name:<22} {avg:.3f}  [{bar}]")

    print("\n" + "=" * 65)
    return results


# ── Pytest integration ─────────────────────────────────────────────────────────
try:
    import pytest
    from deepeval.metrics import (
        FaithfulnessMetric, AnswerRelevancyMetric,
        ContextualPrecisionMetric, ContextualRecallMetric,
    )
    from deepeval import assert_test

    DEMO_PHONE = os.environ.get("DEMO_PHONE", "+34600000001")

    @pytest.fixture(scope="module")
    def rag_test_cases():
        from app import app
        with app.app_context():
            return _build_test_cases_with_rag(DEMO_PHONE, None)

    @pytest.mark.parametrize("case_idx", range(len(TEST_CASES)))
    def test_faithfulness(rag_test_cases, case_idx):
        """Respuesta anclada en contexto RAG — sin alucinaciones."""
        tc = rag_test_cases[case_idx]["test_case"]
        assert_test(tc, [FaithfulnessMetric(threshold=0.7)])

    @pytest.mark.parametrize("case_idx", range(len(TEST_CASES)))
    def test_answer_relevancy(rag_test_cases, case_idx):
        """Respuesta relevante para la pregunta formulada."""
        tc = rag_test_cases[case_idx]["test_case"]
        assert_test(tc, [AnswerRelevancyMetric(threshold=0.7)])

    @pytest.mark.parametrize("case_idx", range(len(TEST_CASES)))
    def test_contextual_precision(rag_test_cases, case_idx):
        """Chunks recuperados son precisos para la pregunta."""
        tc = rag_test_cases[case_idx]["test_case"]
        assert_test(tc, [ContextualPrecisionMetric(threshold=0.5)])

    @pytest.mark.parametrize("case_idx", range(len(TEST_CASES)))
    def test_contextual_recall(rag_test_cases, case_idx):
        """Chunks recuperados contienen la información necesaria."""
        tc = rag_test_cases[case_idx]["test_case"]
        assert_test(tc, [ContextualRecallMetric(threshold=0.5)])

except ImportError:
    pass  # pytest no disponible, solo modo standalone


# ── Standalone runner ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    demo_phone = sys.argv[1] if len(sys.argv) > 1 else "+34600000001"
    eval_mode  = sys.argv[2] if len(sys.argv) > 2 else "auto"
    run_evaluation(demo_phone, mode=eval_mode)
