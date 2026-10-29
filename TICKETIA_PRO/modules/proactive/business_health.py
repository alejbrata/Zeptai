import logging
from datetime import datetime, timedelta
from sqlalchemy import func
from core.db_models import db, Ticket, ActivityLog
from modules.services.notification import NotificationService

logger = logging.getLogger(__name__)


class BusinessCoachAgent:
    def __init__(self):
        from core.clients import get_openai_client
        self.client = get_openai_client()

    def run_daily_analysis(self, user):
        """
        Analiza la salud financiera y genera un informe diario (In-App + Web Push).
        """
        logger.info("Coach: Analizando finanzas de %s", user.business_name)

        # 1. Obtener Datos Financieros
        stats = self._get_financial_stats(user.user_phone)

        if stats['current_month'] == 0 and stats['last_month_total'] == 0:
            return

        # 2. Generar Insight con IA
        insight = self._generate_insight(user, stats)

        # 3. Notificación In-App
        trend_emoji = "📈" if stats['diff_percent'] > 0 else "📉"
        if stats['diff_percent'] == 0: trend_emoji = "➡️"

        title = f"Resumen Diario: {trend_emoji} {stats['diff_percent']}% vs mes pasado"

        NotificationService.send_in_app(
            user_phone=user.user_phone,
            title=title,
            message=f"{insight}\n\nGasto hoy: {stats['current_month']}€ (Proyección: {stats['projection']}€)",
            type="alert" if stats['is_alert'] else "info",
            link="/dashboard"
        )

        # 4. Web Push (si el usuario tiene suscripción registrada)
        push_msg = f"📊 Coach Financiero: {insight}"
        NotificationService.send_push(user.user_phone, title, push_msg, url="/dashboard")

        ActivityLog.log(user.user_phone, "Business Coach", "Informe diario generado")

    def _get_financial_stats(self, phone):
        today = datetime.now()
        start_current = today.replace(day=1, hour=0, minute=0, second=0)

        first_day_last_month = (start_current - timedelta(days=1)).replace(day=1)

        current_total = db.session.query(func.sum(Ticket.total)).filter(
            Ticket.user_phone == phone,
            Ticket.date >= start_current
        ).scalar() or 0.0

        last_month_total = db.session.query(func.sum(Ticket.total)).filter(
            Ticket.user_phone == phone,
            Ticket.date >= first_day_last_month,
            Ticket.date < start_current
        ).scalar() or 0.0

        days_passed = today.day
        days_in_month = (start_current.replace(month=start_current.month % 12 + 1) - timedelta(days=1)).day

        projection = 0
        if days_passed > 0:
            projection = (current_total / days_passed) * days_in_month

        diff_percent = 0
        if last_month_total > 0:
            diff_percent = ((projection - last_month_total) / last_month_total) * 100

        return {
            "current_month": round(current_total, 2),
            "last_month_total": round(last_month_total, 2),
            "projection": round(projection, 2),
            "diff_percent": round(diff_percent, 1),
            "is_alert": diff_percent > 20
        }

    def _generate_insight(self, user, stats):
        prompt = f"""
        Eres el Director Financiero de {user.business_name}.
        Datos:
        - Gasto acumulado: {stats['current_month']}€
        - Proyección fin de mes: {stats['projection']}€
        - Mes pasado total: {stats['last_month_total']}€
        - Variación Proyectada: {stats['diff_percent']}%

        Dame 1 frase (max 20 palabras) analizando la situación.
        Si la variación es > 20%, sé alarmista. Si es < 0%, felicita por el ahorro.
        Usa emojis.
        """
        try:
            from core.llm_tracker import timed_track
            with timed_track(user.user_phone, "gpt-4o", "coach_insight") as t:
                resp = self.client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=50,
                    temperature=0.7
                )
                t["response"] = resp
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("Coach: error generando insight para %s: %s", user.business_name, e)
            return "📊 Tus gastos siguen su curso normal. ¡Sigue así!"
