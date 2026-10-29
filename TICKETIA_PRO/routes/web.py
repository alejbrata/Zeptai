import os
import re
import io
import json
import logging
import secrets
import pandas as pd
from datetime import datetime, timedelta
from functools import wraps

logger = logging.getLogger(__name__)

def _validate_password(password: str) -> tuple:
    """Devuelve (ok: bool, msg: str). Mínimo 8 chars, 1 mayúscula, 1 número."""
    if not password or len(password) < 8:
        return False, 'La contraseña debe tener al menos 8 caracteres.'
    if not re.search(r'[A-Z]', password):
        return False, 'La contraseña debe contener al menos una letra mayúscula.'
    if not re.search(r'[0-9]', password):
        return False, 'La contraseña debe contener al menos un número.'
    return True, ''


def _sanitize_business_name(value: str) -> str:
    """
    Limpia el nombre de negocio antes de guardarlo. Este campo lo define
    cada usuario pero se inyecta sin más filtros en prompts de otros usuarios
    (p.ej. modules/proactive/networker.py al buscar sinergias entre negocios),
    así que es el único punto de entrada que cruza el límite entre inquilinos.
    """
    if not value:
        return ''
    value = re.sub(r'<[^>]+>', '', value)
    value = re.sub(r'[\r\n\t]+', ' ', value)
    value = re.sub(
        r'(?i)(ignora|ignore|olvida|forget|disregard|override|system:|<\|im_start\|>|<\|im_end\|>)',
        '[FILTRADO]', value
    )
    return value.strip()[:100]
from flask import Blueprint, request, session, jsonify, render_template, redirect, url_for, flash, send_file, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Message
from sqlalchemy.sql import func
from core.db_models import BusinessProfile, Ticket, ChatMessage, GeneratedDocument, SynergyMatch, ActivityLog, db
from core.limiter import limiter

web_bp = Blueprint('web', __name__)

# Hash scrypt de una contrasena aleatoria, para verificar contra algo cuando el
# email no existe y que el login tarde lo mismo en ambos casos.
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(16))

# Mismo mensaje exista o no el email, para no revelar quien esta registrado.
_FORGOT_PASSWORD_MSG = 'Si el correo está registrado, recibirás un enlace para restablecer la contraseña.'


def _web_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_phone' not in session:
            return redirect(url_for('web.login'))
        return f(*args, **kwargs)
    return decorated


def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_phone' not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@web_bp.route('/health')
def health():
    return jsonify({"status": "ok"})

@web_bp.route('/')
def index():
    if 'user_phone' in session:
        return redirect(url_for('web.dashboard'))
    return render_template('landing.html')

@web_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '').strip()
        business_name = _sanitize_business_name(request.form.get('business_name', ''))

        # Validaciones básicas
        if not email or not phone or not password:
            flash('Por favor completa todos los campos.', 'error')
            return redirect(url_for('web.register'))

        if not request.form.get('gdpr_consent'):
            flash('Debes aceptar la Política de Privacidad para registrarte.', 'error')
            return redirect(url_for('web.register'))

        ok, msg = _validate_password(password)
        if not ok:
            flash(msg, 'error')
            return redirect(url_for('web.register'))

        # Check duplicados
        if BusinessProfile.query.filter((BusinessProfile.user_phone == phone) | (BusinessProfile.email == email)).first():
            flash('Este teléfono o email ya está registrado.', 'error')
            return redirect(url_for('web.register'))

        # Crear Usuario
        hashed_pw = generate_password_hash(password)
        new_user = BusinessProfile(
            user_phone=phone,
            email=email,
            password_hash=hashed_pw,
            business_name=business_name,
            plan_tier='BASIC', # Plan por defecto
            features={"tickets_allowed": True, "dashboard_access": True}
        )
        
        db.session.add(new_user)
        db.session.commit()
        
        flash('¡Cuenta creada! Por favor inicia sesión.', 'success')
        return redirect(url_for('web.login'))

    return render_template('register.html')

@web_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()
        
        # Buscar usuario por EMAIL
        profile = BusinessProfile.query.filter_by(email=email).first()

        # Si el email no existe se verifica igualmente contra un hash ficticio: sin
        # esto la respuesta era ~100 ms mas rapida y el tiempo revelaba si existe.
        stored_hash = profile.password_hash if profile and profile.password_hash else _DUMMY_HASH
        password_ok = check_password_hash(stored_hash, password)

        if profile and password_ok:
            # Login exitoso. Se descarta cualquier contenido previo de la sesion
            # antes de escribir la identidad, y se marca permanente para aplicar
            # PERMANENT_SESSION_LIFETIME.
            session.clear()
            session.permanent = True
            session['user_phone'] = profile.user_phone
            session['user_email'] = profile.email
            session['business_name'] = profile.business_name
            flash(f'Bienvenido, {profile.business_name}', 'success')
            # Primera vez: redirigir al wizard si no tiene IA configurada
            if not profile.system_prompt:
                return redirect(url_for('web.wizard'))
            return redirect(url_for('web.dashboard'))
        else:
            flash('⚠️ Credenciales incorrectas.', 'error')
            
    return render_template('login.html')

@web_bp.route('/logout')
def logout():
    session.clear()
    flash('Sesión cerrada correctamente.', 'info')
    return redirect(url_for('web.login'))

