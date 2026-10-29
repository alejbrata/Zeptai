"""
Blueprint /knowledge — Base de Conocimiento Vectorial (RAG)

Rutas:
  GET  /knowledge                      → página principal
  POST /knowledge/upload               → subir documento (PDF/TXT)
  POST /knowledge/delete/<source_name> → eliminar documento
  GET  /knowledge/status               → JSON con estadísticas
"""

import os
import logging
from flask import Blueprint, request, session, redirect, url_for, render_template, jsonify, current_app
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

knowledge_bp = Blueprint('knowledge', __name__)

ALLOWED_EXTENSIONS = {'pdf', 'txt', 'md'}
MAX_FILE_MB = 20


def _allowed(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Página principal
# ---------------------------------------------------------------------------

@knowledge_bp.route('/knowledge')
def knowledge_page():
    if 'user_phone' not in session:
        return redirect(url_for('web.login'))

    from modules.services.embeddings import list_documents
    docs = list_documents(session['user_phone'])

    return render_template(
        'knowledge.html',
        current_page='knowledge',
        documents=docs,
    )


# ---------------------------------------------------------------------------
# Subir documento
# ---------------------------------------------------------------------------

@knowledge_bp.route('/knowledge/upload', methods=['POST'])
def knowledge_upload():
    if 'user_phone' not in session:
        return jsonify({'success': False, 'error': 'No autenticado'}), 401

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No se recibió ningún fichero'}), 400

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({'success': False, 'error': 'Nombre de fichero vacío'}), 400

    if not _allowed(file.filename):
        return jsonify({'success': False, 'error': 'Solo se permiten PDF, TXT o MD'}), 400

    # Comprobar tamaño
    file.seek(0, 2)
    size_mb = file.tell() / (1024 * 1024)
    file.seek(0)
    if size_mb > MAX_FILE_MB:
        return jsonify({'success': False, 'error': f'El fichero supera los {MAX_FILE_MB} MB'}), 400

    # Validar MIME real (previene ZIP bombs y ficheros maliciosos con extensión falsa)
    _KNOWLEDGE_ALLOWED_MIME = {
        'application/pdf', 'text/plain', 'text/markdown', 'text/x-markdown',
    }
    try:
        import magic
        mime = magic.from_buffer(file.read(2048), mime=True)
        file.seek(0)
        if mime not in _KNOWLEDGE_ALLOWED_MIME:
            return jsonify({'success': False, 'error': 'El contenido del fichero no es válido'}), 400
    except ImportError:
        file.seek(0)

    user_phone = session['user_phone']
    filename = secure_filename(file.filename)
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'knowledge', user_phone)
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, filename)
    file.save(file_path)

    # Ingestar en background para no bloquear la respuesta HTTP
    import threading
    app = current_app._get_current_object()

    def _ingest():
        with app.app_context():
            try:
                from modules.services.embeddings import ingest_document
                from modules.services.notification import NotificationService
                chunks = ingest_document(user_phone, file_path, filename)
                from modules.services.response_cache import response_cache
                response_cache.invalidate(user_phone)
                NotificationService.send_in_app(
                    user_phone,
                    '📚 Documento procesado',
                    f'"{filename}" añadido a tu base de conocimiento ({chunks} fragmentos).',
                    type='info',
                    link='/knowledge',
                )
                logger.info("Documento '%s' ingestado: %d chunks", filename, chunks)
            except Exception as e:
                logger.error("Error ingestando documento '%s': %s", filename, e)

    threading.Thread(target=_ingest, daemon=True).start()

    return jsonify({
        'success': True,
        'message': f'"{filename}" subido. Procesando embeddings en segundo plano — recibirás una notificación cuando esté listo.',
        'filename': filename,
    })


# ---------------------------------------------------------------------------
# Eliminar documento
# ---------------------------------------------------------------------------

@knowledge_bp.route('/knowledge/delete', methods=['POST'])
def knowledge_delete():
    if 'user_phone' not in session:
        return jsonify({'success': False, 'error': 'No autenticado'}), 401

    filename = request.form.get('filename') or (request.get_json(silent=True) or {}).get('filename')
    if not filename:
        return jsonify({'success': False, 'error': 'Falta el nombre del fichero'}), 400

    user_phone = session['user_phone']
    from modules.services.embeddings import delete_document_chunks
    deleted = delete_document_chunks(user_phone, filename)
    from modules.services.response_cache import response_cache
    response_cache.invalidate(user_phone)

    # Intentar borrar el fichero físico también
    file_path = os.path.join(
        current_app.root_path, 'static', 'uploads', 'knowledge', user_phone,
        secure_filename(filename)
    )
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass

    return jsonify({'success': True, 'deleted_chunks': deleted})


# ---------------------------------------------------------------------------
# Estado JSON (para polling del frontend)
# ---------------------------------------------------------------------------

@knowledge_bp.route('/knowledge/status')
def knowledge_status():
    if 'user_phone' not in session:
        return jsonify({'error': 'No autenticado'}), 401

    from modules.services.embeddings import list_documents
    docs = list_documents(session['user_phone'])
    total_chunks = sum(d['chunks'] for d in docs)
    return jsonify({'documents': docs, 'total_chunks': total_chunks})
