import logging
from core.db_models import BusinessProfile
from modules.proactive.grant_hunter import GrantHunterAgent
from modules.proactive.networker import SynergyAgent
from modules.proactive.business_health import BusinessCoachAgent
from modules.proactive.post_sales import PostSalesAgent

logger = logging.getLogger(__name__)


def run_daily_tasks():
    """
    Punto de entrada para el cron job diario.
    Invocado por run_scheduler.py (APScheduler) o manualmente.
    """
    logger.info("Ejecutando tareas proactivas diarias...")

    users = BusinessProfile.query.all()

    for user in users:
        try:
            active_list = user.active_agents or []
            logger.info("Procesando usuario %s (agentes activos: %s)",
                        user.business_name, active_list)

            if "grant_hunter" in active_list:
                logger.info("  -> Grant Hunter para %s", user.business_name)
                try:
                    GrantHunterAgent().check_new_grants(user)
                except Exception as e:
                    logger.error("  Error Grant Hunter: %s", e)

            if "business_health" in active_list:
                logger.info("  -> Business Coach para %s", user.business_name)
                try:
                    BusinessCoachAgent().run_daily_analysis(user)
                except Exception as e:
                    logger.error("  Error Business Coach: %s", e)

            if "networker" in active_list:
                logger.info("  -> Networker para %s", user.business_name)
                try:
                    SynergyAgent().run_daily_networking(user)
                except Exception as e:
                    logger.error("  Error Networker: %s", e)

            if "admin_redactor" in active_list:
                logger.info("  -> Admin Redactor para %s (sin tarea diaria activa)", user.business_name)

            if "post_sales_service" in active_list:
                logger.info("  -> Post-Sales para %s", user.business_name)
                try:
                    PostSalesAgent().run_daily_checks(user)
                except Exception as e:
                    logger.error("  Error Post-Sales: %s", e)

        except Exception as e:
            logger.error("Error procesando tareas para usuario %s: %s", user.id, e)

    logger.info("Tareas proactivas diarias finalizadas.")