@web_bp.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = BusinessProfile.query.filter_by(email=email).first()
        
        if user:
            token = secrets.token_urlsafe(32)
            user.reset_token = token
            from datetime import timezone
            user.reset_token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
            db.session.commit()
            
            reset_url = url_for('web.reset_password', token=token, _external=True)
            
            msg = Message('Restablecer Contraseña - Zeptai',
                          sender=current_app.config['MAIL_DEFAULT_SENDER'],
                          recipients=[email])
            msg.body = f'Para restablecer tu contraseña, haz clic en el siguiente enlace: {reset_url}\n\nSi no solicitaste esto, ignora este correo.'
            
            try:
                from app import mail
                mail.send(msg)
            except Exception as e:
                # El fallo se registra pero no se muestra: un mensaje distinto
                # delataria que el email existe.
                logger.error("Error enviando mail reset password: %s", e)

        flash(_FORGOT_PASSWORD_MSG, 'info')
        return redirect(url_for('web.login'))
        
    return render_template('forgot_password.html')

@web_bp.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    from datetime import timezone
    user = BusinessProfile.query.filter_by(reset_token=token).first()

    # Invalidar token expirado inmediatamente para evitar reutilización
    if not user or not user.reset_token_expiry:
        flash('El enlace es inválido o ha expirado.', 'error')
        return redirect(url_for('web.login'))

    if user.reset_token_expiry < datetime.now(timezone.utc):
        user.reset_token = None
        user.reset_token_expiry = None
        db.session.commit()
        flash('El enlace ha expirado. Solicita uno nuevo.', 'error')
        return redirect(url_for('web.login'))

    if request.method == 'POST':
        password = request.form.get('password', '').strip()
        ok, msg = _validate_password(password)
        if not ok:
            flash(msg, 'error')
            return redirect(request.url)

        # Invalidar token ANTES de cambiar la contraseña (previene reutilización)
        user.reset_token = None
        user.reset_token_expiry = None
        user.password_hash = generate_password_hash(password)
        db.session.commit()

        flash('¡Contraseña restablecida! Inicia sesión.', 'success')
        return redirect(url_for('web.login'))

    return render_template('reset_token.html')

@web_bp.route('/marketplace')
@_web_login_required
def marketplace():
    user = BusinessProfile.query.filter_by(user_phone=session['user_phone']).first()
    # Asegurar que active_agents sea una lista
    if user.active_agents is None:
        user.active_agents = []
        
    return render_template('marketplace.html', current_user=user, current_page='marketplace')

@web_bp.route('/toggle_agent/<agent_id>', methods=['POST'])
@_web_login_required
def toggle_agent(agent_id):
    user = BusinessProfile.query.filter_by(user_phone=session['user_phone']).first()
    current_agents = list(user.active_agents or []) # Copia explícita
    
    if agent_id in current_agents:
        current_agents.remove(agent_id)
        flash(f'Agente desactivado.', 'info')
    else:
        current_agents.append(agent_id)
        flash(f'¡Agente activado! 🚀', 'success')
        
    user.active_agents = current_agents
    # Forzar actualización en Postgres (detectar cambios en JSON)
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(user, "active_agents")
    
    db.session.commit()
    return redirect(url_for('web.marketplace'))

@web_bp.route('/agent_config/<agent_id>')
@_web_login_required
def agent_config(agent_id):
    user = BusinessProfile.query.filter_by(user_phone=session['user_phone']).first()
    
    # Obtener configuración actual del agente (o dict vacío)
    current_config = user.agent_config.get(agent_id, {}) if user.agent_config else {}
    
    # Nombres bonitos
    agent_names = {
        "post_sales_service": "Servicio Post-Venta",
        "grant_hunter": "Grant Hunter",
        "networker": "Networking Agent"
    }
    name = agent_names.get(agent_id, agent_id)

    return render_template('agent_config.html', agent_id=agent_id, agent_name=name, config=current_config)

@web_bp.route('/save_agent_config/<agent_id>', methods=['POST'])
@_web_login_required
def save_agent_config(agent_id):
    user = BusinessProfile.query.filter_by(user_phone=session['user_phone']).first()
    
    # 1. Leer checkboxes (si no están, es False)
    enable_feedback = 'enable_feedback' in request.form
    enable_reactivation = 'enable_reactivation' in request.form
    # 1.5 Leer Ofertas
    offer_type = request.form.get('offer_type', '10% de Descuento')
    custom_offer_text = request.form.get('custom_offer_text', '')
    
    # 2. Actualizar JSON
    # Necesitamos copiar el dict completo para que SQLAlchemy detecte el cambio si anidamos
    full_config = dict(user.agent_config) if user.agent_config else {}
    
    # Actualizamos solo la key de este agente
    full_config[agent_id] = {
        "enable_feedback": enable_feedback,
        "enable_reactivation": enable_reactivation,
        "offer_type": offer_type,
        "custom_offer_text": custom_offer_text
    }
    
    user.agent_config = full_config
    
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(user, "agent_config")
    
    db.session.commit()
    
    flash('Configuración actualizada.', 'success')
    return redirect(url_for('web.marketplace'))

@web_bp.route('/profile', methods=['GET', 'POST'])
@_web_login_required
def profile():
    user_phone = session['user_phone']
    user = BusinessProfile.query.filter_by(user_phone=user_phone).first()
    
    if request.method == 'POST':
        # Change Password Logic
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        
        ok, msg = _validate_password(new_password)
        if not check_password_hash(user.password_hash, current_password):
            flash('La contraseña actual es incorrecta.', 'error')
        elif not ok:
            flash(msg, 'error')
        else:
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash('Contraseña actualizada correctamente.', 'success')
            
    return render_template('profile.html', user=user, current_page='profile')

