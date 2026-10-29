import random
from datetime import datetime, timedelta
from app import app
from core.db_models import db, Ticket, BusinessProfile, SynergyMatch

def generate_fake_history(phone):
    """Repuebla el historial de demo de un usuario. Idempotente: cada llamada deja
    exactamente 15 tickets, en lugar de acumular 15 mas sobre los existentes."""
    with app.app_context():
        Ticket.query.filter_by(user_phone=phone, concept="Gasto Demo").delete(
            synchronize_session=False
        )
        db.session.commit()

        today = datetime.now()
        last_month = today - timedelta(days=30)
        providers = ["Amazon", "Gasolinera LowCost", "Restaurante Paco", "Apple Store", "Uber"]

        for _ in range(15):
            t = Ticket(
                user_phone=phone, status='processed',
                concept="Gasto Demo", total=round(random.uniform(15, 120), 2),
                date=last_month.replace(day=random.randint(1, 28)),
                provider=random.choice(providers),
                image_path="https://placehold.co/400"
            )
            db.session.add(t)
        db.session.commit()


def demo_sinergias(phone_demo):
    """
    Crea una empresa complementaria ficticia y lanza el agente de sinergias
    para que genere la notificacion en tiempo real durante la presentacion.
    Uso: demo_sinergias('630339601')
    """
    with app.app_context():
        # 1. Limpiar matches anteriores
        SynergyMatch.query.filter(
            (SynergyMatch.user_a_phone == phone_demo) |
            (SynergyMatch.user_b_phone == phone_demo)
        ).delete(synchronize_session=False)
        db.session.commit()

        # 2. Crear (o actualizar) empresa complementaria ficticia
        PARTNER_PHONE = 'demo_partner_tfm'
        partner = BusinessProfile.query.filter_by(user_phone=PARTNER_PHONE).first()
        if not partner:
            partner = BusinessProfile(user_phone=PARTNER_PHONE)
            db.session.add(partner)

        partner.business_name   = 'CreativaMente Marketing'
        partner.email           = 'demo@creativamente.es'
        partner.password_hash   = 'demo'
        partner.static_knowledge = {
            'sector':   'Marketing Digital',
            'services': 'Campanas publicitarias, SEO, Redes sociales, Estrategia digital para startups tech',
            'schedule': 'L-V 9:00-19:00',
            'tone':     'Entusiasta',
        }
        db.session.commit()

        # 3. Ejecutar el agente de sinergias con perfil de gasto explícito
        from modules.proactive.networker import SynergyAgent
        user = BusinessProfile.query.filter_by(user_phone=phone_demo).first()
        if not user:
            print(f'ERROR: usuario {phone_demo} no encontrado')
            return

        spending_profile = (
            f"{user.business_name} opera en {user.static_knowledge.get('sector','Servicios')}. "
            f"Servicios: {user.static_knowledge.get('services', '')}. "
            "Necesita visibilidad digital y captacion de clientes para sus soluciones de IA."
        )

        agent  = SynergyAgent()
        result = agent._analyze_synergy_deep(user, spending_profile, partner)

        if not result:
            print('ERROR: el LLM no devolvio resultado')
            return

        print(f'Score LLM: {result.get("score")} | {result.get("reason")}')

        # Garantizar score >= 80 para la demo
        if result.get('score', 0) < 80:
            result['score'] = 85

        agent._save_match(user, partner, result)
        agent._notify_intro(user, partner, result['reason'])
        print(f'Notificacion de sinergia enviada a {user.business_name}!')


if __name__ == '__main__':
    import sys, os
    default_phone = os.environ.get("DEMO_PHONE", "+34600000001")
    if len(sys.argv) > 1 and sys.argv[1] == 'sinergias':
        phone = sys.argv[2] if len(sys.argv) > 2 else default_phone
        demo_sinergias(phone)
    else:
        generate_fake_history(sys.argv[1] if len(sys.argv) > 1 else default_phone)
