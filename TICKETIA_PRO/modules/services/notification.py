import os
import json
import logging
from flask_mail import Message
from core.db_models import db, Notification, ActivityLog

logger = logging.getLogger(__name__)


class NotificationService:
    @staticmethod
    def send_in_app(user_phone, title, message, type='info', link=None):
        """Guarda una notificación en la base de datos para mostrar en la PWA."""
        try:
            notif = Notification(
                user_phone=user_phone,
                title=title,
                message=message,
                type=type,
                link=link
            )
            db.session.add(notif)
            db.session.commit()
            logger.info("Notificacion in-app guardada para %s: %s", user_phone, title)
            return True
        except Exception as e:
            logger.error("Error guardando notificacion in-app: %s", e)
            db.session.rollback()
            return False

    @staticmethod
    def send_email(subject, recipients, body=None, html=None, attachments=None):
        from app import mail
        try:
            msg = Message(
                subject=subject,
                sender=os.environ.get('MAIL_DEFAULT_SENDER', 'no-reply@zeptai.com'),
                recipients=recipients
            )
            if body:
                msg.body = body
            if html:
                msg.html = html

            if attachments:
                for att in attachments:
                    with open(att['path'], 'rb') as fp:
                        msg.attach(
                            filename=att['filename'],
                            content_type=att.get('content_type', 'application/pdf'),
                            data=fp.read()
                        )

            mail.send(msg)
            return True
        except Exception as e:
            logger.error("Error sending email: %s", e)
            return False

    @staticmethod
    def send_push(user_phone, title, body, url='/dashboard'):
        """
        Envía una Web Push notification al dispositivo del usuario (PWA).
        Requiere que el usuario haya concedido permiso y la suscripción esté guardada.
        """
        from core.db_models import BusinessProfile
        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            logger.warning("pywebpush no instalado. Omitiendo Web Push.")
            return False

        vapid_private = os.environ.get('VAPID_PRIVATE_KEY')
        vapid_public = os.environ.get('VAPID_PUBLIC_KEY')
        vapid_email = os.environ.get('VAPID_CLAIM_EMAIL', 'mailto:admin@zeptai.com')

        if not vapid_private or not vapid_public:
            logger.warning("VAPID keys no configuradas. Omitiendo Web Push.")
            return False

        user = BusinessProfile.query.filter_by(user_phone=user_phone).first()
        if not user or not user.push_subscription:
            return False

        try:
            subscription = json.loads(user.push_subscription)
        except (json.JSONDecodeError, TypeError):
            return False

        payload = json.dumps({"title": title, "body": body, "url": url})

        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": vapid_email}
            )
            logger.info("Web Push enviado a %s: %s", user_phone, title)
            return True
        except WebPushException as e:
            # Si el endpoint ya no existe (usuario revocó permiso), limpiar suscripción
            if e.response and e.response.status_code in (404, 410):
                user.push_subscription = None
                db.session.commit()
                logger.info("Suscripcion push expirada para %s, eliminada.", user_phone)
            else:
                logger.error("Error Web Push: %s", e)
            return False
        except Exception as e:
            logger.error("Error inesperado Web Push: %s", e)
            return False
