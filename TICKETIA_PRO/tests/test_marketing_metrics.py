"""
Tests para el pipeline de generación de vídeo (MarketingAgent) y el sistema de
métricas LLM (LLMTracker + endpoint /api/metrics/llm).

Cubre:
- LLMTracker: estimación de costes, persistencia en BD, context manager timed_track.
- MarketingAgent: pipeline de dos etapas (Stage 1 Vision + Stage 2 Cinematic),
  propagación de user_phone, registro de métricas por stage.
- Endpoint /generate_video_from_image: auth, validación, respuesta correcta.
- Endpoint /api/metrics/llm: auth, estructura JSON, agregación por modelo/stage.

Ejecución:
    cd TICKETIA_PRO
    pytest tests/test_marketing_metrics.py -v
"""

import os
import sys
import io
import json
import time
import unittest
from unittest.mock import patch, MagicMock, mock_open, call
from datetime import datetime, timedelta

os.environ.setdefault('OPENAI_API_KEY', 'sk-fake-key-for-testing')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-very-long-and-random')
os.environ.setdefault('MAIL_DEFAULT_SENDER', 'test@zeptai.com')
os.environ.setdefault('RUNWAY_API_KEY', 'rw-fake-key-for-testing')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db
from core.db_models import BusinessProfile, LLMCall
from core.llm_tracker import _estimate_cost, track


# ─────────────────────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────────────────────

class MetricsTestBase(unittest.TestCase):
    TEST_PHONE = '+34600000002'
    TEST_EMAIL = 'metrics@test.com'
    TEST_PASSWORD = 'Test1234!'

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        app.config['RATELIMIT_ENABLED'] = False
        self.client = app.test_client()

        with app.app_context():
            db.drop_all()
            db.create_all()
            from werkzeug.security import generate_password_hash
            user = BusinessProfile(
                user_phone=self.TEST_PHONE,
                email=self.TEST_EMAIL,
                password_hash=generate_password_hash(self.TEST_PASSWORD),
                business_name='Test Business',
                plan_tier='BASIC',
                features={},
            )
            db.session.add(user)
            db.session.commit()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def _inject_session(self):
        with self.client.session_transaction() as sess:
            sess['user_phone'] = self.TEST_PHONE
            sess['user_email'] = self.TEST_EMAIL
            sess['business_name'] = 'Test Business'

    @staticmethod
    def _openai_response(content: str, prompt_tokens: int = 100, completion_tokens: int = 50):
        """Construye un mock de respuesta OpenAI con usage real."""
        usage = MagicMock()
        usage.prompt_tokens = prompt_tokens
        usage.completion_tokens = completion_tokens
        usage.total_tokens = prompt_tokens + completion_tokens

        msg = MagicMock()
        msg.content = content
        msg.tool_calls = None

        choice = MagicMock()
        choice.message = msg

        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = usage
        return resp


# ─────────────────────────────────────────────────────────────────────────────
# 1. LLMTracker — estimación de costes
# ─────────────────────────────────────────────────────────────────────────────

