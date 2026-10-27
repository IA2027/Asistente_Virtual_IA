
import json
import os
import secrets
import sqlite3
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask, jsonify, redirect, render_template,
    request, session, url_for
)
from google import genai

# --------------------------------------------------
# CONFIGURACION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)

DB_PATH = BASE_DIR / "recepcionista.db"

CLINIC_NAME = os.getenv("CLINIC_NAME", "Centro de Atención")
CLINIC_HOURS = os.getenv("CLINIC_HOURS", "Consultar horarios")
CLINIC_SPECIALTIES = os.getenv(
    "CLINIC_SPECIALTIES",
    "Clínica médica, Dermatología, Cardiología"
)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# --------------------------------------------------
# BASE DE DATOS
# --------------------------------------------------

def conectar_db():
    conexion = sqlite3.connect(DB_PATH, timeout=10)
    conexion.row_factory = sqlite3.Row
    return conexion


def inicializar_db():
    with conectar_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS app_sessions (
                session_id TEXT PRIMARY KEY,
                flow_step TEXT NOT NULL DEFAULT '',
                flow_data TEXT NOT NULL DEFAULT '{}'
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT
                    (datetime('now', 'localtime'))
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                patient_name TEXT NOT NULL,
                specialty TEXT NOT NULL,
                appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pendiente',
                created_at TEXT NOT NULL DEFAULT
                    (datetime('now', 'localtime'))
            )
        """)


def obtener_sesion_chat(session_id):
    with conectar_db() as db:
        fila = db.execute(
            "SELECT * FROM app_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        if fila is None:
            db.execute(
                """INSERT INTO app_sessions
                   (session_id, flow_step, flow_data)
                   VALUES (?, '', '{}')""",
                (session_id,)
            )
            return {"flow_step": "", "flow_data": {}}

        return {
            "flow_step": fila["flow_step"],
            "flow_data": json.loads(fila["flow_data"])
        }


def guardar_estado_chat(session_id, step, data):
    with conectar_db() as db:
        db.execute("""
            UPDATE app_sessions
            SET flow_step = ?, flow_data = ?
            WHERE session_id = ?
        """, (step, json.dumps(data, ensure_ascii=False), session_id))


def guardar_mensaje(session_id, role, content):
    with conectar_db() as db:
        db.execute("""
            INSERT INTO messages (session_id, role, content)
            VALUES (?, ?, ?)
        """, (session_id, role, content))


# --------------------------------------------------
# RESPUESTAS DE GEMINI
# --------------------------------------------------

def consultar_gemini(pregunta):
    if gemini_client is None:
        return (
            "La API de Gemini todavía no está configurada. "
            "Revisá la variable GEMINI_API_KEY del archivo .env."
        )

    prompt = f"""
Sos el asistente virtual de {CLINIC_NAME}.

Respondé en español, de forma clara, cordial y breve.

Información autorizada:
- Horarios: {CLINIC_HOURS}
- Especialidades informadas: {CLINIC_SPECIALTIES}

Reglas:
- No inventes médicos, precios, horarios ni disponibilidad de turnos.
- No diagnostiques enfermedades ni indiques tratamientos.
- Si preguntan por una urgencia médica, indicá que contacten
  al servicio local de emergencias.
- Para pedir un turno, explicá que podés registrar una solicitud,
  pero que el personal debe revisarla.
- No afirmes que un turno está confirmado.

