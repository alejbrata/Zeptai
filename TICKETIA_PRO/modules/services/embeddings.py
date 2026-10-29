"""
Servicio de embeddings vectoriales para RAG.

Responsabilidades:
  - embed_text()            : genera embedding de un texto con text-embedding-3-small
  - ingest_wizard_chunks()  : trocea y embebe los campos del wizard (static_knowledge)
  - ingest_document()       : trocea y embebe un fichero PDF o TXT
  - retrieve_chunks()       : similarity search por coseno en pgvector
  - delete_chunks()         : elimina todos los chunks de un usuario/fuente
"""

import logging
import re

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM   = 1536
# ~400 tokens × 4 chars/token. Overlap del 25% para cubrir boundaries sin duplicar contexto.
CHUNK_SIZE      = 1600
CHUNK_OVERLAP   = 400


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_text(text: str, user_phone: str | None = None, stage: str = "embedding") -> list[float]:
    """Devuelve el embedding de un texto. Eleva excepción si falla."""
    from core.clients import get_openai_client
    from core.llm_tracker import timed_track
    client = get_openai_client()
    with timed_track(user_phone, EMBEDDING_MODEL, stage) as t:
        response = client.embeddings.create(model=EMBEDDING_MODEL, input=text[:8000])
        t["response"] = response
    return response.data[0].embedding


# ---------------------------------------------------------------------------
# Ingesta desde el wizard (campos de static_knowledge + system_prompt)
# ---------------------------------------------------------------------------

def ingest_wizard_chunks(user_phone: str, static_knowledge: dict, system_prompt: str = "") -> int:
    """
    Embebe los campos del wizard como chunks individuales.
    Borra los chunks wizard anteriores antes de reinsertar.
    Devuelve el número de chunks creados.
    """
    from core.db_models import db, KnowledgeChunk

    # Borrar chunks wizard anteriores de este usuario
    KnowledgeChunk.query.filter_by(user_phone=user_phone, source_type='wizard').delete()
    db.session.flush()

    field_labels = {
        'schedule':        'Horario de atención',
        'services':        'Servicios principales',
        'payment_methods': 'Métodos de pago',
        'instructions':    'Instrucciones específicas del negocio',
        'sector':          'Sector',
        'tone':            'Tono del asistente',
    }

    created = 0
    for field, label in field_labels.items():
        value = static_knowledge.get(field, '').strip() if static_knowledge else ''
        if not value:
            continue
        text = f"{label}: {value}"
        try:
            embedding = embed_text(text, user_phone, "embedding_ingest")
            db.session.add(KnowledgeChunk(
                user_phone=user_phone,
                source_type='wizard',
                source_name=field,
                content=text,
                embedding=embedding,
            ))
            created += 1
        except Exception as e:
            logger.warning("Error embebiendo campo wizard '%s': %s", field, e)

    if system_prompt and system_prompt.strip():
        try:
            embedding = embed_text(system_prompt[:2000], user_phone, "embedding_ingest")
            db.session.add(KnowledgeChunk(
                user_phone=user_phone,
                source_type='wizard',
                source_name='system_prompt',
                content=system_prompt[:2000],
                embedding=embedding,
            ))
            created += 1
        except Exception as e:
            logger.warning("Error embebiendo system_prompt: %s", e)

    db.session.commit()
    logger.info("Wizard chunks creados para %s: %d", user_phone, created)
    return created


# ---------------------------------------------------------------------------
# Ingesta de documentos (PDF / TXT)
# ---------------------------------------------------------------------------

