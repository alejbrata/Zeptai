from app import app
from core.db_models import db, BusinessProfile
from werkzeug.security import generate_password_hash
import os

# Configuración del usuario demo
# Personaliza estas variables o pásalas por entorno
MI_TELEFONO = os.environ.get("DEMO_PHONE", "+34600000001")
NOMBRE_NEGOCIO = os.environ.get("DEMO_BUSINESS_NAME", "IAlex Solutions")

def seed():
    with app.app_context():
        print(f"[seed] DB: {app.config['SQLALCHEMY_DATABASE_URI']}")

        db.create_all()
        print("[seed] Tablas creadas.")

        existing_user = BusinessProfile.query.filter_by(user_phone=MI_TELEFONO).first()

        if not existing_user:
            new_profile = BusinessProfile(
                user_phone=MI_TELEFONO,
                email=os.environ.get("DEMO_EMAIL", "admin@demo.com"),
                password_hash=generate_password_hash(os.environ.get("DEMO_PASSWORD", "demo1234")),
                business_name=NOMBRE_NEGOCIO,
                plan_tier='PRO_FULL',
                features={"tickets_allowed": True, "bot_enabled": True, "dashboard_access": True}
            )
            db.session.add(new_profile)
            db.session.commit()
            print(f"[seed] Usuario creado: {MI_TELEFONO} / {os.environ.get('DEMO_EMAIL', 'admin@demo.com')}")
        else:
            print(f"[seed] Usuario ya existe: {MI_TELEFONO}")

        print("[seed] Listo.")

if __name__ == "__main__":
    seed()
