"""
Tests de observabilidad — stack OpenTelemetry + Prometheus + Grafana + Jaeger
=============================================================================

Grupos:
  1. Salud de servicios externos (Prometheus, Grafana, Jaeger)
  2. Scraping de Prometheus → target zeptai en estado UP
  3. Endpoint /api/prometheus: autorización y formato
  4. Métricas Prometheus: contadores LLM se incrementan correctamente
  5. Gauge RAG: update_rag_score refleja valores en Prometheus
  6. Rutas de observabilidad: protección y acceso admin

Requiere: docker-compose up (todos los servicios corriendo)
Ejecutar:  cd /app/TICKETIA_PRO && python -m pytest tests/test_observability.py -v
"""

import os
import sys
import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL_INTERNAL", "http://prometheus:9090")
GRAFANA_URL    = os.environ.get("GRAFANA_URL_INTERNAL",    "http://grafana:3000")
JAEGER_URL     = os.environ.get("JAEGER_URL_INTERNAL",     "http://jaeger:16686")

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def flask_app():
    from app import app, db
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        yield app


@pytest.fixture(scope="module")
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture(scope="module")
def admin_client(flask_app):
    """Cliente Flask con sesión de admin inyectada directamente."""
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_phone']     = '+34600000001'
        # Mismo email que la app considera admin (ADMIN_EMAIL, ver core/auth.py)
        sess['user_email']     = os.environ.get('ADMIN_EMAIL', 'admin@zeptai.com')
        sess['business_name']  = 'Zeptai Admin'
    return c


# ── Grupo 1: Salud de servicios externos ─────────────────────────────────────

class TestServiceHealth:

    def test_prometheus_healthy(self):
        r = requests.get(f"{PROMETHEUS_URL}/-/healthy", timeout=5)
        assert r.status_code == 200
        assert "Healthy" in r.text or "healthy" in r.text.lower()

    def test_grafana_healthy(self):
        r = requests.get(f"{GRAFANA_URL}/api/health", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data.get("database") == "ok"

    def test_jaeger_reachable(self):
        r = requests.get(JAEGER_URL, timeout=5)
        assert r.status_code == 200

    def test_prometheus_graph_ui(self):
        r = requests.get(f"{PROMETHEUS_URL}/graph", timeout=5)
        assert r.status_code == 200


# ── Grupo 2: Scraping Prometheus → target zeptai ───────────────────────────

class TestPrometheusTarget:

    def test_zeptai_target_exists(self):
        r = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=5)
        assert r.status_code == 200
        targets = r.json()["data"]["activeTargets"]
        jobs = [t["labels"]["job"] for t in targets]
        assert "zeptai" in jobs, f"Target 'zeptai' no encontrado. Jobs: {jobs}"

    def test_zeptai_target_is_up(self):
        import time
        zeptai = None
        for _ in range(6):  # hasta 60s (6 × 10s ≥ 2 ciclos de scrape)
            r = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=5)
            targets = r.json()["data"]["activeTargets"]
            zeptai = next((t for t in targets if t["labels"]["job"] == "zeptai"), None)
            if zeptai and zeptai["health"] == "up":
                break
            time.sleep(10)
        assert zeptai is not None, "Target 'zeptai' no encontrado"
        assert zeptai["health"] == "up", (
            f"Target zeptai health={zeptai['health']}, "
            f"error: {zeptai.get('lastError', 'none')}"
        )

    def test_zeptai_scrape_url_correct(self):
        r = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=5)
        targets = r.json()["data"]["activeTargets"]
        zeptai = next(t for t in targets if t["labels"]["job"] == "zeptai")
        assert "/api/prometheus" in zeptai["scrapeUrl"]

    def test_zeptai_app_info_metric_present(self):
        import time
        result = []
        for _ in range(6):
            r = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": "zeptai_app_info"},
                timeout=5,
            )
            result = r.json()["data"]["result"]
            if result:
                break
            time.sleep(10)
        assert len(result) > 0, "zeptai_app_info no encontrada tras 60s de espera"

    def test_all_expected_metric_families_present(self):
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/label/__name__/values",
            timeout=5,
        )
        names = set(r.json()["data"])
        expected = {
            "zeptai_app_info",
            "zeptai_llm_requests_total",
            "zeptai_llm_tokens_total",
            "zeptai_llm_cost_usd_total",
            "zeptai_llm_latency_seconds_bucket",
        }
        missing = expected - names
        assert not missing, f"Métricas no encontradas en Prometheus: {missing}"


