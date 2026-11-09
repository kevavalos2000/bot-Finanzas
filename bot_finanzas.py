import json
import logging
import sqlite3
import os
import re
from datetime import datetime, timedelta
from google import genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
DB_NAME = "finanzas.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS gastos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            monto REAL,
            categoria TEXT,
            descripcion TEXT,
            fecha TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id INTEGER PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def registrar_usuario(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO usuarios (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def extraer_con_regex(texto: str):
    match = re.search(r'\$?(\d+([.,]\d+)?)', texto)
    if match:
        monto = float(match.group(1).replace(',', '.'))
        texto_lower = texto.lower()
        categoria = "Otros"
        if any(w in texto_lower for w in ["almuerzo", "comida", "cena", "comer", "hamburguesa", "pizza", "café"]):
            categoria = "Comida"
        elif any(w in texto_lower for w in ["pasaje", "taxi", "bus", "uber", "gasolina"]):
            categoria = "Transporte"
        elif any(w in texto_lower for w in ["internet", "luz", "agua", "telefono"]):
            categoria = "Servicios"
        elif any(w in texto_lower for w in ["cine", "película", "juego", "salida"]):
            categoria = "Entretenimiento"
        
        return {
            "es_gasto": True,
            "monto": monto,
            "categoria": categoria,
            "descripcion": texto
        }
    return None

def procesar_gasto_con_gemini(texto_usuario: str):
    prompt = f"""
    Analiza este texto y extrae un gasto en formato JSON estricto sin markdown:
    {{"es_gasto": true/false, "monto": 0.0, "categoria": "Comida/Transporte/Servicios/Entretenimiento/Compras/Salud/Otros", "descripcion": "detalle"}}
    Texto: "{texto_usuario}"
    """
    try:
        response = client.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt,
        )
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            lines = [l for l in raw_text.splitlines() if not l.startswith("```")]
            raw_text = "\n".join(lines).strip()
            
        datos = json.loads(raw_text)
        if datos.get("es_gasto") and datos.get("monto", 0) > 0:
            return datos
    except Exception as e:
        logging.warning(f"API saturada, usando respaldo local: {e}")
    
    return extraer_con_regex(texto_usuario)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    registrar_usuario(user_id)
    await update.message.reply_text(
        "👋 ¡Hola! Soy tu Agente de Finanzas.\n"
        "Escríbeme un gasto como: *Gasté $10 en almuerzo*\n\n"
        "Te enviaré reportes automáticos diarios.",
        parse_mode="Markdown"
    )

async def registrar_gasto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    user_id = update.effective_user.id
    registrar_usuario(user_id)
    
    datos = procesar_gasto_con_gemini(texto)
    
    if not datos:
        await update.message.reply_text("🤔 No logré interpretar el monto o gasto. Prueba con: 'Gasté $5 en pasaje'.")
        return

    monto = float(datos["monto"])
    categoria = str(datos.get("categoria", "Otros"))
    descripcion = str(datos.get("descripcion", texto))
    fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO gastos (user_id, monto, categoria, descripcion, fecha) VALUES (?, ?, ?, ?, ?)",
        (user_id, monto, categoria, descripcion, fecha_actual)
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ **Gasto registrado**\n💵 Monto: ${monto:.2f}\n🏷️ Categoría: {categoria}\n📝 Detalle: {descripcion}",
        parse_mode="Markdown"
    )

async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT categoria, SUM(monto) FROM gastos WHERE user_id = ? GROUP BY categoria", (user_id,))
    filas = cursor.fetchall()
    cursor.execute("SELECT SUM(monto) FROM gastos WHERE user_id = ?", (user_id,))
    total = cursor.fetchone()[0] or 0.0
    conn.close()

    if not filas:
        await update.message.reply_text("📊 Aún no hay gastos registrados.")
        return

    rep = "📊 **Resumen General:**\n\n"
    for cat, tot in filas:
        rep += f"• {cat}: ${tot:.2f}\n"
    rep += f"\n💰 **Total:** ${total:.2f}"
    await update.message.reply_text(rep, parse_mode="Markdown")

async def borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM gastos WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Registros borrados.")

# Funciones de Reportes Automáticos
async def enviar_reporte_diario(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM usuarios")
    usuarios = cursor.fetchall()
    
    ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    for (user_id,) in usuarios:
        cursor.execute("SELECT categoria, SUM(monto) FROM gastos WHERE user_id = ? AND date(fecha) = ? GROUP BY categoria", (user_id, ayer))
        filas = cursor.fetchall()
        cursor.execute("SELECT SUM(monto) FROM gastos WHERE user_id = ? AND date(fecha) = ?", (user_id, ayer))
        total = cursor.fetchone()[0] or 0.0
        
        if filas:
            rep = f"🌙 **Reporte Diario ({ayer})**:\n\n"
            for cat, tot in filas:
                rep += f"• {cat}: ${tot:.2f}\n"
            rep += f"\n💰 **Total del día:** ${total:.2f}"
            await context.bot.send_message(chat_id=user_id, text=rep, parse_mode="Markdown")
    conn.close()

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Programar reporte diario a las 9:00 PM (21:00)
    app.job_queue.run_daily(enviar_reporte_diario, time=datetime.strptime("21:00:00", "%H:%M:%S").time())

    # Handlers corregidos (usando app.add_handler directamente)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("resumen", resumen))
    app.add_handler(CommandHandler("borrar", borrar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, registrar_gasto))
    
    print("🤖 Agente de Finanzas activo con reportes automáticos...")
    app.run_polling()
