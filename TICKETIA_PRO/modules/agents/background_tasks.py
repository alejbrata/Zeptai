import logging
import threading
from flask import current_app
from core.db_models import GeneratedDocument, ActivityLog, db

logger = logging.getLogger(__name__)

def perform_async_marketing_generation(user_phone, prompt, format_type, host_url, p_business_name, p_logo_path, context_app, slide_count=None):
    """
    Ejecuta la generación de contenido de marketing en segundo plano para no bloquear
    la respuesta a la petición HTTP. El resultado queda guardado en Documentos (PWA).
    """
    with context_app.app_context():
        try:
            logger.info("Thread: iniciando generación background para %s", user_phone)
            from modules.proactive.marketing_agent import MarketingAgent
            from modules.services.notification import NotificationService
            agent = MarketingAgent()

            file_path, doc_title = agent.generate_marketing_content(prompt, format_type, business_name=p_business_name, logo_path=p_logo_path, user_phone=user_phone, slide_count=slide_count)

            if file_path:
                try:
                    doc_type = 'video_prompt' if format_type == 'video' else ('image' if format_type == 'image' else 'presentation')
                    new_doc = GeneratedDocument(
                        user_phone=user_phone,
                        file_path=file_path,
                        doc_type=doc_type,
                        client_name=doc_title
                    )
                    db.session.add(new_doc)
                    db.session.commit()
                except Exception as e:
                    logger.error("Thread: error guardando GeneratedDocument: %s", e)

                ActivityLog.log(user_phone, "Marketing Agent", f"Diseño listo en Documentos: {format_type}")

                # Notificar al usuario en la app
                type_labels = {'video': ('🎬 Reel listo', 'Tu vídeo ya está disponible en Documentos → Video Prompts.'),
                               'image': ('🖼️ Imagen lista', 'Tu imagen ya está disponible en Documentos → Imágenes.'),
                               'presentation': ('📊 Presentación lista', 'Tu presentación ya está disponible en Documentos.')}
                title, message = type_labels.get(format_type, ('✅ Contenido listo', 'Tu contenido de marketing ya está disponible en Documentos.'))
                NotificationService.send_in_app(user_phone, title, message, type='info', link='/documents')

                logger.info("Thread: contenido guardado y notificación enviada para %s", user_phone)
            else:
                error_titles = {
                    'video': '⚠️ Error generando vídeo',
                    'image': '⚠️ Error generando imagen',
                    'slide': '⚠️ Error generando presentación',
                }
                title = error_titles.get(format_type, '⚠️ Error generando contenido')
                detail = getattr(agent, 'last_error', None) or 'Causa desconocida — revisa los logs del servidor.'
                logger.warning("Thread: generación fallida para %s (%s): %s", user_phone, format_type, detail)
                NotificationService.send_in_app(
                    user_phone,
                    title,
                    f'No se pudo generar el contenido. Motivo: {detail}',
                    type='alert',
                    link='/marketing'
                )
        except Exception as e:
            logger.error("Thread: error crítico para %s: %s", user_phone, e)


def run_marketing_thread(user_phone, prompt, format_type, host_url, p_business_name, p_logo_path, slide_count=None):
    """Lanzador del hilo de marketing"""
    app_obj = current_app._get_current_object()
    thread = threading.Thread(
        target=perform_async_marketing_generation,
        args=(user_phone, prompt, format_type, host_url, p_business_name, p_logo_path, app_obj),
        kwargs={"slide_count": slide_count},
        daemon=True,
    )
    thread.start()
