import os
import json
import logging
import time
import base64
from fpdf import FPDF, XPos, YPos
from datetime import datetime
from core.llm_tracker import track as _track

logger = logging.getLogger(__name__)

class AdminAssistantAgent:
    def __init__(self):
        from core.clients import get_openai_client
        self.openai = get_openai_client()

    def classify_image_intent(self, image_url, user_text=""):
        """
        Determina si la imagen es un GASTO (Ticket/Factura recibida)
        o un BORRADOR (Nota/Servilleta para crear un documento).
        """
        try:
            logger.info("Clasificando imagen: %s...", image_url[:15])
            
            # --- PREPARAR IMAGEN (Local vs URL) ---
            # URLs externas se pasan directamente a la Vision API de OpenAI (OpenAI
            # hace el fetch desde sus servidores, no el nuestro — sin riesgo SSRF).
            # Rutas locales se convierten a base64 para evitar exponer paths internos.
            image_content = []
            if image_url.startswith(('http://', 'https://')):
                image_content = [{"type": "image_url", "image_url": {"url": image_url}}]
            else:
                # Local Path → base64
                try:
                    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    rel_path = image_url.lstrip('/')
                    local_path = os.path.join(base_dir, rel_path)

                    with open(local_path, "rb") as image_file:
                        base64_image = base64.b64encode(image_file.read()).decode('utf-8')
                        image_content = [{
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }]
                except Exception as e:
                    logger.warning("Error cargando imagen local para clasificación: %s", e)
                    image_content = [{"type": "image_url", "image_url": {"url": image_url}}]

            prompt = f"""
            Eres un experto clasificador visual para "Zeptai".
            Clasifica la imagen en UNA de dos categorías:
            
            A) 'receipt': SOLO si ves un TICKET TÉRMICO DE CAJA (supermercado, restaurante) o una FACTURA IMPRESA FORMAL. Debe parecer un documento final de pago.
            
            B) 'draft': Para TODO lo demás.
            - Notas manuscritas (papel, cuaderno, servilleta).
            - Dibujos o esquemas con precios.
            - Hojas de pedido manuales.
            - Pantallas de ordenador o fotos de otros dispositivos.
            - Cualquier cosa que NO sea claramente un ticket de compra final.
            
            CONTEXTO EXTRA: "{user_text}"
            (Si el usuario pide presupuesto, cotización, o dice "hazme", ES DRAFT).
            
            SI NO ESTÁS 100% SEGURO QUE ES UN TICKET TÉRMICO/OFICIAL -> CLASIFICA COMO 'draft'.
            
            Responde JSON estricto: {{"type": "receipt" | "draft"}}
            """

            _t0 = time.time()
            response = self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}] + image_content
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=50
            )
            _track(None, "gpt-4o", "image_classify_intent",
                   response, int((time.time() - _t0) * 1000))
            data = json.loads(response.choices[0].message.content)
            result = data.get('type', 'receipt')
            logger.info("Clasificación imagen: %s | Context: %s", result.upper(), user_text)
            return result
        except Exception as e:
            logger.error("Error clasificando imagen: %s", e)
            return "receipt"

    def process_image_request(self, image_url, user_context):
        """
        Orquesta el flujo: Imagen -> Datos Estructurados -> PDF -> Path
        """
        logger.info("AdminRedactor: procesando imagen %s...", image_url[:20])
        
        # 1. Vision API: Extraer datos (Pasamos contexto sectorial)
        extracted_data = self._analyze_image_with_vision(image_url, extra_context=user_context)
        if not extracted_data:
            return None
            
        # 2. Generar PDF
        pdf_path = self._generate_professional_pdf(extracted_data, user_context)
        return pdf_path

    def generate_proposal_from_data(self, data, user_context):
        """
        Genera PDF directamente desde datos estructurados (sin imagen).
        Útil para comandos de voz/texto.
        """
        logger.info("AdminRedactor: generando PDF desde datos texto")
        return self._generate_professional_pdf(data, user_context)

    def generate_invoice_pdf(self, data, user_context):
        """
        Genera una factura legal en PDF a partir de datos estructurados.
        Requiere data['invoice_number'] (e.g. 'F-2026-001').
        Devuelve (file_path, subtotal, vat_amount, total_amount) o (None, 0, 0, 0).
        """
        logger.info("AdminRedactor: generando factura %s", data.get('invoice_number', '?'))
        return self._generate_professional_pdf(data, user_context, doc_title="FACTURA")

    def _analyze_image_with_vision(self, image_url, extra_context={}):
        """
        Analiza una imagen usando GPT-4o Vision + Contexto Sectorial Dinámico (5 Arquetipos).
        """
        # 1. Cargar "Cerebro Sectorial" (Taxonomía Universal)
        sector = str(extra_context.get('sector', 'General')).lower()
        knowledge_file = "type_services.json" # Default seguro (Consultoría/General)
        
        # Mapeo de Palabras Clave a Arquetipos
        keywords = {
            "type_construction.json": ['obra', 'refor', 'const', 'albañ', 'carpin', 'fontan', 'electr', 'pint', 'insta', 'mader', 'metal'],
            "type_retail.json": ['tienda', 'vent', 'comer', 'shop', 'moda', 'ropa', 'alim', 'frut', 'super', 'panad', 'carn', 'pesc', 'flor'],
            "type_hospitality.json": ['restaur', 'bar', 'cafet', 'hotel', 'hostal', 'cater', 'event', 'boda', 'turis', 'viaj'],
            "type_repair.json": ['taller', 'mecan', 'repar', 'sat', 'tecni', 'auto', 'coche', 'moto', 'bici', 'infor', 'movil'],
            "type_services.json": ['serv', 'consul', 'abog', 'asesor', 'gestor', 'market', 'diseñ', 'salud', 'dent', 'fisio', 'medic', 'vet']
        }
        
        # Algoritmo de clasificación simple
        for file, keys in keywords.items():
            if any(k in sector for k in keys):
                knowledge_file = file
                break
            
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            k_path = os.path.join(base_dir, "modules", "knowledge", knowledge_file)
            with open(k_path, 'r', encoding='utf-8') as f:
                sector_knowledge = f.read()
        except Exception as e:
            logger.warning("Knowledge file error (%s), usando genérico: %s", knowledge_file, e)
            sector_knowledge = "Actúa como experto administrativo general."

        # 2. Preparar Imagen
        image_content = []
        if image_url.startswith(('http://', 'https://')):
            image_content = [{"type": "image_url", "image_url": {"url": image_url}}]
        else:
            # Local logic...
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            rel_path = image_url.lstrip('/') 
            local_path = os.path.join(base_dir, rel_path)
            try:
                with open(local_path, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode('utf-8')
                    image_content = [{
                        "type": "image_url", 
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                    }]
            except FileNotFoundError:
                return None

        # 3. Prompt Dinámico (RAG Ligero)
        prompt = f"""
        ACTUAR COMO: Experto administrativo en el sector: {sector}.
        CONOCIMIENTO ESPECÍFICO DEL SECTOR:
        {sector_knowledge}
        
        TAREA: Extraer datos de este documento (borrador, nota o foto) para crear un presupuesto formal.
        
        REGLAS DE EXTRACCIÓN CRÍTICAS:
        1. Identifica items y cantidades usando el vocabulario del sector (ej: m2, kg, horas).
        2. Si no hay precio unitario pero sí total, calcúlalo.
        3. "masiva" o "+iva" = Precio base (impuestos excluidos).
        4. "iva incluido" = Desglosar hacia atrás.
        
        OUTPUT JSON:
        {{
            "client_name": "Nombre inferido o 'Cliente Contado'",
            "date": "dd/mm/yyyy",
            "items": [
                {{
                    "desc": "Descripción técnica detallada",
                    "qty": 1,
                    "unit_price": 100.00,
                    "total_line": 100.00
                }}
            ],
            "notes": "Notas sobre pago o plazos"
        }}
        """

        try:
            _t0 = time.time()
            response = self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}] + image_content
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=1000
            )
            _track(extra_context.get('phone'), "gpt-4o", "image_extract_data",
                   response, int((time.time() - _t0) * 1000))
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error("Error Vision API: %s", e)
            return None

    def _generate_professional_pdf(self, data, user_context, doc_title="PRESUPUESTO"):
        try:
            pdf = FPDF()
            pdf.add_page()

            # --- VAT Logic ---
            sector = user_context.get('sector', 'Servicios').lower()

            # Helper to sanitize text for FPDF (Latin-1)
            def clean_text(text):
                if not text: return ""
                # Replace common symbols not in Latin-1
                text = str(text).replace("€", "EUR").replace("–", "-").replace("—", "-")
                # Encode to Latin-1, replacing errors with ?
                try:
                    return text.encode('latin-1', 'replace').decode('latin-1')
                except Exception:
                    return str(text)

            # Default logic
            vat_rate = 0.21
            if 'restauración' in sector or 'restauracion' in sector:
                vat_rate = 0.10
            elif 'salud' in sector:
                vat_rate = 0.04 # Or 0.0 depending on service, generic 4 for now
                
            # --- Colors & Fonts ---
            pdf.set_text_color(50, 50, 50)
            
            # --- Header ---
            # Company Name (Left)
            # Match Header Company Name with clean_text
            pdf.set_font("Helvetica", 'B', 20)
            pdf.cell(100, 10, clean_text(user_context.get('business_name', 'Mi Empresa').upper()),
                     new_x=XPos.RIGHT, new_y=YPos.TOP)

            # Document Title (Right)
            pdf.set_font("Helvetica", 'B', 24)
            pdf.set_text_color(100, 100, 100)
            pdf.cell(0, 10, doc_title, align='R',
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            pdf.ln(5)

            # --- Info Grid ---
            pdf.set_text_color(50, 50, 50)
            pdf.set_font("Helvetica", '', 10)

            # Left Column (Issuer)
            extra = user_context.get('extra_info', {})
            issuer_info = [
                f"Tel: {user_context.get('phone', '')}",
                f"Email: {user_context.get('email', '')}",
                f"NIF: {extra.get('nif', '')}",
                f"Sede: {extra.get('address', '')}"
            ]

            top_y = pdf.get_y()
            pdf.set_font("Helvetica", 'B', 10)
            pdf.cell(90, 5, "DE:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", '', 10)
            for line in issuer_info:
                if line.split(": ")[1]:
                    pdf.cell(90, 5, clean_text(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            # Right Column (Client & Date)
            pdf.set_xy(110, top_y)
            pdf.set_font("Helvetica", 'B', 10)
            pdf.cell(0, 5, "PARA:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_xy(110, pdf.get_y())
            pdf.set_font("Helvetica", '', 10)
            pdf.cell(0, 5, f"Cliente: {clean_text(data.get('client_name', 'Cliente Contado'))}",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if data.get('client_nif'):
                pdf.set_xy(110, pdf.get_y())
                pdf.cell(0, 5, f"NIF/CIF: {clean_text(data['client_nif'])}",
                         new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            pdf.set_xy(110, pdf.get_y() + 2)
            pdf.cell(0, 5, f"Fecha: {clean_text(data.get('date') or datetime.now().strftime('%d/%m/%Y'))}",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if doc_title == "FACTURA":
                ref_label = f"Num. Factura: {clean_text(data.get('invoice_number', ''))}"
            else:
                ref_label = f"Ref: B-{int(datetime.now().timestamp())}"
            pdf.cell(0, 5, ref_label, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            pdf.ln(20)

            # --- Table Header ---
            pdf.set_fill_color(240, 240, 240)
            pdf.set_font("Helvetica", 'B', 10)
            pdf.cell(100, 10, "DESCRIPCIÓN", border=0, align='L', fill=True,
                     new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(20, 10, "CANT", border=0, align='C', fill=True,
                     new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(30, 10, "PRECIO UNIT", border=0, align='R', fill=True,
                     new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(30, 10, "TOTAL", border=0, align='R', fill=True,
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            # --- Table Body ---
            pdf.set_font("Helvetica", '', 10)

            subtotal = 0.0

            for item in data.get('items', []):
                desc = str(item.get('desc', 'Item'))
                qty = float(item.get('qty', 1))
                # Support both keys: 'unit_price' (Vision) and 'price' (Tools Schema)
                price = float(item.get('unit_price') or item.get('price') or 0)

                line_total = qty * price
                subtotal += line_total

                pdf.cell(100, 8, clean_text(desc), border="B",
                         new_x=XPos.RIGHT, new_y=YPos.TOP)
                pdf.cell(20, 8, f"{int(qty) if qty.is_integer() else qty}", border="B", align='C',
                         new_x=XPos.RIGHT, new_y=YPos.TOP)
                pdf.cell(30, 8, f"{price:,.2f} EUR", border="B", align='R',
                         new_x=XPos.RIGHT, new_y=YPos.TOP)
                pdf.cell(30, 8, f"{line_total:,.2f} EUR", border="B", align='R',
                         new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            pdf.ln(5)

            # --- Totals Calculation ---
            vat_amount = subtotal * vat_rate
            total_final = subtotal + vat_amount

            # --- Totals Box ---
            pdf.set_x(110)
            pdf.set_font("Helvetica", '', 10)
            pdf.cell(50, 6, "Subtotal (Base Imponible):", align='R',
                     new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(30, 6, f"{subtotal:,.2f} EUR", align='R',
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            pdf.set_x(110)
            pdf.cell(50, 6, f"IVA ({int(vat_rate*100)}%):", align='R',
                     new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(30, 6, f"{vat_amount:,.2f} EUR", align='R',
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            pdf.set_x(110)
            pdf.set_font("Helvetica", 'B', 12)
            pdf.set_fill_color(230, 255, 230)
            pdf.cell(50, 10, "TOTAL A PAGAR:", align='R', fill=True,
                     new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(30, 10, f"{total_final:,.2f} EUR", align='R', fill=True,
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            # --- Footer ---
            pdf.ln(15)
            pdf.set_x(10)
            pdf.set_font("Helvetica", 'I', 9)
            pdf.set_text_color(100, 100, 100)
            if doc_title == "FACTURA":
                default_notes = "Factura emitida de acuerdo con la normativa española de facturación (RD 1619/2012)."
            else:
                default_notes = "Gracias por su confianza. Presupuesto válido por 15 días."
            notes = data.get('notes', default_notes)
            pdf.multi_cell(0, 5, f"Notas: {clean_text(notes)}\nEl precio final incluye los impuestos aplicables.")

            # Save
            if doc_title == "FACTURA":
                inv_slug = str(data.get('invoice_number', 'inv')).replace('/', '-')
                filename = f"invoice_{inv_slug}_{int(datetime.now().timestamp())}.pdf"
            else:
                filename = f"budget_{int(datetime.now().timestamp())}.pdf"
            save_dir = os.path.join("static", "generated_docs")
            os.makedirs(save_dir, exist_ok=True)
            full_path = os.path.join(save_dir, filename)

            pdf.output(full_path)
            logger.info("PDF generado: %s", full_path)
            file_url = f"/static/generated_docs/{filename}"
            if doc_title == "FACTURA":
                return file_url, round(subtotal, 2), round(vat_amount, 2), round(total_final, 2)
            return file_url

        except Exception as e:
            logger.error("Error generando PDF: %s", e)
            if doc_title == "FACTURA":
                return None, 0, 0, 0
            return None