class TestCostEstimation(unittest.TestCase):
    """Tests de la función _estimate_cost sin necesidad de BD."""

    def test_gpt4o_cost_calculated_correctly(self):
        """gpt-4o: 1 000 tokens entrada + 500 salida."""
        # 1000 * 2.50/1M + 500 * 10.00/1M = 0.0025 + 0.005 = 0.0075
        cost = _estimate_cost('gpt-4o', 1000, 500, {})
        self.assertAlmostEqual(cost, 0.0075, places=6)

    def test_gpt4o_mini_cheaper_than_gpt4o(self):
        cost_mini = _estimate_cost('gpt-4o-mini', 1000, 500, {})
        cost_full = _estimate_cost('gpt-4o', 1000, 500, {})
        self.assertLess(cost_mini, cost_full)

    def test_dalle3_flat_per_image(self):
        cost = _estimate_cost('dall-e-3', 0, 0, {})
        self.assertAlmostEqual(cost, 0.040, places=3)

    def test_runway_cost_per_second(self):
        """5 segundos de vídeo gen3a_turbo."""
        cost = _estimate_cost('gen3a_turbo', 0, 0, {'duration_s': 5})
        self.assertAlmostEqual(cost, 0.25, places=4)

    def test_runway_gen4_turbo_has_price(self):
        """El modelo de video en produccion debe tener tarifa: si falta, el panel
        de costes marca 0 USD justo en la llamada mas cara de la aplicacion."""
        cost = _estimate_cost('gen4_turbo', 0, 0, {'duration_s': 5})
        self.assertAlmostEqual(cost, 0.25, places=4)

    def test_unknown_model_returns_zero(self):
        cost = _estimate_cost('modelo-desconocido', 500, 300, {})
        self.assertEqual(cost, 0.0)

    def test_zero_tokens_zero_cost(self):
        cost = _estimate_cost('gpt-4o', 0, 0, {})
        self.assertEqual(cost, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. LLMTracker — persistencia en BD
# ─────────────────────────────────────────────────────────────────────────────

class TestLLMTrackerPersistence(MetricsTestBase):

    def test_track_creates_llmcall_record(self):
        """track() guarda un registro LLMCall en la BD."""
        with app.app_context():
            track(self.TEST_PHONE, 'gpt-4o', 'test_stage', latency_ms=800)
            record = LLMCall.query.filter_by(stage='test_stage').first()

        self.assertIsNotNone(record)
        self.assertEqual(record.model, 'gpt-4o')
        self.assertEqual(record.latency_ms, 800)
        self.assertEqual(record.user_phone, self.TEST_PHONE)
        self.assertTrue(record.success)

    def test_track_extracts_tokens_from_response(self):
        """track() extrae prompt/completion tokens del objeto de respuesta."""
        with app.app_context():
            resp = MetricsTestBase._openai_response('hola', prompt_tokens=200, completion_tokens=80)
            track(self.TEST_PHONE, 'gpt-4o', 'token_test', resp, latency_ms=300)
            record = LLMCall.query.filter_by(stage='token_test').first()

        self.assertEqual(record.prompt_tokens, 200)
        self.assertEqual(record.completion_tokens, 80)
        self.assertEqual(record.total_tokens, 280)
        self.assertGreater(record.cost_usd, 0)

    def test_track_records_failure(self):
        """track() registra llamadas fallidas con success=False y mensaje de error."""
        with app.app_context():
            track(self.TEST_PHONE, 'gpt-4o', 'failed_call',
                  latency_ms=100, success=False, error='Timeout')
            record = LLMCall.query.filter_by(stage='failed_call').first()

        self.assertFalse(record.success)
        self.assertEqual(record.error_message, 'Timeout')

    def test_track_with_none_response_records_zero_tokens(self):
        """track() sin respuesta guarda 0 tokens sin errores."""
        with app.app_context():
            track(self.TEST_PHONE, 'gen3a_turbo', 'runway_no_resp',
                  latency_ms=15000, extra={'duration_s': 5})
            record = LLMCall.query.filter_by(stage='runway_no_resp').first()

        self.assertEqual(record.prompt_tokens, 0)
        self.assertEqual(record.total_tokens, 0)
        self.assertAlmostEqual(record.cost_usd, 0.25, places=3)

    def test_track_system_call_without_user(self):
        """track() acepta user_phone=None para tareas del sistema."""
        with app.app_context():
            track(None, 'gpt-4o', 'system_task', latency_ms=500)
            record = LLMCall.query.filter_by(stage='system_task').first()

        self.assertIsNotNone(record)
        self.assertIsNone(record.user_phone)

    def test_timed_track_measures_latency(self):
        """timed_track() mide el tiempo transcurrido automáticamente."""
        from core.llm_tracker import timed_track

        with app.app_context():
            with timed_track(self.TEST_PHONE, 'gpt-4o', 'timed_stage') as t:
                time.sleep(0.05)  # 50ms mínimo
                t['response'] = MetricsTestBase._openai_response('ok', 10, 5)

            record = LLMCall.query.filter_by(stage='timed_stage').first()

        self.assertIsNotNone(record)
        self.assertGreaterEqual(record.latency_ms, 40)   # al menos 40ms
        self.assertEqual(record.total_tokens, 15)
        self.assertTrue(record.success)

    def test_timed_track_records_failure_on_exception(self):
        """timed_track() registra success=False si el bloque lanza excepción."""
        from core.llm_tracker import timed_track

        with app.app_context():
            with self.assertRaises(ValueError):
                with timed_track(self.TEST_PHONE, 'gpt-4o', 'timed_fail') as t:
                    raise ValueError('test error')

            record = LLMCall.query.filter_by(stage='timed_fail').first()

        self.assertIsNotNone(record)
        self.assertFalse(record.success)
        self.assertIn('test error', record.error_message)

    def test_multiple_stages_tracked_independently(self):
        """Varias llamadas con stages distintos generan registros independientes."""
        with app.app_context():
            track(self.TEST_PHONE, 'gpt-4o', 'stage_a', latency_ms=200)
            track(self.TEST_PHONE, 'gpt-4o', 'stage_b', latency_ms=400)
            track(self.TEST_PHONE, 'gen3a_turbo', 'stage_c', latency_ms=8000)

            self.assertEqual(LLMCall.query.filter_by(stage='stage_a').count(), 1)
            self.assertEqual(LLMCall.query.filter_by(stage='stage_b').count(), 1)
            self.assertEqual(LLMCall.query.filter_by(stage='stage_c').count(), 1)
            self.assertEqual(LLMCall.query.count(), 3)


# ─────────────────────────────────────────────────────────────────────────────
# 3. MarketingAgent — pipeline de dos etapas
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketingAgentPipeline(MetricsTestBase):
    """
    Tests del pipeline Stage 1 (Vision) → Stage 2 (Cinematic) → Runway.
    Se mockean: OpenAI, Runway, y la lectura del archivo de imagen.
    """

    # Bytes mínimos de un PNG 1x1 válido
    FAKE_PNG = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
        b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00'
        b'\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18'
        b'\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
    )

    def _make_agent_with_mocks(self, stage1_content='CATEGORY: clothing', stage2_content='Tracking shot of a model'):
        """Crea un MarketingAgent con OpenAI y Runway completamente mockeados."""
        mock_openai = MagicMock()
        mock_runway = MagicMock()

        # Stage 1: análisis de imagen
        mock_openai.chat.completions.create.side_effect = [
            MetricsTestBase._openai_response(stage1_content, 500, 150),
            MetricsTestBase._openai_response(stage2_content, 300, 80),
        ]

        # Runway: task creada y polling resuelve en SUCCEEDED
        mock_task = MagicMock()
        mock_task.id = 'task-fake-123'
        mock_runway.image_to_video.create.return_value = mock_task

        mock_task_status = MagicMock()
        mock_task_status.status = 'SUCCEEDED'
        mock_task_status.output = ['https://cdn.runway.com/fake_video.mp4']
        mock_runway.tasks.retrieve.return_value = mock_task_status

        with patch('core.clients.get_openai_client', return_value=mock_openai), \
             patch('core.clients.get_runway_client', return_value=mock_runway):
            from modules.proactive.marketing_agent import MarketingAgent
            agent = MarketingAgent()

        # Sustituir directamente para evitar re-instanciación
        agent.openai = mock_openai
        agent.runway_client = mock_runway
        return agent, mock_openai, mock_runway

    def test_stage1_and_stage2_are_called(self):
        """El pipeline llama a GPT-4o dos veces: Stage 1 (Vision) y Stage 2 (Cinematic)."""
        agent, mock_openai, _ = self._make_agent_with_mocks()

        with app.app_context(), \
             patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=self.FAKE_PNG)):
            agent._analyze_product_context('/fake/image.jpg', business_name='Tienda Test')

        self.assertEqual(mock_openai.chat.completions.create.call_count, 2)

    def test_stage1_uses_vision_model(self):
        """Stage 1 pasa el contenido de imagen (base64) al modelo."""
        agent, mock_openai, _ = self._make_agent_with_mocks()

        with app.app_context(), \
             patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=self.FAKE_PNG)):
            agent._analyze_product_context('/fake/shirt.jpg')

        first_call_args = mock_openai.chat.completions.create.call_args_list[0]
        messages = first_call_args[1].get('messages') or first_call_args[0][0]
        user_content = messages[1]['content']
        # El contenido del usuario debe incluir image_url con base64
        has_image = any(
            isinstance(c, dict) and c.get('type') == 'image_url'
            for c in user_content
        )
        self.assertTrue(has_image, "Stage 1 debe incluir image_url con base64")

    def test_stage2_receives_product_analysis(self):
        """Stage 2 recibe el análisis de Stage 1 como contexto."""
        analysis_text = 'CATEGORY: clothing\nCOLORS: white, blue\nNATURAL_CONTEXT: beach'
        agent, mock_openai, _ = self._make_agent_with_mocks(stage1_content=analysis_text)

        with app.app_context(), \
             patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=self.FAKE_PNG)):
            result = agent._analyze_product_context('/fake/shirt.jpg')

        # El resultado debe ser el contenido de Stage 2
        self.assertIn('Tracking shot', result)

        # Stage 2 debe haber recibido el análisis de Stage 1 en el mensaje
        second_call_args = mock_openai.chat.completions.create.call_args_list[1]
        user_msg = (second_call_args[1].get('messages') or second_call_args[0][0])[1]['content']
        self.assertIn(analysis_text, user_msg)

    def test_metrics_recorded_for_both_stages(self):
        """Cada stage del pipeline registra su propio LLMCall en BD."""
        agent, _, _ = self._make_agent_with_mocks()

        with app.app_context(), \
             patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=self.FAKE_PNG)):
            agent.user_phone = self.TEST_PHONE
            agent._analyze_product_context('/fake/image.jpg')

            stage1_record = LLMCall.query.filter_by(stage='video_analyze_image').first()
            stage2_record = LLMCall.query.filter_by(stage='video_generate_prompt').first()

        self.assertIsNotNone(stage1_record, "Debe existir registro para video_analyze_image")
        self.assertIsNotNone(stage2_record, "Debe existir registro para video_generate_prompt")
        self.assertEqual(stage1_record.model, 'gpt-4o')
        self.assertEqual(stage2_record.model, 'gpt-4o')

    def test_user_phone_stored_in_metrics(self):
        """user_phone se propaga correctamente a los registros LLMCall."""
        agent, _, _ = self._make_agent_with_mocks()

        with app.app_context(), \
             patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=self.FAKE_PNG)):
            agent.user_phone = self.TEST_PHONE
            agent._analyze_product_context('/fake/image.jpg')

            records = LLMCall.query.filter_by(user_phone=self.TEST_PHONE).all()

        self.assertGreaterEqual(len(records), 2)

    def test_missing_image_returns_fallback(self):
        """Si la imagen no existe, devuelve el prompt de fallback sin crash."""
        agent, mock_openai, _ = self._make_agent_with_mocks()

        with app.app_context(), \
             patch('os.path.exists', return_value=False):
            result = agent._analyze_product_context('/nonexistent/image.jpg')

        # No debe llamar a OpenAI si la imagen no existe
        mock_openai.chat.completions.create.assert_not_called()
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_runway_metric_recorded_on_success(self):
        """La generación de vídeo Runway registra un LLMCall con model=gen4_turbo."""
        agent, _, mock_runway = self._make_agent_with_mocks()

        with app.app_context(), \
             patch('os.path.exists', return_value=False), \
             patch('requests.get') as mock_req:
            mock_req.return_value.content = b'fake-video-bytes'
            agent.user_phone = self.TEST_PHONE
            agent._generate_runway_video('Tracking shot of a model on a beach')

            record = LLMCall.query.filter_by(stage='runway_video_generation').first()

        self.assertIsNotNone(record)
        self.assertEqual(record.model, 'gen4_turbo')
        self.assertTrue(record.success)

    def test_runway_metric_recorded_on_failure(self):
        """Si Runway falla, el LLMCall queda con success=False."""
        agent, _, mock_runway = self._make_agent_with_mocks()

        # Sobreescribir el status a FAILED
        mock_failed_status = MagicMock()
        mock_failed_status.status = 'FAILED'
        mock_failed_status.failure_reason = 'Content policy violation'
        mock_runway.tasks.retrieve.return_value = mock_failed_status

        with app.app_context(), \
             patch('os.path.exists', return_value=False):
            agent.user_phone = self.TEST_PHONE
            result = agent._generate_runway_video('Test prompt')

        self.assertIsNone(result)

        with app.app_context():
            record = LLMCall.query.filter_by(stage='runway_video_generation').first()
        self.assertIsNotNone(record)
        self.assertFalse(record.success)

    def test_generate_marketing_content_sets_user_phone(self):
        """generate_marketing_content almacena user_phone en self.user_phone."""
        agent, _, _ = self._make_agent_with_mocks()

        with patch.object(agent, '_analyze_product_context', return_value='fake prompt'), \
             patch.object(agent, '_generate_runway_video', return_value='http://fake.mp4'):
            agent.generate_marketing_content(
                '', content_type='video', business_name='Test',
                user_phone=self.TEST_PHONE
            )

        self.assertEqual(agent.user_phone, self.TEST_PHONE)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Endpoint /generate_video_from_image
