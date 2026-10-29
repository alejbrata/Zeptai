import os
import logging
import time
import requests
import json
from datetime import datetime
from pptx import Presentation
from pptx.util import Pt, Inches
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn

from core.config import Config
from core.llm_tracker import track as _track, timed_track

logger = logging.getLogger(__name__)

# Duracion del reel en segundos. Se le pasa explicitamente a Runway y se reutiliza
# al registrar el coste: si no se envia, el modelo aplica su defecto de 10s y la
# llamada cuesta el doble sin que se note en el panel de metricas.
# Runway factura gen4_turbo a 5 creditos/segundo, luego 5s = 25 creditos = 0,25 EUR.
VIDEO_DURATION_S = 5


class MarketingAgent:
    THEMES = {
        "TECH": {"bg": (15, 23, 42), "title": (56, 189, 248), "text": (226, 232, 240)}, # Slate-900 / Sky-400 / Slate-200
        "ECO": {"bg": (20, 83, 45), "title": (74, 222, 128), "text": (220, 252, 231)},   # Green-900 / Green-400 / Green-100
        "CORPORATE": {"bg": (255, 255, 255), "title": (30, 58, 138), "text": (71, 85, 105)}, # White / Blue-900 / Slate-600
        "LUXURY": {"bg": (0, 0, 0), "title": (212, 175, 55), "text": (250, 250, 250)},   # Black / Gold / White
        "CREATIVE": {"bg": (255, 241, 242), "title": (190, 18, 60), "text": (88, 28, 135)} # Pink-50 / Rose-700 / Purple-900
    }

    def __init__(self):
        from core.clients import get_openai_client, get_runway_client
        self.openai = get_openai_client()
        self.runway_client = get_runway_client()
        self.user_phone: str | None = None
        self.last_error: str | None = None
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.output_folder = os.path.join(self.base_dir, 'static', 'generated_docs')
        os.makedirs(self.output_folder, exist_ok=True)

    def generate_marketing_content(self, prompt_text, content_type="image", business_name="Mi Empresa", logo_path=None, user_phone=None, slide_count=None):
        """
        Genera contenido creativo.
        content_type: 'image' (DALL-E 3) o 'slide' (PowerPoint).
        """
        self.user_phone = user_phone
        logger.info("MarketingAgent: creando %s para %s", content_type, business_name)

        if content_type == "image":
            return self._generate_dalle_image(prompt_text, business_name, logo_path), None
        elif content_type == "slide":
            return self._generate_pptx_slide(prompt_text, business_name, slide_count=slide_count)
        elif content_type == "video":
            target_image = logo_path if logo_path else "dummy_product.jpg"
            video_prompt = self._analyze_product_context(
                target_image,
                business_name=business_name,
                user_brief=prompt_text,
                business_context=self._get_brand_context(),
            )
            video_url = self._generate_runway_video(video_prompt, input_image_path=target_image)
            return video_url, None
        else:
            return None, None

    def _get_brand_context(self):
        """Sector, tono y servicios del negocio, para que el vídeo hable su idioma.

        Sin esto, al prompt solo llegaba el nombre de la empresa y el resultado era
        intercambiable entre una consultora tecnologica y una panaderia.
        """
        if not self.user_phone:
            return ""
        try:
            from core.db_models import BusinessProfile
            profile = BusinessProfile.query.filter_by(user_phone=self.user_phone).first()
            sk = (profile.static_knowledge or {}) if profile else {}
            campos = [
                ("Sector", sk.get("sector")),
                ("Brand tone", sk.get("tone")),
                ("Services", (sk.get("services") or "")[:300]),
            ]
            return "\n".join(f"{k}: {v}" for k, v in campos if v)
        except Exception as e:
            logger.warning("No se pudo cargar el contexto de marca: %s", e)
            return ""

    def _generate_dalle_image(self, user_prompt, business_name, logo_path=None):
        try:
            # 1. Optimizar prompt para DALL-E usando GPT-4o
            optimized_prompt = self._refine_prompt_for_dalle(user_prompt, business_name)
            logger.info("Prompt DALL-E: %s...", optimized_prompt[:50])
            
            # 2. Generar Imagen
            # dall-e-3 fue retirado por OpenAI -- usar gpt-image-1. Ojo: gpt-image-1
            # no acepta quality="standard" (solo low/medium/high/auto) y nunca
            # devuelve "url", siempre "b64_json".
            import base64
            with timed_track(self.user_phone, "gpt-image-1", "marketing_image") as t:
                response = self.openai.images.generate(
                    model="gpt-image-1",
                    prompt=optimized_prompt,
                    size="1024x1024",
                    quality="high",
                    n=1,
                )
            img_data = base64.b64decode(response.data[0].b64_json)

            # 3. Guardar localmente
            filename = f"marketing_{int(datetime.now().timestamp())}.png"
            local_path = os.path.join(self.output_folder, filename)

            with open(local_path, 'wb') as handler:
                handler.write(img_data)
                
            # 4. Superponer Logo (Si existe)
            if logo_path:
                try:
                    from PIL import Image
                    # Convertir ruta web relativa a absoluta
                    # logo_path suele ser "/static/uploads/logos/..."
                    # self.base_dir está en "modules/proactive/...", subir 3 niveles para llegar a root
                    # Pero self.output_folder ya usa self.base_dir correctamente calculado en __init__
                    # self.base_dir en __init__ es: os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    # Esto apunta a TICKETIA_PRO root.
                    # logo_path empieza con /, quitarlo.
                    abs_logo_path = os.path.join(self.base_dir, logo_path.lstrip('/'))
                    
                    if os.path.exists(abs_logo_path):
                        base_img = Image.open(local_path).convert("RGBA")
                        logo_img = Image.open(abs_logo_path).convert("RGBA")
                        
                        # Resize logo (ej: 20% del ancho)
                        base_w, base_h = base_img.size
                        target_w = int(base_w * 0.20)
                        aspect_ratio = logo_img.width / logo_img.height
                        target_h = int(target_w / aspect_ratio)
                        logo_img = logo_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                        
                        # Posición: Esquina inferior derecha con padding
                        padding = 30
                        pos_x = base_w - target_w - padding
                        pos_y = base_h - target_h - padding
                        
                        # Pegar (usando el mismo logo como máscara para transparencia)
                        base_img.paste(logo_img, (pos_x, pos_y), logo_img)
                        
                        # Guardar (sobreescribir)
                        base_img.save(local_path, format="PNG")
                        logger.info("Logo superpuesto correctamente")
                    else:
                        logger.warning("Logo no encontrado: %s", abs_logo_path)
                except Exception as e_pil:
                    logger.warning("Error procesando logo PIL: %s", e_pil)
                
            return f"{Config.PUBLIC_URL}/static/generated_docs/{filename}"
            
        except Exception as e:
            logger.error("Error DALL-E: %s", e)
            self.last_error = f"OpenAI (generación de imagen): {e}"
            return None

    def _analyze_product_context(self, image_path, business_name="", user_brief="", business_context=""):
        """
        Two-stage Visual Intelligence pipeline:
        Stage 1 – GPT-4o Vision: understand WHAT is in the image (product type, colors, natural context).
        Stage 2 – GPT-4o Text: convert that analysis into a rich cinematic Runway Gen-3 prompt.
        """
        import base64

        if not os.path.exists(image_path):
            logger.warning("Imagen no encontrada para analisis: %s", image_path)
            return "Cinematic tracking shot of a person using the product outdoors, natural lighting, movement, photorealistic, 4K"

        _ext = os.path.splitext(image_path)[1].lower()
        mime_type = {"jpg": "image/jpeg", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(_ext, "image/png")

        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            base64_image = f"data:{mime_type};base64,{encoded_string}"

        try:
            # ── Stage 1: Image Understanding ──────────────────────────────────
            logger.info("Stage 1 – Analizando imagen: %s", os.path.basename(image_path))

            _t0 = time.time()
            stage1_response = self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a product analyst preparing a still image for image-to-video "
                            "animation. Only what is visible in the frame can be animated, so "
                            "describe the frame itself:\n"
                            "1. PRODUCT_TYPE: What is the main subject/product?\n"
                            "2. CATEGORY: clothing / food / electronics / tool / beauty / decor / other\n"
                            "3. COLORS: Main colors and materials\n"
                            "4. STYLE: Style, mood, target audience\n"
                            "5. LIGHT: Where does the light come from, and what surfaces does it hit?\n"
                            "6. DEPTH: Foreground, midground and background elements (for camera parallax)\n"
                            "7. MOVABLE_DETAIL: Anything that could plausibly drift or shimmer — steam, "
                            "dust, foliage, curtains, reflections, screen glow. Say 'none' if there is nothing.\n"
                            "8. PEOPLE_PRESENT: yes/no, and which body parts are visible. This matters "
                            "because hands and faces must be kept still during animation.\n"
                            "Be concise. Max 150 words."
                        )
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Analyze this product image."},
                            {
                                "type": "image_url",
                                "image_url": {"url": base64_image, "detail": "high"}
                            }
                        ]
                    }
                ],
                max_tokens=200
            )
            _track(self.user_phone, "gpt-4o", "video_analyze_image",
                   stage1_response, int((time.time() - _t0) * 1000))

            product_analysis = stage1_response.choices[0].message.content.strip()
            logger.info("Análisis del producto:\n%s", product_analysis)

            # ── Stage 2: Cinematic Prompt Generation ──────────────────────────
            logger.info("Stage 2 – Generando prompt cinematográfico")

            stage2_system = """You are a world-class prompt engineer for Runway Gen-4 IMAGE-TO-VIDEO, writing SOCIAL MEDIA REELS for brands.

CRITICAL CONTEXT: the user's still image is the first frame. The model animates THAT frame — it cannot invent new subjects or actions. Prompts written for text-to-video ("a model walking on the beach") produce warped, melting results here. Describe how the existing frame comes alive.

THIS IS A REEL, NOT A CORPORATE CLIP. Five seconds, seen on a phone, competing with a
thumb that is about to scroll. It must grab attention in the first second. A timid pan
is a failure. So is anatomy in motion — it looks broken, and broken does not sell.

THE BRAND DICTATES THE REGISTER. You are given the sector, tone and services. A generative
AI consultancy is not a bakery: the light, the pace and the mood must belong to that
business. Deep blues, screen glow, precise geometry and cool clean air read as tech;
warm ambers, soft grain and slow drift read as artisan. Choose deliberately.

MOTION SOURCES — combine AT LEAST TWO, each clearly visible:
1. CAMERA WITH REAL TRAVEL. Not a nudge. "Dolly-in pushing decisively toward X",
   "sweeping parallax across the scene", "crane rising over the surface", "orbit around X".
   The frame must look meaningfully different after 5 seconds.
2. RACK FOCUS. "Focus pulls from the foreground object to X". Renders beautifully and
   reads as deliberate, expensive cinematography.
3. VOLUMETRIC DETAIL WITH PRESENCE. Thick steam curling upward, dust swirling through a
   shaft of light, foliage swaying, curtains billowing, smoke, rippling water.
   Give it substance: "steam rising thickly", never "a hint of steam".
4. LIGHT IN MOTION. Screen glow pulsing and shifting, sunlight sweeping across surfaces,
   reflections travelling over glass and metal, highlights raking along edges.

HARD RULES (breaking these is what visibly ruins the video):
- NEVER animate hands, fingers, faces, mouths or eyes. Anatomy in motion is where the
  model fails most obviously: melted fingers, warped features. If a person is in frame,
  aim the action away from them or note they hold their position — but keep the REST of
  the scene strongly in motion.
- NEVER invent a subject, object or action absent from the frame.
- NEVER request walking, typing, pouring, cutting or any body performance.
- ONE continuous camera move. Cuts or reversals produce morphing.
- Vertical framing: keep the composition working in a tall 9:16 crop.

EXAMPLES:
- Tech consultancy, desk scene: "Dolly-in pushing decisively toward the laptop, focus pulling from the foreground notebook to the glowing screen, cool blue light shifting across the desk, steam rising thickly from the mug, leaves swaying by the window. Cinematic, photorealistic, 4K."
- Fashion brand, clothing: "Sweeping parallax orbit around the subject, fabric rippling in a strong breeze, golden-hour light raking across the texture, background bokeh sliding past. Cinematic, photorealistic, 4K."
- Artisan food: "Crane descending over the plate, focus pulling across the surface, steam billowing upward through warm side light, reflections travelling over the cutlery. Cinematic, 4K."

OUTPUT: Only the final prompt. No explanations. In English. Max 60 words."""

            bloques = []
            if business_name:
                bloques.append(f"Brand: {business_name}")
            if business_context:
                bloques.append(business_context)
            marca = "\n".join(bloques)

            brief = (user_brief or "").strip()
            bloque_brief = f"\nWhat the business owner asked for: {brief}\n" if brief else ""

            _t0 = time.time()
            stage2_response = self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": stage2_system},
                    {
                        "role": "user",
                        "content": (
                            f"{marca}\n{bloque_brief}\n"
                            f"Analysis of the still image that will be animated:\n{product_analysis}\n\n"
                            "Write the reel prompt. The register must match the brand's sector "
                            "and tone, and honour the owner's request."
                        )
                    }
                ],
                max_tokens=350
            )
            _track(self.user_phone, "gpt-4o", "video_generate_prompt",
                   stage2_response, int((time.time() - _t0) * 1000))

            final_prompt = stage2_response.choices[0].message.content.strip()
            logger.info("Prompt final para Runway: %s", final_prompt)
            return final_prompt

        except Exception as e:
            logger.error("Error Visual Intelligence: %s", e)
            return "Cinematic tracking shot of a person using the product outdoors, natural lighting, wind, movement, photorealistic, 4K"
    def _generate_pptx_slide(self, topic, business_name, slide_count=None):
        try:
            import re

            # 1. Número de slides: parámetro explícito > regex en el prompt > default 5
            if slide_count and isinstance(slide_count, int):
                num_total = slide_count
            else:
                num_match = re.search(r'\b(\d+)\s*(?:slides?|diapositivas?|p[aá]ginas?|l[aá]minas?)\b', topic, re.IGNORECASE)
                num_total = int(num_match.group(1)) if num_match else 5
            num_total = max(2, min(8, num_total))
            num_content = num_total - 1  # portada cuenta como 1

            # 2. Contenido de GPT con count exacto
            presentation_data = self._get_presentation_structure(topic, num_content)

            # 3. Abrir plantilla (hereda tema, colores, tipografías del diseñador)
            template_path = os.path.join(self.base_dir, 'static', 'plantilla.pptx')
            prs = Presentation(template_path)

            # 4. Borrar todos los slides existentes de la plantilla
            sldIdLst = prs.slides._sldIdLst
            for sldId in list(sldIdLst):
                rId = sldId.get(qn('r:id'))
                prs.part.drop_rel(rId)
                sldIdLst.remove(sldId)

            # Layouts verificados de plantilla.pptx (13 layouts en total):
            # [5]  "Título de sección"   → idx 0 CENTER_TITLE, idx 1 SUBTITLE
            # [1]  "Título y contenido " → idx 0 TITLE, idx 1 OBJECT (bullets)
            # [12] "Contenido 3"         → idx 0 TITLE, idx 1 OBJECT (cierre)
            layout_cover   = prs.slide_layouts[5]
            layout_content = prs.slide_layouts[1]
            layout_closing = prs.slide_layouts[12]

            def fill_text(placeholder, text):
                placeholder.text_frame.clear()
                p = placeholder.text_frame.paragraphs[0]
                p.text = text

            def fill_bullets(placeholder, points):
                tf = placeholder.text_frame
                tf.clear()
                for i, point in enumerate(points):
                    p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    p.text = point
                    p.level = 0

            # ── PORTADA ───────────────────────────────────────────────────────
            slide = prs.slides.add_slide(layout_cover)
            for ph in slide.placeholders:
                idx = ph.placeholder_format.idx
                if idx == 0:
                    fill_text(ph, presentation_data.get('title', 'Presentación'))
                elif idx == 1:
                    fill_text(ph, f"{presentation_data.get('subtitle', '')}\n{business_name}")

            # ── SLIDES DE CONTENIDO ───────────────────────────────────────────
            slides_data = presentation_data.get('slides', [])[:num_content]
            for i, slide_data in enumerate(slides_data):
                # Último slide de contenido usa el layout de cierre si es el final
                is_last = (i == len(slides_data) - 1)
                layout = layout_closing if is_last else layout_content
                slide = prs.slides.add_slide(layout)
                for ph in slide.placeholders:
                    idx = ph.placeholder_format.idx
                    try:
                        if idx == 0:
                            fill_text(ph, slide_data.get('title', f'Sección {i+1}'))
                        elif idx == 1:
                            fill_bullets(ph, slide_data.get('points', []))
                    except Exception:
                        pass  # ignorar placeholders de imagen u otros tipos

            # 5. Guardar
            filename   = f"presentation_{int(datetime.now().timestamp())}.pptx"
            local_path = os.path.join(self.output_folder, filename)
            prs.save(local_path)

            # 6. Título corto para mostrar en Documentos (máx 5 palabras, title-case)
            raw_title = presentation_data.get('title', '')
            words = raw_title.split()[:5]
            short_title = ' '.join(words).title() if words else 'Presentación'

            return f"{Config.PUBLIC_URL}/static/generated_docs/{filename}", short_title

        except Exception as e:
            logger.error("Error PPTX: %s", e)
            self.last_error = f"Generación de presentación: {e}"
            return None, None

    def _generate_runway_video(self, prompt, input_image_path=None):
        try:
            logger.info("Llamando a Runway gen4_turbo. Prompt: %s...", prompt[:50])

            # Preparar Imagen (Base64 Data URI)
            prompt_image_uri = None
            if input_image_path and os.path.exists(input_image_path):
                 import base64
                 import mimetypes
                 mime_type, _ = mimetypes.guess_type(input_image_path)
                 if not mime_type:
                     mime_type = "image/png"
                 
                 with open(input_image_path, "rb") as image_file:
                    encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                    prompt_image_uri = f"data:{mime_type};base64,{encoded_string}"

            # 1. Iniciar Generación
            # gen3a_turbo fue retirado por Runway el 30/07/2026 (sunset) — usar gen4_turbo.
            # Variable única para el nombre del modelo: se usa también en _track() más abajo,
            # para que las métricas de coste no se queden con el nombre del modelo viejo
            # si esto se actualiza de nuevo en el futuro.
            _runway_model = 'gen4_turbo'
            task_args = {
                "model": _runway_model,
                "prompt_text": prompt,
                # Vertical: es un reel para movil, no una pieza de escritorio.
                "ratio": "720:1280",
                "duration": VIDEO_DURATION_S,
            }
            if prompt_image_uri:
                task_args["prompt_image"] = prompt_image_uri
            
            task = self.runway_client.image_to_video.create(**task_args)
            
            task_id = task.id
            logger.info("Tarea Runway iniciada: %s", task_id)
            
            # 2. Polling con exponential backoff (max ~5 min acumulado)
            video_url = None
            wait = 3  # segundos iniciales
            elapsed = 0
            max_wait = 300  # timeout total
            _runway_start = time.time()

            while elapsed < max_wait:
                time.sleep(wait)
                elapsed += wait
                task_status = self.runway_client.tasks.retrieve(task_id)
                status = task_status.status
                logger.debug("Status Runway [%s]: %s (t+%ds)", task_id, status, elapsed)

                if status == "SUCCEEDED":
                    video_url = task_status.output[0]
                    _track(self.user_phone, _runway_model, "runway_video_generation",
                           latency_ms=int((time.time() - _runway_start) * 1000),
                           extra={"duration_s": VIDEO_DURATION_S})
                    break
                elif status == "FAILED":
                    _track(self.user_phone, _runway_model, "runway_video_generation",
                           latency_ms=int((time.time() - _runway_start) * 1000),
                           success=False, error=task_status.failure_reason)
                    logger.error("Runway task fallida [%s]: %s", task_id, task_status.failure_reason)
                    self.last_error = f"Runway: {task_status.failure_reason}"
                    return None

                wait = min(wait * 2, 30)  # duplicar hasta un máximo de 30s

            if not video_url:
                return None

            # 3. Descargar Video
            filename = f"runway_{int(datetime.now().timestamp())}.mp4"
            local_path = os.path.join(self.output_folder, filename)
            
            r = requests.get(video_url)
            with open(local_path, 'wb') as f:
                f.write(r.content)
                
            logger.info("Video Runway descargado: %s", filename)
            return f"{Config.PUBLIC_URL}/static/generated_docs/{filename}"

        except Exception as e:
            logger.error("Error Runway API: %s", e)
            self.last_error = f"Runway: {e}"
            # Fallback a simulación si falla
            return self._generate_simulated_video(prompt)

    def _generate_simulated_video(self, prompt):
        logger.warning("Runway no disponible — fallo real: %s", self.last_error)
        return None

    def _apply_theme(self, slide, theme):
        # Fondo
        background = slide.background
        fill = background.fill
        fill.solid()
        fill.fore_color.rgb = RGBColor(*theme['bg'])

    def _style_text(self, shape, rgb, bold=False, size=None):
        if not shape.text_frame:
            return
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                run.font.color.rgb = RGBColor(*rgb)
                if bold:
                    run.font.bold = True
                if size:
                    run.font.size = size

    def _refine_prompt_for_dalle(self, text, business_name):
        sys = """
        Eres un experto en Prompts para DALL-E 3.
        Objetivo: Crear una imagen publicitaria de altísimo impacto visual y estético.
        
        RESTRICCIONES DE TEXTO (STRICT):
        - NO incluyas frases largas, eslóganes ni oraciones.
        - SOLO se permite el Nombre de la Marca o Siglas.
        - El texto debe ser mínimo, elegante e integrado en el diseño.
        - Si duda, mejor SIN texto que con texto ilegible.
        
        INSTRUCCIÓN DE MARCA:
        - Incluye visiblemente la marca: '{business_name}'.
        """
        user_msg = f"Idea del usuario: {text}. Marca: {business_name}. Diseña algo moderno, minimalista y profesional."
        
        with timed_track(self.user_phone, "gpt-4o", "marketing_image_prompt") as t:
            resp = self.openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "system", "content": sys}, {"role": "user", "content": user_msg}]
            )
            t["response"] = resp
        return resp.choices[0].message.content

    def _get_presentation_structure(self, topic, num_content_slides=3):
        sys_prompt = f"""Eres un Consultor Senior experto en storytelling y presentaciones de negocio de alto impacto.

TAREA: Crea el guión para una presentación con EXACTAMENTE {num_content_slides} diapositivas de contenido.

REGLAS:
- El array "slides" debe tener EXACTAMENTE {num_content_slides} objetos. Ni uno más, ni uno menos.
- Escribe contenido REAL, específico y valioso. Nada de "Punto 1" ni frases vacías.
- Cada diapositiva: 3-4 "points" concisos pero informativos (1-2 líneas cada uno).
- "theme": elige el más apropiado entre 'TECH', 'ECO', 'CORPORATE', 'LUXURY', 'CREATIVE'.

OUTPUT JSON ESTRICTO (sin texto fuera del JSON):
{{
    "theme": "CORPORATE",
    "title": "TÍTULO IMPACTANTE EN MAYÚSCULAS",
    "subtitle": "Subtítulo que venda la propuesta",
    "slides": [
        {{
            "title": "1. Nombre de la Sección",
            "points": [
                "Primer punto con dato o insight concreto.",
                "Segundo punto de valor real.",
                "Tercer punto de acción o conclusión."
            ]
        }}
    ]
}}"""
        try:
            with timed_track(self.user_phone, "gpt-4o", "presentation_structure") as t:
                resp = self.openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": topic}],
                    response_format={"type": "json_object"},
                    max_tokens=2000
                )
                t["response"] = resp
            data = json.loads(resp.choices[0].message.content)
            # Hard-enforce slide count in case GPT ignores the instruction
            slides = data.get('slides', [])
            if len(slides) > num_content_slides:
                data['slides'] = slides[:num_content_slides]
            elif len(slides) < num_content_slides:
                for i in range(len(slides), num_content_slides):
                    data['slides'].append({
                        "title": f"Sección {i + 1}",
                        "points": ["Contenido pendiente de completar."]
                    })
            return data
        except Exception as e:
            logger.error("Error GPT JSON estructura presentación: %s", e)
            return {
                "theme": "CORPORATE",
                "title": "Presentación",
                "subtitle": "Propuesta de valor",
                "slides": [
                    {"title": f"Sección {i + 1}", "points": ["Contenido no disponible."]}
                    for i in range(num_content_slides)
                ]
            }
