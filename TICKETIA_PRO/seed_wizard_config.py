"""
Configura el perfil de IAlex Solutions en la BD:
- static_knowledge (datos del wizard: sector, tono, horario, FAQ, etc.)
- system_prompt generado para el chatbot IA
Los datos coinciden con ialex_solutions_knowledge_base.pdf (raíz del proyecto),
que se sube en directo desde /documents durante la demo para mostrar el
flujo de ingesta RAG sin introducir contradicciones entre pestaña y PDF.
Uso: python seed_wizard_config.py
"""
import os
from app import app
from core.db_models import db, BusinessProfile

DEMO_PHONE = os.environ.get("DEMO_PHONE", "+34600000001")

STATIC_KNOWLEDGE = {
    "sector": "Consultoría de Inteligencia Artificial Generativa",
    "tone": "Profesional",
    "schedule": "Lunes a viernes, 9:00 a 18:00 (CET).",
    "payment_methods": (
        "Proyectos: 40% a la firma del contrato, 30% al entregable PoC, 30% al go-live. "
        "Mantenimiento y retainer: facturación mensual anticipada. "
        "Talleres: 100% prepago con 10 días de antelación. "
        "Bolsa de horas: 100% prepago, caducidad 12 meses."
    ),
    "services": (
        "Implantación de agentes IA conversacionales (GPT-4o, Claude 3.5, Gemini 1.5 Pro) con RAG. "
        "Automatización de procesos con IA generativa (Gen-AI Ops): resúmenes, clasificación de documentos, extracción de datos. "
        "Formación y talleres corporativos de IA generativa y prompt engineering. "
        "Consultoría estratégica de adopción de IA. "
        "Desarrollo de aplicaciones IA a medida (buscadores semánticos, plataformas multiagente)."
    ),
    "instructions": (
        "Somos una consultora boutique fundada en 2023, con sede en Madrid y clientes en toda la Unión Europea, "
        "especializada en IA generativa para pymes y medianas empresas. "
        "Un agente conversacional Starter puede estar operativo en 3-4 semanas; un proyecto Enterprise completo, en 3-4 meses. "
        "Ofrecemos una demo personalizada gratuita de 45 minutos. "
        "Startups y asociaciones sectoriales: 15% de descuento sobre tarifa estándar."
    ),
    "faq": (
        "¿Qué modelos de IA utilizáis? GPT-4o y GPT-4o-mini de OpenAI, Claude 3.5 Sonnet de Anthropic, Gemini 1.5 Pro de Google, y Llama 3/Mistral open-source para requisitos de privacidad estrictos.\n"
        "¿Mis datos salen de la empresa? Con modelos en la nube, bajo acuerdo DPA. Para datos sensibles, desplegamos on-premise o en nube privada del cliente.\n"
        "¿Cuánto cuesta la IA por uso (tokens)? Lo asume el cliente con su propia API key; para la mayoría de pymes, menos de 50€/mes.\n"
        "¿Necesito infraestructura técnica propia? No es imprescindible, gestionamos AWS/Azure incluido en el mantenimiento.\n"
        "¿Ofrecéis prueba gratuita? Sí, demo personalizada de 45 minutos — escribe a hola@ialex-solutions.com.\n"
        "¿Tenéis experiencia en mi sector? Retail, logística, despachos jurídicos, clínicas, marketing, inmobiliarias e industria.\n"
        "¿Qué garantías ofrecéis? 30 días post-entrega para corrección de defectos sin coste; SLA de 4h (Business) o 1h (Enterprise)."
    ),
    "return_policy": (
        "Periodo de garantía de 30 días post-entrega para corrección de defectos sin coste adicional. "
        "Contratos de mantenimiento con SLA de respuesta según nivel contratado: 4h (Business), 1h (Enterprise)."
    ),
    "delivery_time": (
        "Agente conversacional Starter: 3-4 semanas. "
        "Proyecto Enterprise con integraciones complejas: 3-4 meses."
    ),
    "warranty_info": (
        "30 días de garantía de funcionamiento post-entrega incluidos en todos los proyectos. "
        "SLA de respuesta: 4 horas en plan Business, 1 hora en plan Enterprise."
    ),
    "support_contact": "hola@ialex-solutions.com | soporte@ialex-solutions.com | +34 910 123 456 | L-V 9:00-18:00 CET",
}

