"""
seed_all.py — Reset completo + carga de datos demo para Demo Business S.L.

Ejecutar desde el contenedor:
    docker compose exec web python seed_all.py

O localmente desde TICKETIA_PRO/:
    python seed_all.py

Pasos que ejecuta en orden:
  1. Elimina y recrea todas las tablas (BD limpia)
  2. Crea el usuario demo con plan PRO_FULL
  3. Configura el wizard IA (sector, tono, FAQ, garantías, etc.)
  4. Deja la base de conocimiento vacia: el PDF se sube en directo en la demo
  5. Carga 15 tickets de historial de gastos

Nota: el PDF de knowledge base NO se precarga intencionalmente.
Se sube en directo durante la demo desde /documents para mostrar
el flujo de ingesta RAG en tiempo real.
"""
import os
import sys

# ── Configuración ──────────────────────────────────────────────────────────────
DEMO_PHONE    = os.environ.get("DEMO_PHONE",         "+34600000001")
DEMO_EMAIL    = os.environ.get("DEMO_EMAIL",         "admin@demo.com")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD",      "demo1234")
DEMO_BUSINESS = os.environ.get("DEMO_BUSINESS_NAME", "IAlex Solutions")


def step(n, label):
    print(f"\n[{n}/5] {label}")


def run():
    from app import app
    from core.db_models import db, BusinessProfile
    from werkzeug.security import generate_password_hash

    with app.app_context():

        # ── 1. Reset BD ────────────────────────────────────────────────────────
        step(1, "Limpiando base de datos...")
        db.drop_all()
        db.create_all()
        print("     OK")

        # ── 2. Crear usuario demo ──────────────────────────────────────────────
        step(2, f"Creando usuario: {DEMO_EMAIL}")
        user = BusinessProfile(
            user_phone=DEMO_PHONE,
            email=DEMO_EMAIL,
            password_hash=generate_password_hash(DEMO_PASSWORD),
            business_name=DEMO_BUSINESS,
            plan_tier='PRO_FULL',
            features={"tickets_allowed": True, "bot_enabled": True, "dashboard_access": True},
            active_agents=[
                "grant_hunter", "networker", "business_health",
                "admin_redactor", "post_sales_service",
            ],
            agent_config={"post_sales_service": {"enable_feedback": False, "enable_reactivation": True}},
        )
        db.session.add(user)
        db.session.commit()
        print(f"     OK — login: {DEMO_EMAIL} / {DEMO_PASSWORD}")

        # ── 3. Configurar wizard IA ────────────────────────────────────────────
        step(3, "Configurando wizard IA (sector, tono, FAQ, garantias...)")
        import seed_wizard_config as wiz
        wiz.DEMO_PHONE = DEMO_PHONE
        wiz.seed()

        # ── 4. Base de conocimiento ────────────────────────────────────────────
        # Vacia a proposito: seed_wizard_config.seed() no indexa nada en pgvector

        # ── 5. Tickets ─────────────────────────────────────────────────────────
        # Ninguno: la demo empieza con la lista vacia y se llena subiendo tickets
        # reales por OCR, que quedan con su imagen enlazada y suman sobre datos
        # verificables. El BusinessCoachAgent necesita al menos un ticket para
        # emitir informe, asi que se sube uno antes de lanzarlo.
        step(5, "Tickets: ninguno (se suben en directo por OCR)...")
        print("     OK")

    print("\n" + "="*50)
    print("Demo lista.")
    print(f"  URL      : http://localhost:5000")
    print(f"  Email    : {DEMO_EMAIL}")
    print(f"  Password : {DEMO_PASSWORD}")
    print(f"  Base de conocimiento: vacia — sube el PDF en directo desde /knowledge")
    print("="*50)


if __name__ == "__main__":
    run()
