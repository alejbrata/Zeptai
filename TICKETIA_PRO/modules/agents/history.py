import re
import logging
from core.db_models import db, ChatMessage

logger = logging.getLogger(__name__)

MAX_HISTORY      = 50    # trigger cleanup above this
KEEP_COUNT       = 40    # messages to keep after cleanup
MAX_MSG_LENGTH   = 8000  # caracteres máximos por mensaje en historial

_VALID_ROLES = {'user', 'assistant', 'tool', 'system'}

# Patrones de prompt injection en historial (inyección indirecta vía tool outputs)
_HISTORY_INJECTION_PATTERNS = [
    r'(?i)ignore\s+(all\s+)?(previous\s+)?instructions',
    r'(?i)ignora\s+(todas?\s+)?(las\s+)?instrucciones',
    r'(?i)olvida\s+(todo|tus\s+instrucciones)',
    r'(?i)you\s+are\s+now\s+(a\s+)?different',
    r'(?i)new\s+system\s+prompt',
    r'(?i)<\|im_start\|>',
    r'(?i)<\|im_end\|>',
    r'(?i)\[SYSTEM\]',
    r'(?i)OVERRIDE\s+INSTRUCTIONS',
    r'(?i)disregard\s+(your\s+)?(previous\s+)?',
]

def _sanitize_history_content(content: str) -> str:
    """Limpia contenido antes de guardarlo en historial para evitar history poisoning."""
    if not isinstance(content, str):
        content = str(content)
    if len(content) > MAX_MSG_LENGTH:
        content = content[:MAX_MSG_LENGTH]
    for pattern in _HISTORY_INJECTION_PATTERNS:
        if re.search(pattern, content):
            logger.warning("History poisoning detectado, contenido filtrado: %.60r", content)
            return "[CONTENIDO FILTRADO]"
    return content

class HistoryService:
    @staticmethod
    def get_recent_history(phone, limit=10):
        """
        Recupera el historial reciente de chat en orden cronológico.
        Devuelve formato compatible con OpenAI API (incluyendo campo 'name'
        para mensajes role='tool', obligatorio en strict mode).
        """
        messages_db = (
            ChatMessage.query
            .filter_by(user_phone=phone)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
            .all()
        )
        messages_db.reverse()  # cronológico: antiguo → nuevo

        history = []
        for msg in messages_db:
            msg_dict = {"role": msg.role, "content": msg.content}
            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id
            if msg.name:
                msg_dict["name"] = msg.name
            history.append(msg_dict)

        return history

    @staticmethod
    def save_interaction(phone, role, content, tool_call_id=None, name=None):
        """
        Guarda un mensaje en el historial con limpieza automática.
        'name' es obligatorio para role='tool' (nombre de la función llamada).
        """
        try:
            if role not in _VALID_ROLES:
                logger.error("Role inválido en historial: %r", role)
                return
            content = _sanitize_history_content(content)
            new_msg = ChatMessage(
                user_phone=phone,
                role=role,
                content=content,
                tool_call_id=tool_call_id,
                name=name
            )
            db.session.add(new_msg)

            # Auto-cleanup: contamos ANTES del commit del nuevo mensaje
            # +1 porque el nuevo ya está en la sesión pero no en el count de DB
            current_count = ChatMessage.query.filter_by(user_phone=phone).count() + 1

            if current_count > MAX_HISTORY:
                to_delete_count = current_count - KEEP_COUNT
                old_messages = (
                    ChatMessage.query
                    .filter_by(user_phone=phone)
                    .order_by(ChatMessage.created_at.asc())
                    .limit(to_delete_count)
                    .all()
                )
                for old_msg in old_messages:
                    db.session.delete(old_msg)
                logger.info("History cleanup: %d mensajes eliminados para %s", len(old_messages), phone)

            db.session.commit()
        except Exception as e:
            logger.error("Error saving history: %s", e)
            db.session.rollback()