# ── Grupo 3: Endpoint /api/prometheus ────────────────────────────────────────

class TestPrometheusEndpoint:

    def test_endpoint_returns_prometheus_format(self, admin_client):
        r = admin_client.get("/api/prometheus")
        assert r.status_code == 200
        assert b"zeptai_app_info" in r.data
        content_type = r.headers.get("Content-Type", "")
        assert "text/plain" in content_type

    def test_endpoint_contains_llm_metrics_family(self, admin_client):
        r = admin_client.get("/api/prometheus")
        assert r.status_code == 200
        assert b"zeptai_llm_requests_total" in r.data
        assert b"zeptai_llm_cost_usd_total" in r.data
        assert b"zeptai_llm_latency_seconds" in r.data

    def test_endpoint_blocked_for_anonymous(self, flask_app):
        """Sin sesión y con IP externa → 403."""
        with flask_app.test_client() as anon:
            r = anon.get(
                "/api/prometheus",
                environ_base={"REMOTE_ADDR": "8.8.8.8"},
            )
            assert r.status_code == 403

    def test_endpoint_contains_app_info_labels(self, admin_client):
        r = admin_client.get("/api/prometheus")
        assert b'service="zeptai"' in r.data


# ── Grupo 4: Contadores LLM se incrementan ───────────────────────────────────

class TestLLMMetricsIncrement:

    def test_record_llm_increments_request_counter(self, flask_app):
        from core.telemetry import llm_requests, record_llm
        with flask_app.app_context():
            before = llm_requests.labels(
                model="gpt-test", stage="test_stage", success="true"
            )._value.get()
            record_llm(
                model="gpt-test",
                stage="test_stage",
                success=True,
                prompt_tokens=100,
                completion_tokens=50,
                latency_ms=500,
                cost_usd=0.001,
            )
            after = llm_requests.labels(
                model="gpt-test", stage="test_stage", success="true"
            )._value.get()
        assert after == before + 1

    def test_record_llm_increments_token_counters(self, flask_app):
        from core.telemetry import llm_tokens, record_llm
        with flask_app.app_context():
            before_prompt = llm_tokens.labels(
                model="gpt-test2", stage="token_stage", token_type="prompt"
            )._value.get()
            record_llm(
                model="gpt-test2",
                stage="token_stage",
                success=True,
                prompt_tokens=200,
                completion_tokens=100,
                latency_ms=300,
                cost_usd=0.002,
            )
            after_prompt = llm_tokens.labels(
                model="gpt-test2", stage="token_stage", token_type="prompt"
            )._value.get()
        assert after_prompt == before_prompt + 200

    def test_record_llm_increments_cost_counter(self, flask_app):
        from core.telemetry import llm_cost, record_llm
        with flask_app.app_context():
            before = llm_cost.labels(
                model="gpt-test3", stage="cost_stage"
            )._value.get()
            record_llm(
                model="gpt-test3",
                stage="cost_stage",
                success=True,
                prompt_tokens=50,
                completion_tokens=25,
                latency_ms=200,
                cost_usd=0.005,
            )
            after = llm_cost.labels(
                model="gpt-test3", stage="cost_stage"
            )._value.get()
        assert abs(after - (before + 0.005)) < 1e-9

    def test_failed_call_tracked_with_false_label(self, flask_app):
        from core.telemetry import llm_requests, record_llm
        with flask_app.app_context():
            before = llm_requests.labels(
                model="gpt-fail", stage="fail_stage", success="false"
            )._value.get()
            record_llm(
                model="gpt-fail",
                stage="fail_stage",
                success=False,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=100,
                cost_usd=0.0,
            )
            after = llm_requests.labels(
                model="gpt-fail", stage="fail_stage", success="false"
            )._value.get()
        assert after == before + 1

    def test_record_llm_observes_latency_histogram(self, flask_app):
        from core.telemetry import llm_latency, record_llm
        with flask_app.app_context():
            before_count = llm_latency.labels(
                model="gpt-lat", stage="lat_stage"
            )._sum.get()
            record_llm(
                model="gpt-lat",
                stage="lat_stage",
                success=True,
                prompt_tokens=10,
                completion_tokens=10,
                latency_ms=2000,
                cost_usd=0.0,
            )
            after_count = llm_latency.labels(
                model="gpt-lat", stage="lat_stage"
            )._sum.get()
        # 2000ms = 2.0s deben sumarse
        assert abs(after_count - (before_count + 2.0)) < 0.01

    def test_llm_tracker_calls_record_llm(self, flask_app):
        """Verifica integración: llm_tracker.track() actualiza Prometheus."""
        from unittest.mock import MagicMock, patch
        from core.telemetry import llm_requests

        with flask_app.app_context():
            before = llm_requests.labels(
                model="gpt-4o-mini", stage="safety_classifier", success="true"
            )._value.get()

            mock_response = MagicMock()
            mock_response.usage.prompt_tokens = 50
            mock_response.usage.completion_tokens = 20
            mock_response.usage.total_tokens = 70

            from core.llm_tracker import track
            track(
                user_phone="+34000000000",
                model="gpt-4o-mini",
                stage="safety_classifier",
                response=mock_response,
                latency_ms=120,
                success=True,
            )
            after = llm_requests.labels(
                model="gpt-4o-mini", stage="safety_classifier", success="true"
            )._value.get()
        assert after == before + 1


