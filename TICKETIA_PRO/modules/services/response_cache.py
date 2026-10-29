import hashlib
import logging
import re
import time
from threading import Lock

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600   # 1 hora — suficiente para preguntas repetidas de horarios/servicios
_MAX_ENTRIES = 500    # ~500 negocios activos simultáneos

# La respuesta a "¿qué tengo mañana?" cambia de un día para otro: las preguntas
# con referencias temporales relativas nunca se cachean.
_TEMPORAL_RE = re.compile(
    r'(?i)\b(hoy|mañana|manana|ayer|pasado|semana|mes|año|ahora|esta tarde|esta noche|'
    r'lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)\b'
)


class _ResponseCache:
    """Caché en memoria con TTL para respuestas LLM informacionales (sin tool calls)."""

    def __init__(self):
        self._store: dict = {}
        self._lock = Lock()

    @staticmethod
    def _make_key(business_phone: str, message: str) -> str:
        normalized = message.lower().strip()
        return hashlib.sha256(f"{business_phone}|{normalized}".encode()).hexdigest()

    @staticmethod
    def is_cacheable(message: str) -> bool:
        return not _TEMPORAL_RE.search(message or "")

    def get(self, business_phone: str, message: str) -> str | None:
        if not self.is_cacheable(message):
            return None
        key = self._make_key(business_phone, message)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry["expires_at"]:
                del self._store[key]
                return None
            logger.debug("CACHE HIT [%s] %.60s", business_phone, message)
            return entry["response"]

    def set(self, business_phone: str, message: str, response: str) -> None:
        if not self.is_cacheable(message):
            return
        key = self._make_key(business_phone, message)
        with self._lock:
            if len(self._store) >= _MAX_ENTRIES:
                # Desalojar la entrada que expira antes
                oldest = min(self._store, key=lambda k: self._store[k]["expires_at"])
                del self._store[oldest]
            self._store[key] = {
                "business_phone": business_phone,  # necesario para invalidate()
                "response": response,
                "expires_at": time.monotonic() + _TTL_SECONDS,
            }
            logger.debug("CACHE SET [%s] %.60s", business_phone, message)

    def invalidate(self, business_phone: str) -> int:
        """Elimina todas las entradas de un negocio.

        Se llama al indexar o borrar documentos y al guardar el wizard: sin esto,
        una pregunta repetida tras subir el PDF devolvía la respuesta anterior
        ("no tengo esa información") durante una hora. Es por proceso: con varios
        workers cada uno invalida la suya cuando atiende la petición; el resto
        caduca por TTL. Con Redis la invalidación sería global.
        """
        with self._lock:
            before = len(self._store)
            # La clave puede llevar un espacio de nombres "telefono|version": se
            # compara solo el telefono.
            to_delete = [k for k, v in self._store.items()
                         if (v.get("business_phone") or "").split("|")[0] == business_phone]
            for k in to_delete:
                del self._store[k]
            return before - len(self._store)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Singleton de proceso — se comparte entre todos los workers del mismo proceso
response_cache = _ResponseCache()