# ─────────────────────────────────────────────────────────────────────────────

class TestVideoEndpoint(MetricsTestBase):

    def setUp(self):
        super().setUp()
        self._inject_session()
        # Reset in-memory rate limit counters so tests don't bleed into each other
        from core.limiter import limiter
        limiter.reset()

    def test_requires_authentication(self):
        """Sin sesión activa devuelve 401."""
        fresh_client = app.test_client()
        resp = fresh_client.post('/generate_video_from_image')
        self.assertEqual(resp.status_code, 401)

    def test_returns_400_without_image(self):
        """Sin adjuntar imagen devuelve 400."""
        resp = self.client.post('/generate_video_from_image')
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertFalse(data['success'])

    def test_returns_400_with_empty_filename(self):
        """Archivo con nombre vacío devuelve 400."""
        resp = self.client.post(
            '/generate_video_from_image',
            data={'image': (io.BytesIO(b'fake'), '')},
            content_type='multipart/form-data',
        )
        self.assertEqual(resp.status_code, 400)

    @patch('routes.api.run_marketing_thread')
    def test_successful_generation_returns_processing(self, mock_thread):
        """El endpoint async devuelve 200 con processing=True (Runway corre en background)."""
        mock_thread.return_value = None  # el thread se lanza y no devuelve nada

        fake_image = io.BytesIO(b'\x89PNG\r\n\x1a\nfakedata')
        resp = self.client.post(
            '/generate_video_from_image',
            data={'image': (fake_image, 'shirt.png')},
            content_type='multipart/form-data',
        )

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['success'])
        self.assertTrue(data.get('processing'))
        self.assertIn('message', data)

    @patch('routes.api.run_marketing_thread')
    def test_thread_launched_on_valid_request(self, mock_thread):
        """Verifica que run_marketing_thread se llama con el user_phone correcto."""
        mock_thread.return_value = None

        fake_image = io.BytesIO(b'\x89PNG\r\n\x1a\nfakedata')
        self.client.post(
            '/generate_video_from_image',
            data={'image': (fake_image, 'shirt.png')},
            content_type='multipart/form-data',
        )

        self.assertTrue(mock_thread.called)
        call_kwargs = mock_thread.call_args[1]
        self.assertEqual(call_kwargs.get('user_phone'), self.TEST_PHONE)

    @patch('routes.api.run_marketing_thread')
    def test_failed_generation_returns_500(self, mock_thread):
        """Si run_marketing_thread lanza excepción, el endpoint responde 500."""
        mock_thread.side_effect = Exception("Runway unavailable")

        fake_image = io.BytesIO(b'\x89PNG\r\n\x1a\nfakedata')
        resp = self.client.post(
            '/generate_video_from_image',
            data={'image': (fake_image, 'shirt.png')},
            content_type='multipart/form-data',
        )

        self.assertEqual(resp.status_code, 500)
        data = json.loads(resp.data)
        self.assertFalse(data['success'])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Endpoint /api/metrics/llm
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricsEndpoint(MetricsTestBase):

    def setUp(self):
        super().setUp()
        self._inject_session()

    def _seed_calls(self, n_gpt4o=3, n_runway=2):
        """Inserta registros LLMCall de prueba en la BD."""
        with app.app_context():
            for i in range(n_gpt4o):
                db.session.add(LLMCall(
                    user_phone=self.TEST_PHONE,
                    model='gpt-4o',
                    stage='chat_main',
                    prompt_tokens=100,
                    completion_tokens=50,
                    total_tokens=150,
                    latency_ms=1200 + i * 100,
                    cost_usd=0.002,
                    success=True,
                ))
            for i in range(n_runway):
                db.session.add(LLMCall(
                    user_phone=self.TEST_PHONE,
                    model='gen3a_turbo',
                    stage='runway_video_generation',
                    total_tokens=0,
                    latency_ms=30000,
                    cost_usd=0.25,
                    success=True,
                ))
            db.session.commit()

    def test_requires_authentication(self):
        """Sin sesión devuelve 401."""
        fresh_client = app.test_client()
        resp = fresh_client.get('/api/metrics/llm')
        self.assertEqual(resp.status_code, 401)

    def test_returns_valid_json_structure(self):
        """El endpoint devuelve JSON con todos los campos esperados."""
        self._seed_calls()
        resp = self.client.get('/api/metrics/llm')
        self.assertEqual(resp.status_code, 200)

        data = json.loads(resp.data)
        for field in ('total_calls', 'total_tokens', 'total_cost_usd',
                      'avg_latency_ms', 'success_rate', 'by_model', 'by_stage', 'daily'):
            self.assertIn(field, data, f"Campo '{field}' ausente en la respuesta")

    def test_total_calls_matches_seeded_data(self):
        """total_calls refleja exactamente los registros insertados."""
        self._seed_calls(n_gpt4o=4, n_runway=1)
        resp = self.client.get('/api/metrics/llm')
        data = json.loads(resp.data)
        self.assertEqual(data['total_calls'], 5)

    def test_total_tokens_aggregated_correctly(self):
        """total_tokens suma todos los registros del usuario."""
        self._seed_calls(n_gpt4o=3, n_runway=0)
        # 3 registros * 150 tokens = 450
        resp = self.client.get('/api/metrics/llm')
        data = json.loads(resp.data)
        self.assertEqual(data['total_tokens'], 450)

    def test_by_model_groups_correctly(self):
        """by_model agrupa registros por modelo con conteo correcto."""
        self._seed_calls(n_gpt4o=3, n_runway=2)
        resp = self.client.get('/api/metrics/llm')
        data = json.loads(resp.data)

        models = {m['model']: m for m in data['by_model']}
        self.assertIn('gpt-4o', models)
        self.assertIn('gen3a_turbo', models)
        self.assertEqual(models['gpt-4o']['calls'], 3)
        self.assertEqual(models['gen3a_turbo']['calls'], 2)

    def test_by_stage_present_for_seeded_stages(self):
        """by_stage incluye los stages de los registros insertados."""
        self._seed_calls()
        resp = self.client.get('/api/metrics/llm')
        data = json.loads(resp.data)

        stages = {s['stage'] for s in data['by_stage']}
        self.assertIn('chat_main', stages)
        self.assertIn('runway_video_generation', stages)

    def test_empty_state_returns_zero_totals(self):
        """Sin registros LLMCall los totales son cero."""
        resp = self.client.get('/api/metrics/llm')
        data = json.loads(resp.data)
        self.assertEqual(data['total_calls'], 0)
        self.assertEqual(data['total_tokens'], 0)
        self.assertEqual(data['total_cost_usd'], 0.0)
        self.assertEqual(data['by_model'], [])
        self.assertEqual(data['by_stage'], [])

    def test_success_rate_100_when_all_succeed(self):
        """Tasa de éxito 100% cuando no hay fallos."""
        self._seed_calls(n_gpt4o=2, n_runway=1)
        resp = self.client.get('/api/metrics/llm')
        data = json.loads(resp.data)
        self.assertEqual(data['success_rate'], 100.0)

    def test_success_rate_reflects_failures(self):
        """Tasa de éxito baja correctamente cuando hay registros fallidos."""
        with app.app_context():
            db.session.add(LLMCall(
                user_phone=self.TEST_PHONE, model='gpt-4o', stage='ok',
                total_tokens=100, latency_ms=500, cost_usd=0.001, success=True,
            ))
            db.session.add(LLMCall(
                user_phone=self.TEST_PHONE, model='gen3a_turbo', stage='fail',
                total_tokens=0, latency_ms=1000, cost_usd=0, success=False,
            ))
            db.session.commit()

        resp = self.client.get('/api/metrics/llm')
        data = json.loads(resp.data)
        self.assertEqual(data['success_rate'], 50.0)

    def test_only_returns_own_user_data(self):
        """Un usuario normal solo ve sus propios registros, no los de otros."""
        with app.app_context():
            # Registro del usuario de prueba
            db.session.add(LLMCall(
                user_phone=self.TEST_PHONE, model='gpt-4o', stage='mine',
                total_tokens=100, latency_ms=300, cost_usd=0.001, success=True,
            ))
            # Registro de otro usuario
            db.session.add(LLMCall(
                user_phone='+34999000000', model='gpt-4o', stage='other',
                total_tokens=200, latency_ms=400, cost_usd=0.002, success=True,
            ))
            db.session.commit()

        resp = self.client.get('/api/metrics/llm')
        data = json.loads(resp.data)
        # El usuario solo debe ver su propio registro
        self.assertEqual(data['total_calls'], 1)
        stages = {s['stage'] for s in data['by_stage']}
        self.assertIn('mine', stages)
        self.assertNotIn('other', stages)

    def test_metrics_page_requires_login(self):
        """/metrics redirige si no hay sesión."""
        fresh_client = app.test_client()
        resp = fresh_client.get('/metrics', follow_redirects=False)
        self.assertIn(resp.status_code, [301, 302])

    def test_metrics_page_accessible_when_logged_in(self):
        """/metrics devuelve 200 con sesión activa."""
        resp = self.client.get('/metrics')
        self.assertEqual(resp.status_code, 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
