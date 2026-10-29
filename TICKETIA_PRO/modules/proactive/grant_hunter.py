import logging
from core.db_models import db, Grant, ActivityLog
from modules.services.notification import NotificationService

logger = logging.getLogger(__name__)


class GrantHunterAgent:
    def __init__(self):
        from core.clients import get_openai_client
        self.client = get_openai_client()

    def check_new_grants(self, user):
        """
        Busca nuevas ayudas para un usuario específico y le notifica si encuentra algo relevante.
        """
        logger.info("GrantHunter: revisando oportunidades para %s", user.business_name)

        all_grants = Grant.query.all()

        user_sector = user.static_knowledge.get('sector', 'General') if user.static_knowledge else 'General'
        user_location = user.static_knowledge.get('location', 'España') if user.static_knowledge else 'España'

        relevant_grants = []

        for grant in all_grants:
            notified_list = grant.notified_phones or []
            if user.user_phone in notified_list:
                continue

            is_match = False
            if grant.sector_focus == "General":
                is_match = True
            elif user_sector.lower() in grant.sector_focus.lower():
                is_match = True
            else:
                is_match = self._ai_match(user_sector, user_location, grant, user.user_phone)

            if is_match:
                relevant_grants.append(grant)

        for grant in relevant_grants[:2]:
            self._notify_grant(user, grant)

            current_notified = list(grant.notified_phones or [])
            current_notified.append(user.user_phone)
            grant.notified_phones = current_notified
            db.session.commit()

    def _ai_match(self, user_sector, user_location, grant, user_phone=None):
        try:
            prompt = f"""
            Actúa como consultor de ayudas.
            Usuario: Sector '{user_sector}', Ubicación '{user_location}'.
            Ayuda: '{grant.title}' para sector '{grant.sector_focus}'. Descripción: {grant.description}.

            ¿Es ALTA la probabilidad de que esta ayuda le interese y aplique?
            Responde SOLO 'SI' o 'NO'.
            """
            from core.llm_tracker import timed_track
            with timed_track(user_phone, "gpt-4o-mini", "grant_match") as t:
                resp = self.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=5,
                    temperature=0
                )
                t["response"] = resp
            return "SI" in resp.choices[0].message.content.upper()
        except Exception as e:
            logger.warning("GrantHunter: error AI match para '%s': %s", grant.title, e)
            return False

    def _notify_grant(self, user, grant):
        try:
            # 1. Generar mensaje persuasivo con IA
            prompt = f"""
            Escribe un mensaje corto (max 50 palabras) y emocionante para el dueño de un negocio ({user.business_name}).
            Avísale de esta ayuda: {grant.title} ({grant.amount}).
            Dile por qué le conviene. Usa emojis.
            """
            from core.llm_tracker import timed_track
            with timed_track(user.user_phone, "gpt-4o", "grant_pitch") as t:
                resp = self.client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=100
                )
                t["response"] = resp
            pitch_text = resp.choices[0].message.content

            # 2. Notificación In-App
            NotificationService.send_in_app(
                user_phone=user.user_phone,
                title="💰 Nueva Ayuda Disponible",
                message=f"{pitch_text}\n\nCuantía: {grant.amount}. Plazo: {grant.deadline}.",
                type="grant",
                link=grant.link
            )

            # 3. Web Push
            NotificationService.send_push(
                user_phone=user.user_phone,
                title="💰 Nueva Ayuda Detectada",
                body=f"{grant.title} — {grant.amount}",
                url=grant.link or "/dashboard"
            )

            ActivityLog.log(user.user_phone, "Grant Hunter", f"Avisado de ayuda: {grant.title}")

        except Exception as e:
            logger.error("GrantHunter: error notificando ayuda '%s': %s", grant.title, e)