@web_bp.route('/dashboard')
@_web_login_required
def dashboard():
    user_phone = session['user_phone']
    
    # 2. Obtener Datos Reales
    profile = BusinessProfile.query.filter_by(user_phone=user_phone).first()

    # Limitamos a 5 para el dashboard — ordenamos por id desc para mostrar los últimos subidos
    tickets = Ticket.query.filter_by(user_phone=user_phone).order_by(Ticket.id.desc()).limit(5).all()
    # Para calcular contadores, necesitamos TODOS los tickets (o hacer queries count separadas)
    # Haremos queries separadas para contadores abajo para no traer todo a memoria en dashboard

    
    # --- MÉTRICAS ---
    # A. Total Gastos Mes Actual — por fecha de subida (created_at), no por fecha del recibo
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)

    total_gastos = db.session.query(func.sum(Ticket.total)).filter(
        Ticket.user_phone == user_phone,
        Ticket.created_at >= month_start
    ).scalar() or 0.0

    # Formatear bonito (ej: 1.250,50)
    total_gastos_fmt = f"{total_gastos:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    
    # B. Tickets Pendientes
    tickets_pendientes = Ticket.query.filter_by(
        user_phone=user_phone, 
        status='pending'
    ).count()

    # B2. Tickets Procesados
    tickets_procesados = Ticket.query.filter_by(
        user_phone=user_phone, 
        status='processed'
    ).count()
    
    # C. Chats Atendidos
    chats_atendidos = ChatMessage.query.filter_by(
        user_phone=user_phone, 
        role='user'
    ).count()

    # D. Activity Logs (NUEVO)
    recent_activity = ActivityLog.query.filter_by(user_phone=user_phone)\
        .order_by(ActivityLog.timestamp.desc())\
        .limit(10).all()
    
    # Nombre del mes
    meses_es = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto", 
        9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
    }
    month_name = meses_es.get(now.month, "Mes Actual")

    class UserContext:
        is_authenticated = True
        phone = user_phone
        business_name = profile.business_name if profile else "Mi Negocio"
        subscription_status = 'active' 
        has_bot = profile.features.get('bot_enabled', False) if profile and profile.features else False
        is_configured = True if (profile and profile.system_prompt) else False
        bot_status = 'active' if (has_bot and is_configured) else 'inactive' 
    
    # --- Date Helpers for UI ---
    from datetime import timedelta
    yesterday = now - timedelta(days=1)
    now_str = now.strftime('%Y-%m-%d')
    yesterday_str = yesterday.strftime('%Y-%m-%d')

    return render_template(
        'dashboard.html',
        current_user=UserContext(),
        tickets=tickets,
        total_gastos=total_gastos_fmt,
        tickets_pendientes=tickets_pendientes,
        tickets_procesados=tickets_procesados,
        chats_atendidos=chats_atendidos,
        current_month_name=month_name,
        logs=recent_activity,
        now_str=now_str,
        yesterday_str=yesterday_str,
        current_page='dashboard'
    )

@web_bp.route('/tickets')
def tickets_redirect():
    return redirect(url_for('web.transactions'), 301)

@web_bp.route('/transactions')
@_web_login_required
def transactions():
    user_phone = session['user_phone']
    
    # Traer TODOS los tickets
    tickets = Ticket.query.filter_by(user_phone=user_phone).order_by(Ticket.date.desc()).all()

    # Resumen IVA trimestral sobre tickets con base imponible registrada
    from sqlalchemy import func as sqlfunc
    iva_rows = db.session.query(
        sqlfunc.extract('year',    Ticket.date).label('year'),
        sqlfunc.extract('quarter', Ticket.date).label('quarter'),
        sqlfunc.coalesce(sqlfunc.sum(Ticket.base),  0).label('base'),
        sqlfunc.coalesce(sqlfunc.sum(Ticket.fee),   0).label('fee'),
        sqlfunc.coalesce(sqlfunc.sum(Ticket.total), 0).label('total'),
        sqlfunc.count(Ticket.id).label('count'),
    ).filter(
        Ticket.user_phone == user_phone,
        Ticket.base.isnot(None),
    ).group_by('year', 'quarter').order_by('year', 'quarter').all()

    iva_summary = [
        {
            "year":    int(r.year),
            "quarter": int(r.quarter),
            "base":    round(float(r.base),  2),
            "fee":     round(float(r.fee),   2),
            "total":   round(float(r.total), 2),
            "count":   r.count,
        }
        for r in iva_rows
    ]

    return render_template('transactions.html', tickets=tickets,
                           iva_summary=iva_summary, current_page='transactions')

@web_bp.route('/documents')
@_web_login_required
def documents_page():
    user_phone = session['user_phone']
    
    # Obtener documentos
    all_docs = GeneratedDocument.query.filter_by(user_phone=user_phone).order_by(GeneratedDocument.created_at.desc()).all()
    
    def group_by_date(docs):
        grouped = {}
        for doc in docs:
            year = doc.created_at.year
            month = doc.created_at.strftime('%B').capitalize() # Enero, Febrero... (depende de locale, por ahora usamos inglés o map)
            
            # Map simple de meses por si acaso el locale no está en ES
            months_es = {
                1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 
                5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto", 
                9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
            }
            month = months_es.get(doc.created_at.month, month)

            if year not in grouped: grouped[year] = {}
            if month not in grouped[year]: grouped[year][month] = []
            grouped[year][month].append(doc)
        return grouped

    # Categorizar y Agrupar
    docs_proposals = [d for d in all_docs if d.doc_type == 'proposal']
    docs_invoices  = [d for d in all_docs if d.doc_type == 'invoice']
    docs_reports   = [d for d in all_docs if d.doc_type == 'report']
    docs_images = [d for d in all_docs if d.doc_type == 'image']
    docs_presentations = [d for d in all_docs if d.doc_type == 'presentation']
    docs_video_prompts = [d for d in all_docs if d.doc_type == 'video_prompt']

    # --- Mobile UX helpers ---
    from datetime import timedelta
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    now_str = now.strftime('%Y-%m-%d')
    yesterday_str = yesterday.strftime('%Y-%m-%d')

    return render_template('documents.html',
                           proposals=docs_proposals,
                           invoices=docs_invoices,
                           images=docs_images,
                           presentations=docs_presentations,
                           videos=docs_video_prompts,
                           now_str=now_str,
                           yesterday_str=yesterday_str,
                           current_page='documents')