Consulta del usuario:
{pregunta}
"""

    try:
        respuesta = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        texto = respuesta.text
        return texto.strip() if texto else (
            "No pude generar una respuesta. Intentá nuevamente."
        )
    except Exception as error:
        print("Error de Gemini:", error)
        return (
            "No pude conectarme con Gemini en este momento. "
            "Verificá la API y volvé a intentarlo."
        )


# --------------------------------------------------
# FLUJO DE SOLICITUD DE TURNOS
# --------------------------------------------------

def procesar_mensaje(session_id, mensaje):
    texto = mensaje.strip()
    texto_min = texto.lower()

    estado = obtener_sesion_chat(session_id)
    paso = estado["flow_step"]
    datos = estado["flow_data"]

    if texto_min in {"cancelar", "cancelar solicitud", "salir"}:
        guardar_estado_chat(session_id, "", {})
        return "Cancelé el proceso de solicitud. ¿En qué más puedo ayudarte?"

    # Inicio del flujo
    if not paso and any(
        palabra in texto_min
        for palabra in ("turno", "reservar", "sacar una cita", "solicitar cita")
    ):
        guardar_estado_chat(session_id, "nombre", {})
        return "Vamos a registrar una solicitud de turno. ¿Cuál es tu nombre y apellido?"

    if paso == "nombre":
        if len(texto) < 3:
            return "Ingresá tu nombre y apellido para continuar."
        datos["nombre"] = texto
        guardar_estado_chat(session_id, "especialidad", datos)
        return (
            f"Gracias, {texto}. ¿Qué especialidad necesitás? "
            f"Opciones informadas: {CLINIC_SPECIALTIES}."
        )

    if paso == "especialidad":
        datos["especialidad"] = texto
        guardar_estado_chat(session_id, "fecha", datos)
        return (
            "¿Qué fecha preferís? Usá el formato AAAA-MM-DD, "
            "por ejemplo 2026-11-05."
        )

    if paso == "fecha":
        # Validación estricta de formato y fecha real.
        from datetime import datetime
        try:
            fecha = datetime.strptime(texto, "%Y-%m-%d").date()
            if fecha.isoformat() != texto:
                raise ValueError("Formato inválido")
        except ValueError:
            return "La fecha no es válida. Usá el formato AAAA-MM-DD, por ejemplo 2026-11-05."

        datos["fecha"] = texto
        guardar_estado_chat(session_id, "hora", datos)
        return "¿Qué horario preferís? Usá el formato de 24 horas, por ejemplo 10:30."

    if paso == "hora":
        from datetime import datetime
        try:
            hora = datetime.strptime(texto, "%H:%M").strftime("%H:%M")
        except ValueError:
            return "La hora no es válida. Escribí un horario como 09:30 o 14:00."

        datos["hora"] = hora

        with conectar_db() as db:
            db.execute("""
                INSERT INTO appointments
                (session_id, patient_name, specialty,
                 appointment_date, appointment_time, status)
                VALUES (?, ?, ?, ?, ?, 'Pendiente')
            """, (
                session_id,
                datos["nombre"],
                datos["especialidad"],
                datos["fecha"],
                datos["hora"]
            ))

        guardar_estado_chat(session_id, "", {})
        return (
            "Tu solicitud quedó registrada correctamente.\n\n"
            f"Nombre: {datos['nombre']}\n"
            f"Especialidad: {datos['especialidad']}\n"
            f"Fecha solicitada: {datos['fecha']}\n"
            f"Horario solicitado: {datos['hora']}\n\n"
            "Estado: PENDIENTE DE REVISIÓN. Esto no confirma un turno. "
            "El personal deberá revisar la disponibilidad."
        )

    return consultar_gemini(texto)


# --------------------------------------------------
# PAGINAS
# --------------------------------------------------

@app.route("/")
def inicio():
    return render_template(
        "index.html",
        clinic_name=CLINIC_NAME
    )


@app.route("/api/chat", methods=["POST"])
def api_chat():
    cuerpo = request.get_json(silent=True) or {}
    mensaje = cuerpo.get("message", "")
    session_id = cuerpo.get("session_id", "")

    if not isinstance(mensaje, str) or not mensaje.strip():
        return jsonify({"error": "Escribí un mensaje."}), 400

    if len(mensaje) > 2000:
        return jsonify({"error": "El mensaje es demasiado largo."}), 400

    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > 100
        or not all(c.isalnum() or c in "-_" for c in session_id)
    ):
        return jsonify({"error": "Identificador de sesión inválido."}), 400

    guardar_mensaje(session_id, "user", mensaje.strip())
    respuesta = procesar_mensaje(session_id, mensaje)
    guardar_mensaje(session_id, "assistant", respuesta)

    return jsonify({"reply": respuesta})


def requiere_admin(funcion):
    @wraps(funcion)
    def decorada(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return funcion(*args, **kwargs)
    return decorada


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None

    if request.method == "POST":
        password = request.form.get("password", "")

        if ADMIN_PASSWORD and secrets.compare_digest(password, ADMIN_PASSWORD):
            session.clear()
            session["admin_logged_in"] = True
            return redirect(url_for("admin_panel"))

        error = "Contraseña incorrecta o ADMIN_PASSWORD sin configurar."

    return render_template("admin_login.html", error=error)


@app.route("/admin/logout", methods=["POST"])
@requiere_admin
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@requiere_admin
def admin_panel():
    with conectar_db() as db:
        turnos = db.execute("""
            SELECT id, patient_name, specialty, appointment_date,
                   appointment_time, status, created_at
            FROM appointments
            ORDER BY id DESC
        """).fetchall()

        total = len(turnos)
        pendientes = sum(t["status"] == "Pendiente" for t in turnos)
        aprobados = sum(t["status"] == "Aprobado" for t in turnos)
        rechazados = sum(t["status"] == "Rechazado" for t in turnos)

    return render_template(
        "admin.html",
        turnos=turnos,
        total=total,
        pendientes=pendientes,
        aprobados=aprobados,
        rechazados=rechazados,
        clinic_name=CLINIC_NAME
    )


@app.route("/admin/turno/<int:turno_id>", methods=["POST"])
@requiere_admin
def cambiar_estado(turno_id):
    nuevo_estado = request.form.get("status", "")

    if nuevo_estado not in {"Aprobado", "Rechazado", "Pendiente"}:
        return "Estado inválido", 400

    with conectar_db() as db:
        db.execute(
            "UPDATE appointments SET status = ? WHERE id = ?",
            (nuevo_estado, turno_id)
        )

    return redirect(url_for("admin_panel"))


# --------------------------------------------------
# EJECUCION
# --------------------------------------------------

inicializar_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
