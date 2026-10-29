import os
import re
import json
import time
import logging
import base64
from datetime import datetime
from openai import OpenAI
from core.db_models import db, Ticket
from core.llm_tracker import track as _track

logger = logging.getLogger(__name__)

# El modelo extrae; no estima. Un NIF o una base imponible "estimados" son datos
# fiscales inventados, asi que lo que no se lea se devuelve como null y una
# validacion determinista decide despues si el ticket necesita revision.
_TICKET_PROMPT = """
Actúa como experto contable español. Extrae los datos de este ticket o factura.
HOY ES: {today}.

Devuelve un objeto JSON con exactamente estas claves:
{{
    "fecha": "DD/MM/YYYY",
    "nif": "string",
    "proveedor": "string",
    "numero_ticket": "string",
    "base": float,
    "iva_percent": float,
    "cuota_iva": float,
    "total": float,
    "concepto": "resumen corto"
}}
Copia los valores tal como aparecen en el documento. Si un dato no se ve o no se
puede leer con seguridad, pon null. No estimes ni calcules datos que no aparezcan.
"""

# Tipos de IVA vigentes en España (general, reducido, superreducido y exento)
_VALID_IVA = {0.0, 4.0, 10.0, 21.0}
# NIF/CIF/NIE: letra o dígito + 7 dígitos + letra o dígito
_NIF_RE = re.compile(r'^[A-Z0-9]\d{7}[A-Z0-9]$')


def _parse_ticket_date(fecha_str):
    """Parsea fecha DD/MM/YYYY con fallback seguro a hoy."""
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
        try:
            return datetime.strptime(fecha_str, fmt)
        except (ValueError, TypeError):
            continue
    return datetime.now()


def _to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace('€', '').replace(',', '.').strip())
    except ValueError:
        return None


def _review_reasons(data: dict) -> list[str]:
    """Comprobaciones deterministas sobre lo que ha extraído el modelo."""
    reasons = []
    base, cuota, total = _to_float(data.get('base')), _to_float(data.get('cuota_iva')), _to_float(data.get('total'))
    iva = _to_float(data.get('iva_percent'))

    if total is None:
        reasons.append("sin total")
    if base is not None and cuota is not None and total is not None and abs(base + cuota - total) > 0.05:
        reasons.append(f"base + cuota ({base + cuota:.2f}) no cuadra con el total ({total:.2f})")
    if iva is not None and iva not in _VALID_IVA:
        reasons.append(f"tipo de IVA {iva:g}% no vigente")
    nif = (data.get('nif') or '').upper().replace('-', '').replace(' ', '')
    if nif and not _NIF_RE.match(nif):
        reasons.append("NIF con formato no válido")
    return reasons


def _pdf_first_page_as_png(content: bytes) -> bytes:
    """La entrada de imágenes de la API de visión no acepta PDF: se rasteriza la primera página."""
    import fitz  # PyMuPDF, dependencia de pymupdf4llm
    with fitz.open(stream=content, filetype="pdf") as doc:
        return doc[0].get_pixmap(dpi=200).tobytes("png")


def _build_openai_client():
    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def process_ticket_image(file_path, user_phone):
    """
    Procesa un ticket subido desde la web (PWA).
    file_path: Ruta absoluta local del archivo ya guardado.
    """
    client = _build_openai_client()

    try:
        with open(file_path, "rb") as image_file:
            content = image_file.read()

        # Detectar tipo MIME real por cabecera de bytes
        if content[:4] == b'%PDF':
            content = _pdf_first_page_as_png(content)
            media_type = 'image/png'
        elif content[:8] == b'\x89PNG\r\n\x1a\n':
            media_type = 'image/png'
        elif content[:4] == b'RIFF' and content[8:12] == b'WEBP':
            media_type = 'image/webp'
        else:
            media_type = 'image/jpeg'

        image_data = base64.b64encode(content).decode('utf-8')
        data_url = f"data:{media_type};base64,{image_data}"

        prompt = _TICKET_PROMPT.format(today=datetime.now().strftime('%d/%m/%Y'))

        _t0 = time.time()
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]}
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=400,
        )
        _track(user_phone, "gpt-4o", "ticket_ocr", response, int((time.time() - _t0) * 1000))

        try:
            data = json.loads(response.choices[0].message.content)
        except (json.JSONDecodeError, TypeError):
            data = {}

        reasons = _review_reasons(data)

        # Calcular ruta relativa para la DB
        if 'static' in file_path:
            rel_path = file_path.split('static')[-1].replace('\\', '/')
            db_image_path = f"/static{rel_path}"
        else:
            db_image_path = f"/static/uploads/{os.path.basename(file_path)}"

        new_ticket = Ticket(
            user_phone=user_phone,
            image_path=db_image_path,
            # 'pending' = pendiente de revisión: el dashboard ya los cuenta aparte
            status='pending' if reasons else 'processed',
            concept=data.get('concepto') or 'Gasto Web',
            total=_to_float(data.get('total')) or 0.0,
            date=_parse_ticket_date(data.get('fecha') or ''),
            nif=data.get('nif'),
            provider=data.get('proveedor'),
            ticket_number=data.get('numero_ticket'),
            base=_to_float(data.get('base')),
            tax_percent=_to_float(data.get('iva_percent')),
            fee=_to_float(data.get('cuota_iva')),
            raw_data=json.dumps({**data, "_review": reasons}, ensure_ascii=False)
        )

        db.session.add(new_ticket)
        db.session.commit()

        msg = f"Ticket de {data.get('proveedor') or 'proveedor desconocido'} ({new_ticket.total}€) guardado."
        if reasons:
            msg += " Pendiente de revisión: " + "; ".join(reasons) + "."
        return msg

    except Exception as e:
        logger.error("Error process_ticket_image: %s", e)
        return f"Error procesando imagen: {e}"