SYSTEM_PROMPT = """
Eres el asistente virtual de IAlex Solutions, consultora boutique fundada en 2023 en Madrid, especializada en el diseño, desarrollo e implantación de soluciones de Inteligencia Artificial Generativa para pymes y medianas empresas en toda la Unión Europea.
Tu tono debe ser Profesional pero cercano: usa un lenguaje claro, técnico cuando sea necesario, y siempre orientado a resolver las dudas del cliente.

INFORMACION CLAVE:
- Horario: Lunes a viernes, 9:00 a 18:00 (CET).
- Contacto: hola@ialex-solutions.com (comercial) | soporte@ialex-solutions.com (soporte técnico) | +34 910 123 456.
- Dirección: Calle Gran Vía 42, 3.ª planta, 28013 Madrid.
- Servicios principales: agentes IA conversacionales con RAG, automatización de procesos (Gen-AI Ops), formación y talleres, consultoría estratégica, desarrollo de aplicaciones IA a medida.

SOBRE PRECIOS Y PLAZOS:
- Implantación de Agentes IA Conversacionales — Starter: 2.500EUR (1 agente, hasta 2 integraciones, 3 meses soporte). Business: 6.000EUR (hasta 3 agentes, integraciones ilimitadas, 6 meses soporte). Enterprise: desde 15.000EUR (multimodal, RAG avanzado, SLA 24/7). Mantenimiento mensual desde 350EUR/mes.
- Automatización de procesos: auditoría (800EUR), prototipo 2 semanas (3.500EUR), implementación completa desde 8.000EUR.
- Consultoría estratégica: sesión diagnóstico 400EUR, hoja de ruta 2.200EUR, retainer desde 1.500EUR/mes.
- Consultor senior IA: 95EUR/hora, mínimo 10 horas.
- Un agente Starter puede estar operativo en 3-4 semanas; un proyecto Enterprise, en 3-4 meses.
- Demo personalizada gratuita de 45 minutos.

GARANTIAS:
- 30 días de garantía post-entrega para corrección de defectos sin coste adicional.
- SLA de respuesta: 4 horas (Business), 1 hora (Enterprise).
- Startups y asociaciones sectoriales: 15% de descuento sobre tarifa estándar.

INSTRUCCIONES ESPECIFICAS:
- Si preguntan por un caso de uso concreto, da ejemplos reales de cómo lo hemos resuelto (LegalDoc Asesores, Moda Próxima, Grupo Logístico Serrano, Academia Digital Pro).
- Si preguntan por precios, indica los paquetes y ofrece siempre la demo gratuita de 45 minutos.
- Si no sabes algo técnico muy específico, pide que contacten por email o teléfono para hablar con un especialista.
- Nunca inventes plazos ni precios fuera de los indicados arriba.

OBJETIVO:
Responder dudas de clientes potenciales y actuales basándote ÚNICAMENTE en esta información.
Si preguntan algo que no sabes, pide amablemente que contacten vía email: hola@ialex-solutions.com
""".strip()


def seed():
    with app.app_context():
        user = BusinessProfile.query.filter_by(user_phone=DEMO_PHONE).first()
        if not user:
            print(f"[seed_wizard] ERROR: usuario {DEMO_PHONE} no encontrado. Ejecuta seed_owner.py primero.")
            return

        user.business_name    = "IAlex Solutions"
        user.static_knowledge = STATIC_KNOWLEDGE
        user.system_prompt    = SYSTEM_PROMPT
        user.features         = {**(user.features or {}), "bot_enabled": True}
        db.session.commit()
        print(f"[seed_wizard] Wizard configurado para {DEMO_PHONE}")

        # El indice RAG se deja vacio a proposito: el conocimiento del negocio ya
        # viaja al modelo dentro del system prompt (static_knowledge), asi que el
        # agente no pierde contexto. Indexar aqui los campos del wizard restaba
        # claridad a la demostracion, porque con documentos ya presentes no se
        # distingue una respuesta recuperada por RAG de una escrita en el prompt.
        # Subiendo el PDF en directo, el contraste antes/despues es inequivoco.
        print("[seed_wizard] Indice RAG vacio: el PDF se sube durante la demo.")

        print("[seed_wizard] Listo.")


if __name__ == "__main__":
    seed()
