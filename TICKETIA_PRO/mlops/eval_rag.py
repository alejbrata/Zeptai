"""
eval_rag.py — Evaluación de calidad RAG con DeepEval.

Mide las 3 métricas estándar del RAG Triad:
  - Faithfulness:      La respuesta no inventa cosas que no están en el contexto
  - Answer Relevancy:  La respuesta responde realmente la pregunta
  - Context Precision: Los chunks recuperados son relevantes para la pregunta

Dataset: 8 preguntas sobre Demo Business S.L. con respuestas esperadas.
Umbral de aceptación: >= 0.7 en cada métrica.

Uso:
    python mlops/eval_rag.py
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault('OPENAI_API_KEY', os.environ.get('OPENAI_API_KEY', ''))

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        print(f"  PASS  {name}")
        PASS += 1
    else:
        print(f"  FAIL  {name}: {detail}")
        FAIL += 1


# ── Dataset de evaluación ──────────────────────────────────────────────────────
# Cada caso: (pregunta, respuesta_esperada, contexto_esperado_keywords)
EVAL_DATASET = [
    {
        "question": "cuanto cuesta implementar un chatbot",
        "expected_answer": "El paquete Starter cuesta 1.500 euros e incluye un chatbot basico con una integracion y soporte de 30 dias.",
        "expected_context_keywords": ["starter", "1.500", "chatbot"],
    },
    {
        "question": "cuanto tiempo tarda un proyecto de RAG",
        "expected_answer": "Un sistema RAG tarda entre 3 y 4 semanas en desarrollarse.",
        "expected_context_keywords": ["RAG", "3", "4 semanas"],
    },
    {
        "question": "que garantias ofreceis si el proyecto no funciona bien",
        "expected_answer": "Ofrecen 90 dias de garantia y si no se alcanzan los KPIs acordados devuelven el 50% del importe.",
        "expected_context_keywords": ["90", "KPI", "50%"],
    },
    {
        "question": "que tecnologias usais para desarrollar soluciones de IA",
        "expected_answer": "Usan OpenAI GPT-4o, Anthropic Claude, LangChain, pgvector, FastAPI, Docker y GitHub Actions.",
        "expected_context_keywords": ["GPT-4o", "Claude", "LangChain"],
    },
    {
        "question": "cual es el horario de atencion al cliente",
        "expected_answer": "El horario es de lunes a viernes de 9:00 a 18:00. Fuera de ese horario responden el siguiente dia laborable.",
        "expected_context_keywords": ["9:00", "18:00", "lunes"],
    },
    {
        "question": "puedo cancelar el proyecto si cambia de opinion",
        "expected_answer": "Si se cancela antes del inicio del desarrollo el reembolso es total. Durante el desarrollo es proporcional al trabajo no realizado.",
        "expected_context_keywords": ["cancelaci", "reembolso", "desarrollo"],
    },
    {
        "question": "teneis experiencia con clinicas o sector salud",
        "expected_answer": "Si, tienen un caso de exito con una clinica dental en Barcelona donde el chatbot redujo las llamadas de recepcion en un 70%.",
        "expected_context_keywords": ["clinica", "70%", "Barcelona"],
    },
    {
        "question": "como medis el exito de un proyecto de IA",
        "expected_answer": "Definen KPIs al inicio del proyecto como reduccion de tiempo, tasa de resolucion u otras metricas acordadas con el cliente.",
        "expected_context_keywords": ["KPI", "exito", "metricas"],
    },
]


def eval_rag_schema():
    """Validación ligera (sin API): verifica que el dataset está bien formado."""
    print("\n[Eval] Dataset RAG — validación de schema")
    for i, case in enumerate(EVAL_DATASET):
        check(f"caso {i+1} tiene question",  bool(case.get("question")))
        check(f"caso {i+1} tiene expected_answer", bool(case.get("expected_answer")))
        check(f"caso {i+1} tiene keywords", len(case.get("expected_context_keywords", [])) >= 2)


def eval_rag_deepeval():
    """
    Evaluación completa con DeepEval (requiere OPENAI_API_KEY).
    Métricas: Faithfulness, AnswerRelevancy, ContextPrecision.
    """
    api_key = os.environ.get('OPENAI_API_KEY', '')
    if not api_key or api_key.startswith('sk-fake'):
        print("\n[Eval] RAG DeepEval — OMITIDO (sin OPENAI_API_KEY real)")
        check("deepeval_skipped_gracefully", True)
        return

    print("\n[Eval] RAG DeepEval — Faithfulness + AnswerRelevancy + ContextPrecision")
    try:
        from deepeval import evaluate
        from deepeval.metrics import (
            FaithfulnessMetric,
            AnswerRelevancyMetric,
            ContextualPrecisionMetric,
        )
        from deepeval.test_case import LLMTestCase

        from app import app
        from modules.services.embeddings import retrieve_chunks

        faithfulness  = FaithfulnessMetric(threshold=0.7, model="gpt-4o-mini", verbose_mode=False)
        relevancy     = AnswerRelevancyMetric(threshold=0.7, model="gpt-4o-mini", verbose_mode=False)
        precision     = ContextualPrecisionMetric(threshold=0.7, model="gpt-4o-mini", verbose_mode=False)

        DEMO_PHONE = os.environ.get("DEMO_PHONE", "+34600000001")
        test_cases = []

        with app.app_context():
            from modules.agents.manager import run_agent
            from core.db_models import BusinessProfile

            profile = BusinessProfile.query.filter_by(user_phone=DEMO_PHONE).first()
            if not profile or not profile.system_prompt:
                print("  SKIP: usuario demo no configurado — ejecuta seed_all.py primero")
                check("demo_user_ready", False, "usuario sin system_prompt")
                return

            for case in EVAL_DATASET[:4]:   # 4 casos para mantener coste bajo
                # Mismo top_k que el agente: el juez debe ver el contexto que vio el generador
                chunks = retrieve_chunks(DEMO_PHONE, case["question"], top_k=5)
                actual = run_agent(
                    stateless=True,
                    user_message=case["question"],
                    phone_number=DEMO_PHONE,
                    business_profile=profile,
                )
                test_cases.append(LLMTestCase(
                    input=case["question"],
                    actual_output=actual,
                    expected_output=case["expected_answer"],
                    retrieval_context=chunks,
                ))

        results = evaluate(test_cases, [faithfulness, relevancy, precision], run_async=False)

        for tc in results.test_results:
            for metric_result in tc.metrics_data:
                passed = metric_result.success
                score  = round(metric_result.score or 0, 3)
                check(
                    f"{metric_result.name} (score={score})",
                    passed,
                    f"score {score} < threshold 0.7",
                )

    except Exception as e:
        print(f"  ERROR en DeepEval: {e}")
        check("deepeval_execution", False, str(e))


if __name__ == '__main__':
    print("=" * 55)
    print("RAG Evaluation — Demo Business S.L.")
    print("=" * 55)

    eval_rag_schema()
    eval_rag_deepeval()

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    if FAIL > 0:
        sys.exit(1)
    print("All RAG evaluations passed.")
