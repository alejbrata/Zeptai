import json
import logging
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone, date as date_type
try:
    from pgvector.sqlalchemy import Vector as _Vector
    _EMBEDDING_COL = _Vector(1536)
except ImportError:
    # Fallback para entornos de test (SQLite) donde pgvector no está disponible
    from sqlalchemy import Text as _Text
    _EMBEDDING_COL = _Text()

_logger = logging.getLogger(__name__)

db = SQLAlchemy()

def _now():
    return datetime.now(timezone.utc)

class BusinessProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    business_name = db.Column(db.String(100))
    logo_path = db.Column(db.String(300))
    password_hash = db.Column(db.String(200))

    # Password Recovery
    reset_token = db.Column(db.String(100), nullable=True)
    reset_token_expiry = db.Column(db.DateTime(timezone=True), nullable=True)

    # SaaS Info
    plan_tier = db.Column(db.String(20), default='BASIC')
    features = db.Column(db.JSON, default={})

    # Chatbot Config
    system_prompt = db.Column(db.Text)
    static_knowledge = db.Column(db.JSON, default={})

    # Marketplace (Suscripciones a Agentes)
    active_agents = db.Column(db.JSON, default=[])
    agent_config = db.Column(db.JSON, default={})

    # Web Push (PWA notifications — replaces WhatsApp push)
    push_subscription = db.Column(db.Text, nullable=True)  # JSON PushSubscription object

    created_at = db.Column(db.DateTime(timezone=True), default=_now)

    @property
    def static_knowledge_dict(self) -> dict:
        sk = self.static_knowledge or {}
        if isinstance(sk, str):
            try:
                return json.loads(sk)
            except (json.JSONDecodeError, ValueError):
                return {}
        return sk if isinstance(sk, dict) else {}

    def to_agent_context(self) -> dict:
        sk = self.static_knowledge_dict
        return {
            "business_name": self.business_name or "",
            "phone":         self.user_phone,
            "email":         self.email or "",
            "sector":        sk.get("sector", "Servicios"),
            "extra_info":    sk,
        }


class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=False, index=True)
    image_path = db.Column(db.String(300))
    status = db.Column(db.String(20), default='pending')

    # Datos Fiscales
    concept = db.Column(db.String(100))
    total = db.Column(db.Float)
    date = db.Column(db.DateTime(timezone=True), default=_now)

    # Detalles Avanzados
    nif = db.Column(db.String(20))
    provider = db.Column(db.String(100))
    ticket_number = db.Column(db.String(50))
    base = db.Column(db.Float)
    tax_percent = db.Column(db.Float)
    fee = db.Column(db.Float)

    raw_data = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    business_phone = db.Column(db.String(20), nullable=False, index=True)
    # Stored as proper Date — callers must pass datetime.date objects or "YYYY-MM-DD" strings
    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.String(10), nullable=False)      # HH:MM start
    end_time = db.Column(db.String(10), nullable=True)   # HH:MM end (None = start + 1h)
    client_name = db.Column(db.String(100))
    client_phone = db.Column(db.String(20))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)  # 'user', 'assistant', 'tool'
    content = db.Column(db.Text)
    # Required by OpenAI API for role='tool' messages
    name = db.Column(db.String(100), nullable=True)
    tool_call_id = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class Grant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    sector_focus = db.Column(db.String(100))
    amount = db.Column(db.String(50))
    link = db.Column(db.String(300))
    deadline = db.Column(db.String(50))
    notified_phones = db.Column(db.JSON, default=[])
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class SynergyMatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_a_phone = db.Column(db.String(20), nullable=False)
    user_b_phone = db.Column(db.String(20), nullable=False)
    score = db.Column(db.Integer)
    reason = db.Column(db.Text)
    status = db.Column(db.String(20), default='suggested')
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=False)
    agent_name = db.Column(db.String(50), nullable=False)
    action = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), default=_now)

    @staticmethod
    def log(phone, agent, action):
        try:
            new_log = ActivityLog(user_phone=phone, agent_name=agent, action=action)
            db.session.add(new_log)
            db.session.commit()
            _logger.debug("ActivityLog: %s -> %s", agent, action)
        except Exception as e:
            _logger.error("Error guardando ActivityLog: %s", e)
            db.session.rollback()


