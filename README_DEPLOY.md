# 🚀 Zeptai: Guía de Despliegue Rápido

## 1. Requisitos Previos
*   Python 3.10+
*   PostgreSQL (o SQLite para pruebas)
*   Claves de API: OpenAI, Twilio.

## 2. Instalación Local
```bash
# Clonar repo
git clone https://github.com/alejbrata/ticketia.git
cd ticketia

# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate

# Instalar dependencias
pip install -r requirements.txt

# Configurar variables de entorno (.env)
cp .env.example .env
# EDITA EL .ENV CON TUS CLAVES REALES

# Inicializar Base de Datos (Creará tablas y admin por defecto)
python3 reset_db_full.py
```

## 3. Ejecutar Servidor
```bash
# Iniciar Gunicorn (Producción)
gunicorn TICKETIA_PRO.app:app

# O modo desarrollo
flask run --reload
```

## 4. Pruebas Automáticas (TDD)
Para verificar que los agentes (Grant Hunter, Networking, Coach, Post-Venta) funcionan correctamente:

```bash
python3 TICKETIA_PRO/tests/test_proactive_agents.py
```
✅ Deberías ver "OK" al final.

## 5. Despliegue en la Nube (Railway/Render)
Simplemente conecta este repositorio. El archivo `Procfile` ya está configurado.
Asegúrate de configurar las variables de entorno en el panel de control de tu proveedor.
