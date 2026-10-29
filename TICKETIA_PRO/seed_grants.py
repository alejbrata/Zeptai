from app import app
from core.db_models import db, Grant

def seed_grants():
    with app.app_context():
        print("🌱 Sembrando Ayudas...")
        grants = [
            {"title": "Kit Digital III", "desc": "Ayuda digitalización.", "sector": "General", "amount": "2.000€", "link": "[https://acelerapyme.es](https://acelerapyme.es)", "deadline": "31/12/2025"},
            {"title": "Renove Hostelería", "desc": "Maquinaria eficiente.", "sector": "Restauración", "amount": "15.000€", "link": "[https://madrid.es](https://madrid.es)", "deadline": "15/03/2026"},
            {"title": "Comercio 4.0", "desc": "Venta online.", "sector": "Comercio", "amount": "3.000€", "link": "[https://camara.es](https://camara.es)", "deadline": "30/06/2026"}
        ]
        
        for g in grants:
            if not Grant.query.filter_by(title=g["title"]).first():
                db.session.add(Grant(
                    title=g["title"], description=g["desc"], sector_focus=g["sector"],
                    amount=g["amount"], link=g["link"], deadline=g["deadline"]
                ))
        db.session.commit()
        print("✅ Ayudas sembradas.")

if __name__ == "__main__":
    seed_grants()