class Incident(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=False)
    order_id = db.Column(db.String(50), nullable=True)
    type = db.Column(db.String(50))
    status = db.Column(db.String(20), default="Abierto")
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class GeneratedDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=False, index=True)
    file_path = db.Column(db.String(300), nullable=False)
    doc_type = db.Column(db.String(50))
    client_name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)

    # Ciclo de vida de factura
    invoice_number = db.Column(db.String(20),  nullable=True)   # F-2026-001
    client_nif     = db.Column(db.String(20),  nullable=True)
    subtotal       = db.Column(db.Float,        nullable=True)
    vat_amount     = db.Column(db.Float,        nullable=True)
    total_amount   = db.Column(db.Float,        nullable=True)
    invoice_status = db.Column(db.String(20),  nullable=True)   # draft | sent | paid
    doc_data       = db.Column(db.JSON,         nullable=True)  # items, notas, etc.

    @staticmethod
    def next_invoice_number(user_phone: str, year: int) -> str:
        from sqlalchemy import func
        count = db.session.query(func.count(GeneratedDocument.id)).filter(
            GeneratedDocument.user_phone == user_phone,
            GeneratedDocument.doc_type == 'invoice',
            GeneratedDocument.invoice_number.isnot(None),
            GeneratedDocument.invoice_number.like(f'F-{year}-%'),
        ).scalar() or 0
        return f"F-{year}-{count + 1:03d}"


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=False, index=True)
    title = db.Column(db.String(100), nullable=False)
    message = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(20), default='info')
    link = db.Column(db.String(300), nullable=True)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class KnowledgeChunk(db.Model):
    """Fragmento de conocimiento del negocio almacenado como embedding vectorial."""
    __tablename__ = 'knowledge_chunk'
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=False, index=True)
    source_type = db.Column(db.String(50), nullable=False)   # 'wizard' | 'document'
    source_name = db.Column(db.String(255), nullable=False)  # nombre del campo o del fichero
    content = db.Column(db.Text, nullable=False)
    embedding = db.Column(_EMBEDDING_COL)                    # Vector(1536) en PostgreSQL, Text en SQLite/test
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class LLMCall(db.Model):
    """Registro de cada llamada a un modelo de lenguaje o generación de medios."""
    __tablename__ = 'llm_call'
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=True, index=True)
    model = db.Column(db.String(60), nullable=False, index=True)
    stage = db.Column(db.String(100), nullable=False)
    prompt_tokens = db.Column(db.Integer, default=0)
    completion_tokens = db.Column(db.Integer, default=0)
    total_tokens = db.Column(db.Integer, default=0)
    latency_ms = db.Column(db.Integer, default=0)
    cost_usd = db.Column(db.Float, default=0.0)
    success = db.Column(db.Boolean, default=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, index=True)


class ChatFeedback(db.Model):
    """Feedback OK/KO del usuario sobre cada respuesta del chatbot."""
    __tablename__ = 'chat_feedback'
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=False, index=True)
    rating = db.Column(db.SmallInteger, nullable=False)   # +1 OK, -1 KO
    message_preview = db.Column(db.String(200))           # primeros 200 chars de la respuesta
    created_at = db.Column(db.DateTime(timezone=True), default=_now, index=True)


class RagRetrieval(db.Model):
    """Log de cada consulta RAG: cuántos chunks se recuperaron y su score medio."""
    __tablename__ = 'rag_retrieval'
    id = db.Column(db.Integer, primary_key=True)
    user_phone = db.Column(db.String(20), nullable=False, index=True)
    query_preview = db.Column(db.String(200))             # primeros 200 chars de la query
    chunks_returned = db.Column(db.Integer, default=0)
    avg_score = db.Column(db.Float, nullable=True)        # 0=perfecto, 1=sin relación (distancia coseno)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, index=True)