@web_bp.route('/delete_document/<int:doc_id>', methods=['POST'])
@_login_required
def delete_document(doc_id):
    
    doc = db.session.get(GeneratedDocument, doc_id)
    if not doc:
        return jsonify({"success": False, "error": "Document not found"}), 404
        
    if doc.user_phone != session['user_phone']:
        return jsonify({"success": False, "error": "Forbidden"}), 403
        
    try:
        # 1. Borrar archivo físico
        try:
            # doc.file_path es relativo: /static/generated_docs/file.pdf
            # Convertir a absoluto
            relative_path = doc.file_path.lstrip('/')
            absolute_path = os.path.join(current_app.root_path, relative_path)
            if os.path.exists(absolute_path):
                os.remove(absolute_path)
        except Exception as e:
            logger.warning("Error borrando archivo físico %s: %s", doc.file_path, e)
            # Continuamos para borrar de DB aunque falle disco
            
        # 2. Borrar de DB
        db.session.delete(doc)
        db.session.commit()
        return jsonify({"success": True})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

@web_bp.route('/wizard')
@_web_login_required
def wizard():
    user_phone = session['user_phone']
    profile = BusinessProfile.query.filter_by(user_phone=user_phone).first()
    
    if not profile:
        class MockProfile:
            business_name = ""
            static_knowledge = {}
            logo_path = None
        profile = MockProfile()
    elif not profile.static_knowledge:
        profile.static_knowledge = {}

    return render_template('wizard.html', current_user=profile, current_page='wizard')

@web_bp.route('/save_config', methods=['POST'])
@_web_login_required
def save_config():
    user_phone = session['user_phone']
    
    # 2. Recuperar y sanitizar datos del formulario
    # Los campos de texto libre se limpian para evitar prompt injection:
    # se eliminan etiquetas HTML y se truncan a longitud razonable.
    def _clean(value, max_len=500):
        if not value:
            return ''
        value = re.sub(r'<[^>]+>', '', str(value))
        value = re.sub(
            r'(?i)(ignore|ignora|olvida|forget|disregard|override|system:|<\|im_start\|>|<\|im_end\|>)',
            '[FILTRADO]', value
        )
        return value.strip()[:max_len]

    data = request.form
    b_name          = _sanitize_business_name(data.get('business_name', ''))
    sector          = _clean(data.get('sector'), 100)
    tone            = _clean(data.get('tone'), 50)
    schedule        = _clean(data.get('schedule'), 300)
    payment_methods = _clean(data.get('payment_methods'), 200)
    services        = _clean(data.get('services'), 500)
    instructions    = _clean(data.get('business_instructions', ''), 1000)
    faq             = _clean(data.get('faq', ''), 1000)
    return_policy   = _clean(data.get('return_policy', ''), 500)
    support_contact = _clean(data.get('support_contact', ''), 200)
    delivery_time   = _clean(data.get('delivery_time', ''), 200)
    warranty_info   = _clean(data.get('warranty_info', ''), 300)
    nif             = _clean(data.get('nif', ''), 20)
    address         = _clean(data.get('address', ''), 200)

    # 3. Lógica: Construir System Prompt
    cs_block = ""
    if any([faq, return_policy, support_contact, delivery_time, warranty_info]):
        cs_block = f"""
    ATENCIÓN AL CLIENTE Y POSTVENTA:
    - Preguntas frecuentes: {faq}
    - Política de devoluciones: {return_policy}
    - Tiempo de entrega/respuesta: {delivery_time}
    - Garantía: {warranty_info}
    - Contacto de soporte: {support_contact}
    """

    generated_system_prompt = f"""
    Eres el asistente virtual de {b_name}, un negocio del sector {sector}.
    Tu tono debe ser {tone}.

    INFORMACIÓN CLAVE:
    - Horario: {schedule}
    - Pagos aceptados: {payment_methods}
    - Servicios principales: {services}

    INSTRUCCIONES EXTRA:
    {instructions}
    {cs_block}
    OBJETIVO:
    Responde a las dudas de los clientes basándote ÚNICAMENTE en esta información.
    Si te preguntan algo que no sabes, pide amablemente que contacten por teléfono.
    """
    
    # 3.5 Guardar Logo si existe
    logo_path = None
    if 'logo_file' in request.files:
        file = request.files['logo_file']
        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename = secure_filename(f"logo_{user_phone}_{file.filename}")
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'logos')
            os.makedirs(upload_dir, exist_ok=True)
            file.save(os.path.join(upload_dir, filename))
            logo_path = f"/static/uploads/logos/{filename}"
    
    # 4. Guardar en Base de Datos
    profile = BusinessProfile.query.filter_by(user_phone=user_phone).first()
    
    static_data = {
        "sector": sector,
        "tone": tone,
        "schedule": schedule,
        "payment_methods": payment_methods,
        "services": services,
        "instructions": instructions,
        "faq": faq,
        "return_policy": return_policy,
        "support_contact": support_contact,
        "delivery_time": delivery_time,
        "warranty_info": warranty_info,
        "nif": nif,
        "address": address,
    }
    
    if profile:
        # Actualizar existente
        profile.business_name = b_name
        profile.system_prompt = generated_system_prompt.strip()
        profile.static_knowledge = static_data
        if logo_path:
            profile.logo_path = logo_path
        # Crear dict nuevo para que SQLAlchemy detecte el cambio en columna JSON
        profile.features = {**(profile.features or {}), 'bot_enabled': True}
    else:
        # Crear nuevo
        new_profile = BusinessProfile(
            user_phone=user_phone,
            business_name=b_name,
            system_prompt=generated_system_prompt.strip(),
            static_knowledge=static_data,
            logo_path=logo_path,
            features={'bot_enabled': True}
        )
        db.session.add(new_profile)
    
    db.session.commit()

    # La ficha cambia el system prompt: las respuestas cacheadas ya no valen
    from modules.services.response_cache import response_cache
    response_cache.invalidate(user_phone)

    # Re-indexar conocimiento del wizard en pgvector (background)
    import threading
    _app = current_app._get_current_object()
    _phone = user_phone
    _sk = static_data
    _sp = generated_system_prompt.strip()

    def _reindex():
        with _app.app_context():
            try:
                from modules.services.embeddings import ingest_wizard_chunks
                ingest_wizard_chunks(_phone, _sk, _sp)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Error re-indexando wizard: %s", e)

    threading.Thread(target=_reindex, daemon=True).start()

    flash('¡Configuración guardada y Agente IA actualizado!', 'success')
    return redirect(url_for('web.dashboard'))

