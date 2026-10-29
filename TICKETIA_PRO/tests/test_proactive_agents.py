import os
import sys
import unittest
from datetime import datetime, timedelta

# MOCK ENV VARS BEFORE IMPORT
os.environ['OPENAI_API_KEY'] = 'sk-fake-key-for-testing'

# Add path to sys to import modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db
from core.db_models import BusinessProfile, Grant, Ticket, Notification, SynergyMatch, Incident
from modules.proactive.grant_hunter import GrantHunterAgent
from modules.proactive.networker import SynergyAgent
from modules.proactive.business_health import BusinessCoachAgent
from modules.proactive.post_sales import PostSalesAgent

class TestZeptaiProactiveAgents(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:' # In-memory DB
        self.app = app.test_client()
        with app.app_context():
            db.drop_all()
            db.create_all()

            # Create Dummy User
            self.user = BusinessProfile(
                user_phone="123456789",
                email="test@zeptai.com",
                business_name="Panadería Pepe",
                static_knowledge={"sector": "Panadería", "location": "Madrid"}
            )
            db.session.add(self.user)
            db.session.commit()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_grant_hunter(self):
        with app.app_context():
            # Reload user in current session
            user = db.session.merge(self.user)
            
            # 1. Create Grant
            grant = Grant(
                title="Ayuda Panaderías 2026",
                description="Subvención hornos eficientes",
                sector_focus="Panadería",
                amount="5000€",
                deadline="31/12/2026"
            )
            db.session.add(grant)
            db.session.commit()
            
            # 2. Run Agent
            agent = GrantHunterAgent()
            # Mock OpenAI to avoid costs/network issues in test
            agent.client.chat.completions.create = lambda **kwargs: type('obj', (object,), {'choices': [type('obj', (object,), {'message': type('obj', (object,), {'content': 'SI' if 'SI' in str(kwargs) else 'Mensaje persuasivo'})})]})()
            
            agent.check_new_grants(user)
            
            # 3. Check Notification
            notif = Notification.query.filter_by(user_phone=user.user_phone, type='grant').first()
            self.assertIsNotNone(notif)
            self.assertIn("Nueva Ayuda", notif.title)
            print("OK Grant Hunter Test Passed")

    def test_networker_synergy(self):
        with app.app_context():
            # Reload user
            user = db.session.merge(self.user)
            
            # 1. Create Partner
            partner = BusinessProfile(
                user_phone="987654321",
                email="partner@harinas.com",
                business_name="Harinas Martínez",
                static_knowledge={"sector": "Proveedor Harina", "services": "Venta al por mayor"}
            )
            db.session.add(partner)
            
            # 2. Add Spending History to User
            t1 = Ticket(user_phone=user.user_phone, total=100.0, concept="Saco Harina", date=datetime.now())
            db.session.add(t1)
            db.session.commit()
            
            # 3. Run Agent
            agent = SynergyAgent()
            # Mock OpenAI
            agent.openai.chat.completions.create = lambda **kwargs: type('obj', (object,), {'choices': [type('obj', (object,), {'message': type('obj', (object,), {'content': '{"score": 85, "reason": "Match perfecto"}'})})]})()
            
            agent.run_daily_networking(user)
            
            # 4. Check Match & Notification
            match = SynergyMatch.query.first()
            self.assertIsNotNone(match)
            notif = Notification.query.filter_by(user_phone=user.user_phone, type='networking').first()
            self.assertIsNotNone(notif)
            print("OK Networker Test Passed")

    def test_business_coach_projection(self):
        with app.app_context():
            # Reload user
            user = db.session.merge(self.user)
            
            # 1. Add Expenses (Last Month vs This Month)
            today = datetime.now()
            last_month = today.replace(day=1) - timedelta(days=10)
            
            # Last Month: 1000€
            t_last = Ticket(user_phone=user.user_phone, total=1000.0, date=last_month)
            # This Month: 1500€ (Spending more!)
            t_curr = Ticket(user_phone=user.user_phone, total=1500.0, date=today)
            
            db.session.add(t_last)
            db.session.add(t_curr)
            db.session.commit()
            
            # 2. Run Coach
            agent = BusinessCoachAgent()
            # Mock OpenAI
            agent.client.chat.completions.create = lambda **kwargs: type('obj', (object,), {'choices': [type('obj', (object,), {'message': type('obj', (object,), {'content': 'Estás gastando mucho 📉'})})]})()
            
            agent.run_daily_analysis(user)
            
            # 3. Check Alert
            notif = Notification.query.filter_by(user_phone=user.user_phone).first()
            self.assertIsNotNone(notif)
            # Should be alert because +50% spending
            self.assertEqual(notif.type, 'alert') 
            print("OK Business Coach Test Passed")

    def test_post_sales_complaint(self):
        with app.app_context():
            # Reload user
            user = db.session.merge(self.user)
            
            # 1. Simulate Complaint
            msg = "Estoy muy enfadado, quiero devolver esto ya y que me devolváis el dinero."
            
            agent = PostSalesAgent()
            # Mock OpenAI intent detection
            agent.client.chat.completions.create = lambda **kwargs: type('obj', (object,), {'choices': [type('obj', (object,), {'message': type('obj', (object,), {'content': '{"intent": "COMPLAINT", "sentiment": "angry"}'})})]})()
            
            response, media = agent.handle_inquiry(user.user_phone, msg, user)
            
            # 2. Check Incident & Alert to Owner
            incident = Incident.query.filter_by(type='Queja').first()
            self.assertIsNotNone(incident)
            
            # Alert to owner (self.user is owner here)
            notif = Notification.query.filter_by(user_phone=user.user_phone, type='alert').first()
            self.assertIsNotNone(notif)
            self.assertIn("CLIENTE ENFADADO", notif.title)
            print("OK Post-Sales Complaint Test Passed")

if __name__ == '__main__':
    unittest.main()
