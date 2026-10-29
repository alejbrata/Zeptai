import os
import json
import logging
from datetime import datetime
from fpdf import FPDF
from core.db_models import db, Incident, ActivityLog
from core.config import Config
from modules.services.notification import NotificationService

logger = logging.getLogger(__name__)


class PostSalesAgent:
    def __init__(self):
        from core.clients import get_openai_client
        self.client = get_openai_client()

    def handle_inquiry(self, user_phone, user_message, owner_profile, channel=None):
        """
        Analiza el mensaje y ejecuta acciones respetando la POLITICA DE EMPRESA.
        """
        # 0. Cargar configuración de seguridad (agent_config) y política del wizard (static_knowledge)
        config = owner_profile.agent_config or {}
        ps_config = config.get("post_sales", {})

        forbidden_items    = ps_config.get("forbidden_items", [])
        exchange_config    = ps_config.get("exchange_policy", {})
        exchange_url       = exchange_config.get("url")
        exchange_instructions = exchange_config.get("instructions")
        allow_refunds      = ps_config.get("allow_refunds", False)

        sk = owner_profile.static_knowledge or {}
        if isinstance(sk, str):
            try:
                sk = json.loads(sk)
            except Exception:
                sk = {}

        faq             = sk.get("faq", "")
        return_policy   = sk.get("return_policy", "")
        support_contact = sk.get("support_contact", "")
        delivery_time   = sk.get("delivery_time", "")
        warranty_info   = sk.get("warranty_info", "")

        # Contexto de empresa para los prompts del agente
        cs_context = f"""
Política de devoluciones: {return_policy or 'No especificada'}
Tiempo de entrega/respuesta: {delivery_time or 'No especificado'}
Garantía: {warranty_info or 'No especificada'}
Contacto de soporte: {support_contact or 'No especificado'}
Preguntas frecuentes: {faq or 'No especificadas'}
""".strip()

        # 1. Análisis de intención + EXTRACCIÓN DE PRODUCTO
        tools_prompt = f"""
        Eres un experto en Atención al Cliente de {owner_profile.business_name}. Tu trabajo es CLASIFICAR el mensaje y detectar el PRODUCTO.

        FORMATO RESPUESTA: JSON
        {{
            "intent": "RETOUR" | "EXCHANGE" | "STATUS" | "COMPLAINT" | "WARRANTY" | "GENERAL",
            "product": "nombre del producto identificado o null",
            "sentiment": "positive" | "neutral" | "angry"
        }}

        Reglas:
        - Si quiere devolver algo (reembolso): RETOUR
        - Si quiere cambiar talla o color: EXCHANGE
        - Si pregunta dónde está su pedido: STATUS
        - Si pregunta por garantía: WARRANTY
        - Si está enfadado/queja: COMPLAINT
        """
        try:
            from core.llm_tracker import timed_track
            with timed_track(owner_profile.user_phone, "gpt-4o", "postsales_intent") as t:
                response = self.client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": tools_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=150
                )
                t["response"] = response
            data = json.loads(response.choices[0].message.content)
            intent = data.get("intent", "GENERAL")
            product = data.get("product", "") or ""
            sentiment = data.get("sentiment", "neutral")
            
        except Exception as e:
            logger.warning("PostSales: error clasificando intención: %s", e)
            intent = "GENERAL"
            product = ""
            sentiment = "neutral"

        # 2. Ejecutar Acción con FILTROS DE SEGURIDAD
        response_text = ""
        media_url = None
        
        if "RETOUR" in intent:
            # 🛡️ FILTRO 1: Artículos Prohibidos
            is_forbidden = any(bad_item in product.lower() for bad_item in forbidden_items)

            if is_forbidden:
                response_text = f"🚫 Lo siento, pero por política de empresa no admitimos devoluciones de '{product}' (artículo no retornable)."
                ActivityLog.log(user_phone, "Post-Venta", f"🛑 Devolución DENEGADA: {product}")

            # 🛡️ FILTRO 2: Permisos de Reembolso Automático
            elif not allow_refunds:
                policy_note = f"\n\nNuestra política: {return_policy}" if return_policy else ""
                contact_note = f"\n\nSi tienes dudas: {support_contact}" if support_contact else ""
                response_text = f"📝 He registrado tu solicitud de devolución. Un supervisor revisará el caso y te contactará en breve.{policy_note}{contact_note}"
                self._log_incident(user_phone, "PENDING", "Solicitud Devolución (Requiere Aprobación)", user_message)
                NotificationService.send_in_app(
                    user_phone=owner_profile.user_phone,
                    title="⚠️ Solicitud de Devolución",
                    message=f"El cliente quiere devolver '{product}'. Requiere aprobación manual.",
                    type="alert",
                    link="/incidents"
                )

            else:
                # ✅ APROBADO: Generar etiqueta
                fake_order_id = f"PED-{int(datetime.now().timestamp())}"
                relative_path = self._generate_return_label(user_phone, fake_order_id, return_policy)
                host = os.environ.get('PUBLIC_URL', 'http://localhost:5000').rstrip('/')
                media_url = f"{host}{relative_path}"
                response_text = f"✅ He tramitado tu devolución (ref. {fake_order_id}). Aquí tienes tu etiqueta de envío. 📦"
                self._log_incident(user_phone, fake_order_id, "Devolución Aprobada", user_message)
                ActivityLog.log(user_phone, "Post-Venta", f"Generada etiqueta devolución {fake_order_id}")

        elif "EXCHANGE" in intent:
            if exchange_url:
                response_text = f"🔄 Para gestionar un cambio, accede a nuestro portal: {exchange_url}"
            elif exchange_instructions:
                response_text = f"🔄 {exchange_instructions}"
            else:
                response_text = "🔄 Para cambiar un artículo, gestiona la devolución y realiza un nuevo pedido."

        elif "STATUS" in intent:
            time_note = delivery_time or "el plazo habitual"
            response_text = f"🚚 Tu pedido está siendo procesado. El tiempo estimado de entrega es {time_note}."
            if support_contact:
                response_text += f" Si necesitas más detalles, contacta con nosotros en {support_contact}."

        elif "WARRANTY" in intent:
            if warranty_info:
                response_text = f"🛡️ Información de garantía: {warranty_info}"
            else:
                response_text = "🛡️ Todos nuestros productos tienen la garantía legal de 2 años."
            if support_contact:
                response_text += f" Para gestionar una incidencia de garantía contacta en {support_contact}."
            ActivityLog.log(user_phone, "Post-Venta", "Consulta de garantía atendida")

        elif "COMPLAINT" in intent or sentiment == "angry":
            self._log_incident(user_phone, "N/A", "Queja", user_message)
            NotificationService.send_in_app(
                user_phone=owner_profile.user_phone,
                title="🚨 CLIENTE ENFADADO",
                message=f"Queja recibida: '{user_message[:60]}...'. Atender URGENTE.",
                type="alert"
            )
            try:
                prompt = (
                    f"Actúas como Manager de Experiencia de Cliente de {owner_profile.business_name}. "
                    f"El cliente está enfadado. Calma la situación sin prometer dinero. Sé empático y profesional.\n\n"
                    f"Contexto de la empresa:\n{cs_context}"
                )
                from core.llm_tracker import timed_track
                with timed_track(owner_profile.user_phone, "gpt-4o", "postsales_complaint") as t:
                    response = self.client.chat.completions.create(
                        model="gpt-4o",
                        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_message}]
                    )
                    t["response"] = response
                response_text = response.choices[0].message.content
            except Exception as e:
                logger.warning("PostSales: error generando respuesta para queja: %s", e)
                response_text = "Entiendo tu malestar. He elevado tu incidencia con prioridad ALTA a dirección."
            ActivityLog.log(user_phone, "Post-Venta", "🔴 Queja: IA calma al cliente + Alerta dueño")

        else:
            # Respuesta general usando FAQ y contexto configurado
            prompt = (
                f"Eres el asistente de atención al cliente de {owner_profile.business_name}. "
                f"Responde de forma breve y útil basándote en el siguiente contexto de la empresa:\n\n{cs_context}"
            )
            try:
                from core.llm_tracker import timed_track
                with timed_track(owner_profile.user_phone, "gpt-4o", "postsales_answer") as t:
                    response = self.client.chat.completions.create(
                        model="gpt-4o",
                        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_message}]
                    )
                    t["response"] = response
                response_text = response.choices[0].message.content
            except Exception as e:
                logger.warning("PostSales: error en respuesta general: %s", e)
                response_text = "Gracias por contactarnos. En breve un agente atenderá tu consulta."

        return response_text, media_url

    def _generate_return_label(self, phone, order_id, return_policy=""):
        """Genera un PDF simple simulando una etiqueta de envío."""
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", "B", 16)
        pdf.cell(200, 10, txt="ETIQUETA DE DEVOLUCION", ln=1, align="C")
        pdf.ln(10)
        pdf.set_font("Arial", size=12)
        pdf.cell(200, 10, txt=f"Ref: {order_id}", ln=1)
        pdf.cell(200, 10, txt=f"Cliente: {phone}", ln=1)
        if return_policy:
            pdf.ln(5)
            pdf.set_font("Arial", "I", 10)
            pdf.multi_cell(0, 8, txt=f"Politica de devoluciones: {return_policy}")
        
        filename = f"return_label_{order_id}.pdf"
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        save_path = os.path.join(base_dir, "static", "generated_docs", filename)
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        pdf.output(save_path)
        
        return f"/static/generated_docs/{filename}"

    def _log_incident(self, phone, order, type, desc):
        try:
            inc = Incident(user_phone=phone, order_id=order, type=type, description=desc)
            db.session.add(inc)
            db.session.commit()
        except Exception as e:
            print(f"Error logging incident: {e}")