@web_bp.route('/agents')
@_web_login_required
def agents_page():
    user = BusinessProfile.query.filter_by(user_phone=session['user_phone']).first()
    if user.active_agents is None:
        user.active_agents = []
    return render_template('agents.html', current_user=user, current_page='agents')

@web_bp.route('/marketing')
@_web_login_required
def marketing_page():
    return render_template('marketing.html', current_page='marketing')

def _get_expense_account(concept: str, provider: str = "") -> str:
    """Devuelve el código de cuenta PGC más adecuado según el concepto del gasto.
    Fuente: Plan General Contable español (RD 1514/2007), Grupo 6 - Compras y gastos.
    Fallback: 629 Otros servicios.
    """
    import re

    text = f"{concept or ''} {provider or ''}".lower()
    words = set(re.findall(r"[a-záéíóúüñ]+", text))

    # Comprobación por palabra completa para evitar falsos positivos ("gas" en "gasolina")
    def match(keywords):
        for k in keywords:
            kw = k.lower()
            if " " in kw:          # frases: substring match
                if kw in text:
                    return True
            else:                   # palabras sueltas: match exacto
                if kw in words:
                    return True
        return False

    # El orden importa: reglas más específicas primero
    rules = [
        ("621", ["arrendamiento", "alquiler", "renting"]),
        # Antes que 622: "comisión de mantenimiento" de una cuenta es un servicio
        # bancario, no una reparación (caso detectado por mlops/eval_agents.py).
        ("626", ["banco", "comision", "comisión", "tarjeta", "tpv",
                 "leasing", "prestamo", "préstamo"]),
        ("622", ["reparacion", "reparación", "mantenimiento", "conservacion",
                 "conservación", "limpieza", "fontanero", "electricista",
                 "pintura", "obra"]),
        ("623", ["asesoria", "asesoría", "abogado", "gestoria", "gestoría",
                 "notario", "consultoria", "consultoría", "auditoria",
                 "auditoría", "arquitecto", "ingeniero", "perito"]),
        ("624", ["transporte", "mensajeria", "mensajería", "courier",
                 "envio", "envío", "seur", "glovo", "uber", "taxi",
                 "gasolina", "combustible", "parking", "autopista", "peaje"]),
        ("625", ["seguro", "prima", "poliza", "póliza"]),
        ("627", ["publicidad", "marketing", "anuncio", "propaganda",
                 "google ads", "meta ads", "instagram",
                 "diseño grafico", "diseño gráfico", "fotografia", "fotografía"]),
        ("628", ["luz", "electricidad", "agua", "gas natural", "internet",
                 "fibra", "telefono", "teléfono", "movil", "móvil", "suministro"]),
    ]

    for account, keywords in rules:
        if match(keywords):
            return account

    return "629"  # Otros servicios


