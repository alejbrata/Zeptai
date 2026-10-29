import json
import logging
import random
from core.db_models import db, BusinessProfile, SynergyMatch, Ticket, ActivityLog
from modules.services.notification import NotificationService

logger = logging.getLogger(__name__)


class SynergyAgent:
    def __init__(self):
        from core.clients import get_openai_client
        self.openai = get_openai_client()

    def run_daily_networking(self, user):
        """
        Busca aliados estratégicos basándose en perfil y comportamiento de compra.
        """
        logger.info("Networker: buscando aliados para %s", user.business_name)

        user_spending_profile = self._get_spending_profile(user)

        candidates = BusinessProfile.query.filter(BusinessProfile.id != user.id).all()
        random.shuffle(candidates)

        # Precargar todos los matches ya existentes del usuario en una sola query
        existing_matches = SynergyMatch.query.filter(
            (SynergyMatch.user_a_phone == user.user_phone) |
            (SynergyMatch.user_b_phone == user.user_phone)
        ).all()
        already_matched = {
            m.user_b_phone if m.user_a_phone == user.user_phone else m.user_a_phone
            for m in existing_matches
        }

        match_found = False

        for candidate in candidates[:5]:
            if candidate.user_phone in already_matched:
                continue

            synergy = self._analyze_synergy_deep(user, user_spending_profile, candidate)

            if synergy and synergy.get('score', 0) >= 80:
                self._save_match(user, candidate, synergy)
                self._notify_intro(user, candidate, synergy['reason'])
                match_found = True
                break

        if not match_found:
            logger.info("Networker: sin matches hoy para %s", user.business_name)

    def _get_spending_profile(self, user):
        tickets = Ticket.query.filter_by(user_phone=user.user_phone).order_by(Ticket.date.desc()).limit(10).all()
        if not tickets:
            return "Sin historial de compras reciente."

        concepts = [t.concept or "Gasto vario" for t in tickets]
        providers = [t.provider or "Desconocido" for t in tickets]

        return f"Últimos gastos en: {', '.join(concepts[:5])}. Proveedores frecuentes: {', '.join(providers[:3])}."

    def _analyze_synergy_deep(self, user_a, spending_a, user_b):
        info_a = f"Empresa A: {user_a.business_name} ({user_a.static_knowledge.get('sector', 'Varios')}).\nPerfil de Gasto A: {spending_a}"
        info_b = f"Empresa B: {user_b.business_name} ({user_b.static_knowledge.get('sector', 'Varios')}).\nServicios B: {user_b.static_knowledge.get('services', 'General')}"

        prompt = f"""
        Actúa como consultor de negocios B2B experto.
        Analiza si hay una oportunidad de negocio CLARA entre A y B.

        {info_a}

        {info_b}

        Busca:
        1. Relación Cliente-Proveedor (A gasta en lo que B vende).
        2. Alianza Estratégica (Ej: Gimnasio + Tienda Suplementos).

        Devuelve JSON: {{ "score": (0-100), "reason": "Frase corta y persuasiva para A explicando por qué conectar con B." }}
        """
        try:
            from core.llm_tracker import timed_track
            with timed_track(user_a.user_phone, "gpt-4o", "networker_synergy") as t:
                resp = self.openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    max_tokens=150,
                    temperature=0.3
                )
                t["response"] = resp
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.error("Error IA Synergy: %s", e)
            return None

    def _save_match(self, user_a, user_b, synergy):
        match = SynergyMatch(
            user_a_phone=user_a.user_phone,
            user_b_phone=user_b.user_phone,
            score=synergy['score'],
            reason=synergy['reason'],
            status='pending'
        )
        db.session.add(match)
        db.session.commit()
        ActivityLog.log(user_a.user_phone, "Networker Agent", f"Match sugerido: {user_b.business_name}")

    def _notify_intro(self, user, candidate, reason):
        try:
            # 1. Notificación In-App
            NotificationService.send_in_app(
                user_phone=user.user_phone,
                title="🤝 Nueva Oportunidad de Negocio",
                message=f"Creemos que deberías colaborar con {candidate.business_name}.\n\n💡 Motivo: {reason}\n\n¿Te interesa conectar?",
                type="networking",
                link="/networking"
            )

            # 2. Web Push
            NotificationService.send_push(
                user_phone=user.user_phone,
                title="🤝 Nueva Oportunidad de Negocio",
                body=f"Posible alianza con {candidate.business_name} detectada.",
                url="/networking"
            )

            ActivityLog.log(user.user_phone, "Networker Agent", f"Sinergia sugerida: {candidate.business_name}")

        except Exception as e:
            logger.error("Error enviando notificación de sinergia: %s", e)