# ── Grupo 5: Gauge RAG ───────────────────────────────────────────────────────

class TestRAGScoreGauge:

    def test_update_rag_score_sets_gauge(self, flask_app):
        from core.telemetry import rag_score, update_rag_score
        with flask_app.app_context():
            update_rag_score("faithfulness", 0.92)
            val = rag_score.labels(metric="faithfulness")._value.get()
        assert abs(val - 0.92) < 1e-9

    def test_update_rag_score_overwrites_previous(self, flask_app):
        from core.telemetry import rag_score, update_rag_score
        with flask_app.app_context():
            update_rag_score("answer_relevancy", 0.75)
            update_rag_score("answer_relevancy", 0.88)
            val = rag_score.labels(metric="answer_relevancy")._value.get()
        assert abs(val - 0.88) < 1e-9

    def test_multiple_rag_metrics_independent(self, flask_app):
        from core.telemetry import rag_score, update_rag_score
        with flask_app.app_context():
            update_rag_score("contextual_precision", 0.80)
            update_rag_score("contextual_recall", 0.70)
            p = rag_score.labels(metric="contextual_precision")._value.get()
            r = rag_score.labels(metric="contextual_recall")._value.get()
        assert abs(p - 0.80) < 1e-9
        assert abs(r - 0.70) < 1e-9


# ── Grupo 6: Rutas de observabilidad ─────────────────────────────────────────

class TestObservabilidadRoutes:

    def test_observabilidad_requires_login(self, client):
        with client.application.test_client() as anon:
            r = anon.get("/observabilidad", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_observabilidad_rejects_non_admin(self, flask_app):
        """Usuario no-admin con sesión válida → redirigido al dashboard."""
        with flask_app.test_client() as c:
            with c.session_transaction() as sess:
                sess['user_phone']    = '+34700000001'
                sess['user_email']    = 'user@example.com'
                sess['business_name'] = 'Usuario Normal'
            r = c.get("/observabilidad", follow_redirects=True)
        # Redirige al dashboard con flash de error
        assert r.status_code == 200
        assert b"dashboard" in r.request.path.encode() or b"Acceso" in r.data or b"Inicio" in r.data

    def test_observabilidad_accessible_for_admin(self, admin_client):
        r = admin_client.get("/observabilidad")
        assert r.status_code == 200
        assert b"Observabilidad" in r.data
        assert b"grafana" in r.data.lower()

    def test_observabilidad_contains_all_tabs(self, admin_client):
        r = admin_client.get("/observabilidad")
        assert b"Grafana" in r.data
        assert b"Jaeger" in r.data
        assert b"Prometheus" in r.data

    def test_metrics_page_accessible_for_admin(self, admin_client):
        r = admin_client.get("/metrics", follow_redirects=True)
        assert r.status_code == 200
        assert b"M\xc3\xa9tricas" in r.data or b"LLM" in r.data

    def test_metrics_page_requires_login(self, client):
        with client.application.test_client() as anon:
            r = anon.get("/metrics", follow_redirects=False)
        assert r.status_code == 302