@web_bp.route('/export_excel')
@_web_login_required
def export_excel():
    user_phone = session['user_phone']
    
    # Recuperar perfil para el nombre del archivo
    profile = BusinessProfile.query.filter_by(user_phone=user_phone).first()
    business_name = profile.business_name if profile and profile.business_name else f"Zeptai_{user_phone}"

    # 2. Query a Base de Datos (Filtrado por usuario)
    tickets = Ticket.query.filter_by(user_phone=user_phone).order_by(Ticket.date.desc()).all()
    
    if not tickets:
        flash('No hay tickets para exportar.', 'warning')
        return redirect(url_for('web.dashboard'))
    
    # 3. Preparar Datos para DataFrame
    data_list = []
    base_url = request.host_url.rstrip('/') 
    urls = [] # Lista auxiliar para guardar las URLs
    
    for t in tickets:
        # Calcular Cuota IVA (Fee) si falta
        fee = t.fee
        if (fee is None or fee == 0) and t.base and t.tax_percent:
             fee = t.base * (t.tax_percent / 100)

        # Normalizar fecha a date (sin hora) para evitar errores de serialización en xlsxwriter
        raw_date = t.date
        if raw_date is not None and hasattr(raw_date, 'date'):
            raw_date = raw_date.date()  # datetime → date
        month_val = raw_date.month if raw_date else None
        year_val  = raw_date.year  if raw_date else None

        # Construir URL de imagen
        full_img_url = ""
        if t.image_path:
            if t.image_path.startswith(('http://', 'https://')):
                full_img_url = t.image_path
            else:
                full_img_url = f"{base_url}{t.image_path}"

        urls.append(full_img_url)

        data_list.append({
            "ID": t.id,
            "Date": raw_date,
            "Month": month_val,
            "Year": year_val,
            "NIF": t.nif,
            "Name": t.provider, # Mapeado desde provider
            "Ticket_Number": t.ticket_number,
            "Concept": t.concept,
            "Expense_Account": _get_expense_account(t.concept, t.provider),
            "Base": t.base,
            "%IVA": t.tax_percent,
            "Fee": fee,
            "Total": t.total,
            "Enlace Imagen": "Ver Recibo" if full_img_url else ""
        })
        
    # 4. Crear DataFrame y Excel con XlsxWriter
    column_order = ["ID", "Date", "Month", "Year", "NIF", "Name", "Ticket_Number", "Concept", "Expense_Account", "Base", "%IVA", "Fee", "Total", "Enlace Imagen"]
    
    df = pd.DataFrame(data_list)
    # Asegurarse que todas las columnas existan
    for col in column_order:
        if col not in df.columns:
            df[col] = None
            
    if not df.empty:
        df = df[column_order]
    
    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='xlsxwriter', date_format='DD/MM/YYYY', datetime_format='DD/MM/YYYY') as writer:
        sheet_name = 'Gastos'
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        
        workbook  = writer.book
        worksheet = writer.sheets[sheet_name]
        
        # --- ESTILOS ---
        
        # 1. Cabecera (Azul Oscuro, Negrita, Blanco)
        header_format = workbook.add_format({
            'bold': True,
            'text_wrap': True,
            'valign': 'top',
            'fg_color': '#1f4e78', # Business Blue
            'font_color': '#ffffff',
            'border': 1
        })
        
        # 2. Moneda (€)
        currency_format = workbook.add_format({'num_format': '#,##0.00 "€"'})
        
        # 3. Fecha Centrada
        date_format = workbook.add_format({'num_format': 'dd/mm/yyyy', 'align': 'center'})
        
        # 4. Centro
        center_format = workbook.add_format({'align': 'center'})
        
        # 5. Enlaces
        link_format = workbook.add_format({'font_color': 'blue', 'underline': 1})
        
        # Aplicar formato a columnas (0-index based)
        # ID (A)
        worksheet.set_column('A:A', 8, center_format)
        # Date (B)
        worksheet.set_column('B:B', 12, date_format)
        # Month (C)
        worksheet.set_column('C:C', 6, center_format)
        # Year (D)
        worksheet.set_column('D:D', 6, center_format)
        # NIF (E)
        worksheet.set_column('E:E', 12, center_format)
        # Name (F) - Provider
        worksheet.set_column('F:F', 25)
        # Ticket_Number (G)
        worksheet.set_column('G:G', 15, center_format)
        # Concept (H)
        worksheet.set_column('H:H', 30)
        # Expense_Account (I)
        worksheet.set_column('I:I', 10, center_format)
        # Base (J)
        worksheet.set_column('J:J', 12, currency_format)
        # %IVA (K)
        worksheet.set_column('K:K', 8, center_format)
        # Fee (L)
        worksheet.set_column('L:L', 12, currency_format)
        # Total (M)
        worksheet.set_column('M:M', 15, currency_format)
        # Enlace (N)
        worksheet.set_column('N:N', 15, center_format)

        # Sobreescribir Cabeceras con Estilo
        for col_num, value in enumerate(df.columns.values):
            worksheet.write(0, col_num, value, header_format)
            
        # Re-escribir enlaces
        link_col_idx = 13 # Columna N (0-based index 13)
        for i, url in enumerate(urls):
            if url:
                worksheet.write_url(i + 1, link_col_idx, url, link_format, string="Ver Recibo")
        
    output.seek(0)
    
    # Nombre de archivo dinámico (Con TIMESTAMP para cache busting)
    import re
    safe_name = re.sub(r'[^a-zA-Z0-9]', '', business_name)
    date_str = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    filename = f"Gastos_{safe_name}_{date_str}.xlsx"
    
    logger.info("Generando Excel: %s", filename)
    
    return send_file(output, as_attachment=True, download_name=filename, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

def _seed_demo_data_for_user(user_phone):
    """Inserta tickets y notificaciones de ejemplo para un usuario nuevo."""
    from core.db_models import Ticket, Notification, Grant
    from datetime import timedelta
    import random

    now = datetime.now()
    providers = [
        ("Mercadona", 84.20), ("Repsol Combustible", 120.00), ("El Corte Ingles", 230.50),
        ("Amazon Business", 67.99), ("Iberdrola Luz", 145.30), ("Telefonica", 89.00),
        ("Proveedor Materia Prima", 320.00), ("Seguridad Social", 280.00),
        ("Alquiler Local", 950.00), ("Material Oficina", 45.60),
    ]
    existing = Ticket.query.filter_by(user_phone=user_phone).count()
    if existing < 3:
        for i, (provider, total) in enumerate(providers):
            days_ago = random.randint(0, 60)
            t = Ticket(
                user_phone=user_phone,
                provider=provider,
                total=total,
                base=round(total / 1.21, 2),
                tax_percent=21,
                concept=f"Compra {provider}",
                date=now - timedelta(days=days_ago),
                status='processed',
            )
            db.session.add(t)

    # Notificación de bienvenida
    existing_notif = Notification.query.filter_by(user_phone=user_phone, type='info').first()
    if not existing_notif:
        db.session.add(Notification(
            user_phone=user_phone,
            title="Bienvenido a Zeptai",
            message="Tu cuenta esta lista. Configura tu asistente IA y activa los agentes proactivos desde el menu.",
            type='info',
        ))
    db.session.commit()


@web_bp.route('/demo')
@_web_login_required
def demo_panel():
    return render_template('demo_panel.html')

@web_bp.route('/demo/seed_data', methods=['POST'])
@_web_login_required
def demo_seed():
    from seed_demo import generate_fake_history
    generate_fake_history(session['user_phone'])
    flash('✅ Datos históricos inyectados.', 'success')
    return redirect(url_for('web.demo_panel'))

@web_bp.route('/demo/seed_grants', methods=['POST'])
@_login_required
def demo_seed_grants():
    from core.db_models import Grant
    grants_data = [
        {
            "title": "Kit Digital — Digitalización de Pymes (Seg. II)",
            "description": "Ayudas para digitalizar procesos mediante IA, automatización y presencia online. Especialmente indicado para empresas de tecnología y servicios.",
            "sector_focus": "Tecnología",
            "amount": "hasta 12.000€",
            "deadline": "30/09/2026",
            "link": "https://www.acelerapyme.gob.es/kit-digital",
        },
        {
            "title": "CDTI — Proyectos de I+D+i en IA Generativa",
            "description": "Financiación no reembolsable para proyectos de investigación y desarrollo en inteligencia artificial, machine learning y automatización de procesos.",
            "sector_focus": "General",
            "amount": "hasta 250.000€",
            "deadline": "15/11/2026",
            "link": "https://www.cdti.es/",
        },
        {
            "title": "Comunidad de Madrid — Emprendimiento Tecnológico",
            "description": "Subvención para startups tecnológicas con sede en Madrid que desarrollen soluciones SaaS o plataformas digitales con impacto social.",
            "sector_focus": "General",
            "amount": "hasta 50.000€",
            "deadline": "01/10/2026",
            "link": "https://www.comunidad.madrid/servicios/empresa",
        },
    ]
    created = 0
    for g in grants_data:
        existing = Grant.query.filter_by(title=g["title"]).first()
        if not existing:
            grant = Grant(**g, notified_phones=[])
            db.session.add(grant)
            created += 1
    db.session.commit()
    return jsonify({"ok": True, "created": created, "total": len(grants_data)})


@web_bp.route('/demo/trigger/<agent_name>', methods=['POST'])
@_login_required
def demo_trigger(agent_name):
    user = BusinessProfile.query.filter_by(user_phone=session['user_phone']).first()
    if not user:
        return jsonify({"error": "Usuario no encontrado."}), 404
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    try:
        if agent_name == 'coach':
            from modules.proactive.business_health import BusinessCoachAgent
            from core.db_models import Ticket
            ticket_count = Ticket.query.filter_by(user_phone=user.user_phone).count()
            if ticket_count == 0:
                msg = "No hay tickets en la base de datos. Ejecuta primero el Paso 1 (Generar Datos de Demo)."
                return jsonify({"error": msg}), 400 if is_ajax else (flash(msg, 'error'), redirect(url_for('web.demo_panel')))[1]
            BusinessCoachAgent().run_daily_analysis(user)

        elif agent_name == 'hunter':
            from core.db_models import Grant
            from modules.proactive.grant_hunter import GrantHunterAgent
            if Grant.query.count() == 0:
                msg = "No hay subvenciones en la base de datos. Ejecuta primero el Paso 1 (Generar Datos de Demo)."
                return jsonify({"error": msg}), 400 if is_ajax else (flash(msg, 'error'), redirect(url_for('web.demo_panel')))[1]
            for grant in Grant.query.all():
                phones = list(grant.notified_phones or [])
                if user.user_phone in phones:
                    phones.remove(user.user_phone)
                    grant.notified_phones = phones
            db.session.commit()
            GrantHunterAgent().check_new_grants(user)

        elif agent_name == 'networker':
            from core.db_models import SynergyMatch
            from modules.proactive.networker import SynergyAgent

            SynergyMatch.query.filter(
                (SynergyMatch.user_a_phone == user.user_phone) |
                (SynergyMatch.user_b_phone == user.user_phone)
            ).delete(synchronize_session=False)
            db.session.commit()

            PARTNER_PHONE = 'demo_partner_tfm'
            partner = BusinessProfile.query.filter_by(user_phone=PARTNER_PHONE).first()
            if not partner:
                partner = BusinessProfile(user_phone=PARTNER_PHONE)
                db.session.add(partner)
            partner.business_name     = 'CreativaMente Marketing'
            partner.email             = 'demo@creativamente.es'
            partner.password_hash     = 'demo'
            partner.static_knowledge  = {
                'sector':   'Marketing Digital',
                'services': 'Campanas publicitarias, SEO, Redes sociales, Estrategia digital para startups tech',
                'schedule': 'L-V 9:00-19:00',
                'tone':     'Entusiasta',
            }
            db.session.commit()

            knowledge = user.static_knowledge or {}
            spending = (
                f"{user.business_name} trabaja en {knowledge.get('sector', 'Servicios')}. "
                f"Servicios: {knowledge.get('services', '')}. "
                "Necesita visibilidad digital y captacion de clientes."
            )
            agent  = SynergyAgent()
            result = agent._analyze_synergy_deep(user, spending, partner)
            if result:
                # En la demo el match se guarda aunque no llegue al umbral de 80 del
                # agente diario, para poder ensenar el flujo completo, pero con la
                # puntuacion que devolvio el modelo: no se maquilla.
                agent._save_match(user, partner, result)
                agent._notify_intro(user, partner, result['reason'])

        else:
            return jsonify({"error": f"Agente '{agent_name}' no reconocido."}), 400

        if is_ajax:
            return jsonify({"ok": True})
        flash(f'Agente {agent_name} ejecutado con exito.', 'success')
    except Exception as e:
        logger.exception("Error en demo_trigger para agente '%s'", agent_name)
        if is_ajax:
            return jsonify({"error": str(e)}), 500
        flash(f'Error: {e}', 'error')
    return redirect(url_for('web.demo_panel'))


# --- THE COUNCIL ROUTES ---
@web_bp.route('/council')
@_web_login_required
def council_page():
    return render_template('council.html', current_page='council')


@web_bp.route('/metrics')
@_web_login_required
def metrics_page():
    from core.auth import is_admin
    return render_template('metrics.html', current_page='metrics', is_admin=is_admin())


@web_bp.route('/eval')
@_web_login_required
def eval_page():
    return render_template('eval.html', current_page='eval')


@web_bp.route('/observabilidad')
@_web_login_required
def observabilidad_page():
    from core.auth import is_admin
    if not is_admin():
        flash('Acceso restringido a administradores.', 'error')
        return redirect(url_for('web.dashboard'))
    grafana_url    = os.environ.get('GRAFANA_URL',    'http://localhost:3000')
    jaeger_url     = os.environ.get('JAEGER_URL',     'http://localhost:16686')
    prometheus_url = os.environ.get('PROMETHEUS_URL', 'http://localhost:9090')
    return render_template(
        'observabilidad.html',
        current_page='observabilidad',
        grafana_url=grafana_url,
        jaeger_url=jaeger_url,
        prometheus_url=prometheus_url,
    )


@web_bp.route('/networking')
@_web_login_required
def networking():
    user_phone = session['user_phone']
    matches = SynergyMatch.query.filter(
        (SynergyMatch.user_a_phone == user_phone) |
        (SynergyMatch.user_b_phone == user_phone)
    ).order_by(SynergyMatch.created_at.desc()).all()

    # Enriquecer con nombre de la empresa partner
    enriched = []
    for m in matches:
        partner_phone = m.user_b_phone if m.user_a_phone == user_phone else m.user_a_phone
        partner = BusinessProfile.query.filter_by(user_phone=partner_phone).first()
        enriched.append({
            'match': m,
            'partner_name': partner.business_name if partner else partner_phone,
            'partner_sector': (partner.static_knowledge or {}).get('sector', '') if partner else '',
        })

    return render_template('networking.html', current_page='networking', matches=enriched)


@web_bp.route('/networking/contact/<int:match_id>', methods=['POST'])
@_login_required
def networking_contact(match_id):

    user_phone = session['user_phone']
    match = SynergyMatch.query.get_or_404(match_id)

    # Verificar que el usuario es parte del match
    if user_phone not in (match.user_a_phone, match.user_b_phone):
        return jsonify({"error": "Forbidden"}), 403

    # Evitar doble envío
    if match.status == 'contacted':
        return jsonify({"ok": True, "msg": "Ya enviado anteriormente"})

    # Obtener datos del solicitante y del partner
    user    = BusinessProfile.query.filter_by(user_phone=user_phone).first()
    partner_phone = match.user_b_phone if match.user_a_phone == user_phone else match.user_a_phone
    partner = BusinessProfile.query.filter_by(user_phone=partner_phone).first()

    # Cambiar estado del match
    match.status = 'contacted'
    db.session.commit()

    # Enviar email si el partner tiene email real
    try:
        from app import mail
        recipient = partner.email if partner and partner.email and '@' in partner.email else None
        sender_email = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@zeptai.com')

        if recipient and not recipient.endswith('creativamente.es'):  # excluir demo ficticia
            msg = Message(
                subject=f"🤝 {user.business_name} quiere conectar contigo en Zeptai",
                sender=sender_email,
                recipients=[recipient],
                body=(
                    f"Hola,\n\n"
                    f"{user.business_name} ha visto tu perfil en Zeptai y cree que podéis colaborar.\n\n"
                    f"Motivo de la sinergia detectada por IA:\n{match.reason}\n\n"
                    f"Puedes responder directamente a este email para iniciar la conversación.\n\n"
                    f"— El equipo de Zeptai"
                )
            )
            mail.send(msg)

        # Notificación in-app al propio usuario confirmando el contacto
        from modules.services.notification import NotificationService
        NotificationService.send_in_app(
            user_phone=user_phone,
            title="📨 Solicitud enviada",
            message=f"Hemos notificado a {partner.business_name if partner else 'la empresa'} tu interés en colaborar.",
            type="info",
            link="/networking"
        )
        ActivityLog.log(user_phone, "Networker", f"Contacto solicitado con {partner.business_name if partner else partner_phone}")

    except Exception as e:
        logger.warning("networking_contact: error enviando email: %s", e)

    return jsonify({"ok": True})


@web_bp.route('/agenda')
@_web_login_required
def agenda():
    return render_template('agenda.html', current_page='agenda')


@web_bp.route('/privacy-policy')
def privacy_policy():
    return render_template('privacy_policy.html')


@web_bp.route('/compliance')
def compliance():
    return render_template('compliance.html')


@web_bp.route('/codigo-conducta')
def codigo_conducta():
    return render_template('codigo_conducta.html')


@web_bp.route('/agenda/events')
def agenda_events():
    if not session.get('user_phone'):
        return jsonify([])
    from core.db_models import Appointment
    user_phone = session['user_phone']
    appointments = Appointment.query.filter_by(business_phone=user_phone).all()
    events = []
    for a in appointments:
        start = f"{a.date}T{a.time}:00"
        # end_time: si no existe, 1 hora después del inicio
        if a.end_time:
            end = f"{a.date}T{a.end_time}:00"
        else:
            from datetime import datetime, timedelta
            end_dt = datetime.strptime(f"{a.date}T{a.time}", "%Y-%m-%dT%H:%M") + timedelta(hours=1)
            end = end_dt.strftime("%Y-%m-%dT%H:%M:00")
        events.append({
            'id': a.id,
            'title': a.client_name or 'Cita',
            'start': start,
            'end': end,
            'extendedProps': {
                'client_phone': a.client_phone or '',
                'start_time': a.time,
                'end_time': a.end_time or end_dt.strftime("%H:%M") if not a.end_time else a.end_time,
            }
        })
    return jsonify(events)


@web_bp.route('/chatbot-cliente')
@_web_login_required
def chatbot_cliente():
    business_name = session.get('business_name', 'Mi Negocio')
    return render_template('chatbot_cliente.html', current_page='chatbot_cliente', business_name=business_name)