def _split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Chunker recursivo: intenta cortar por párrafos, luego por frases, luego por
    caracteres. Garantiza que ningún chunk supera chunk_size y que hay overlap
    entre chunks consecutivos para no perder contexto en los límites.
    """
    separators = ["\n\n", "\n", ". ", " ", ""]

    def _recursive(text: str, seps: list[str]) -> list[str]:
        sep = seps[0]
        next_seps = seps[1:]
        if not sep:
            # nivel base: partir por chars con overlap
            result = []
            start = 0
            while start < len(text):
                end = start + chunk_size
                result.append(text[start:end].strip())
                start += chunk_size - overlap
            return result

        parts = re.split(re.escape(sep), text)
        chunks, current = [], ""
        for part in parts:
            candidate = (current + sep + part).strip() if current else part.strip()
            if len(candidate) <= chunk_size:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                # si la parte sola ya es mayor que chunk_size, bajar de nivel
                if len(part.strip()) > chunk_size and next_seps:
                    chunks.extend(_recursive(part.strip(), next_seps))
                else:
                    current = part.strip()
        if current:
            chunks.append(current)
        return chunks

    raw = _recursive(text, separators)

    # añadir solapamiento entre chunks consecutivos
    result = []
    for i, chunk in enumerate(raw):
        if i > 0 and len(chunk) < chunk_size:
            prev_tail = raw[i - 1][-overlap:]
            chunk = (prev_tail + " " + chunk).strip()
        result.append(chunk)

    return [c for c in result if len(c) > 50]


def _extract_text_from_file(file_path: str, filename: str) -> str:
    """Extrae texto en markdown de PDF (preservando estructura) o lee TXT/MD tal cual."""
    ext = filename.rsplit('.', 1)[-1].lower()
    if ext == 'pdf':
        try:
            import pymupdf4llm
            return pymupdf4llm.to_markdown(file_path)
        except ImportError:
            logger.warning("pymupdf4llm no disponible, usando pypdf como fallback para '%s'", filename)
        except Exception as e:
            logger.error("Error convirtiendo PDF a markdown '%s': %s", filename, e)
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            return "\n".join(page.extract_text() or '' for page in reader.pages)
        except Exception as e:
            logger.error("Error leyendo PDF '%s': %s", filename, e)
            return ""
    elif ext in ('txt', 'md'):
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    else:
        logger.warning("Formato no soportado: %s", ext)
        return ""


def ingest_document(user_phone: str, file_path: str, filename: str) -> int:
    """
    Trocea y embebe un documento (PDF/TXT).
    Devuelve el número de chunks creados.
    """
    from core.db_models import db, KnowledgeChunk

    text = _extract_text_from_file(file_path, filename)
    if not text.strip():
        logger.warning("Documento vacío o sin texto extraíble: %s", filename)
        return 0

    chunks = _split_text(text)
    created = 0
    for i, chunk in enumerate(chunks):
        try:
            embedding = embed_text(chunk, user_phone, "embedding_ingest")
            db.session.add(KnowledgeChunk(
                user_phone=user_phone,
                source_type='document',
                source_name=filename,
                content=chunk,
                embedding=embedding,
            ))
            created += 1
        except Exception as e:
            logger.warning("Error embebiendo chunk %d de '%s': %s", i, filename, e)

    db.session.commit()
    logger.info("Documento '%s' ingestado para %s: %d chunks", filename, user_phone, created)
    return created


# ---------------------------------------------------------------------------
# Retrieval (RAG)
# ---------------------------------------------------------------------------

def retrieve_chunks(user_phone: str, query: str, top_k: int = 5) -> list[str]:
    """
    Devuelve los top_k chunks más relevantes. Loguea la calidad en RagRetrieval.
    Retorna lista vacía si no hay chunks o falla el embedding.
    """
    results = retrieve_chunks_scored(user_phone, query, top_k)
    return [content for content, _ in results]


def retrieve_chunks_scored(user_phone: str, query: str, top_k: int = 5) -> list[tuple]:
    """
    Devuelve lista de (content, score) donde score es la distancia coseno
    (0 = perfecto, 1 = sin relación). Loguea métricas en RagRetrieval.
    """
    from core.db_models import KnowledgeChunk, RagRetrieval, db
    from sqlalchemy import literal_column

    count = KnowledgeChunk.query.filter_by(user_phone=user_phone).count()
    if count == 0:
        return []

    try:
        query_embedding = embed_text(query, user_phone, "embedding_query")
    except Exception as e:
        logger.warning("Error embebiendo query RAG: %s", e)
        return []

    try:
        distance_col = KnowledgeChunk.embedding.cosine_distance(query_embedding)
        rows = (
            KnowledgeChunk.query
            .filter_by(user_phone=user_phone)
            .add_columns(distance_col.label("score"))
            .order_by(distance_col)
            .limit(top_k)
            .all()
        )
        pairs = [(r.KnowledgeChunk.content, round(float(r.score), 4)) for r in rows]

        # Log de calidad RAG
        try:
            avg = sum(s for _, s in pairs) / len(pairs) if pairs else None
            db.session.add(RagRetrieval(
                user_phone=user_phone,
                query_preview=query[:200],
                chunks_returned=len(pairs),
                avg_score=avg,
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()

        return pairs
    except Exception as e:
        logger.error("Error en similarity search pgvector: %s", e)
        return []


# ---------------------------------------------------------------------------
# Utilidades de gestión
# ---------------------------------------------------------------------------

def delete_document_chunks(user_phone: str, filename: str) -> int:
    """Elimina todos los chunks de un documento concreto."""
    from core.db_models import db, KnowledgeChunk
    deleted = KnowledgeChunk.query.filter_by(
        user_phone=user_phone,
        source_type='document',
        source_name=filename
    ).delete()
    db.session.commit()
    return deleted


def list_documents(user_phone: str) -> list[dict]:
    """Lista los documentos ingestados (agrupados por source_name)."""
    from core.db_models import db, KnowledgeChunk
    from sqlalchemy import func
    rows = (
        db.session.query(
            KnowledgeChunk.source_name,
            KnowledgeChunk.source_type,
            func.count(KnowledgeChunk.id).label('chunks'),
            func.max(KnowledgeChunk.created_at).label('uploaded_at'),
        )
        .filter_by(user_phone=user_phone)
        .group_by(KnowledgeChunk.source_name, KnowledgeChunk.source_type)
        .order_by(func.max(KnowledgeChunk.created_at).desc())
        .all()
    )
    return [
        {
            'name': r.source_name,
            'type': r.source_type,
            'chunks': r.chunks,
            'uploaded_at': r.uploaded_at.strftime('%d/%m/%Y %H:%M') if r.uploaded_at else '',
        }
        for r in rows
    ]
