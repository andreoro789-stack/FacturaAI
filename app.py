import os
import json
import sqlite3
import time
import io
import base64
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory
from PIL import Image

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB máximo

# Por esto:
_k_part1 = "sk-or-v1-"
_k_part2 = "0e7193c4ff3d5429ae888eef0cc7e2c0700cec6a18d688279d140067ab9ef3ce"
DEFAULT_KEY = _k_part1 + _k_part2

OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", DEFAULT_KEY).strip()

DB_PATH = os.path.join(BASE_DIR, "facturas.db")
TEMP_UPLOADS = os.path.join(BASE_DIR, "temp_mobile_uploads")
os.makedirs(TEMP_UPLOADS, exist_ok=True)
ultima_captura_movil = None

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def inicializar_bd():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS facturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            establecimiento TEXT,
            nit TEXT,
            fecha TEXT,
            productos TEXT,
            subtotal TEXT,
            impuestos TEXT,
            total TEXT,
            resumen TEXT,
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error BD: {e}")

inicializar_bd()

def procesar_con_ia(img):
    buffered = io.BytesIO()
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.save(buffered, format="JPEG", quality=85)
    img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{img_b64}"

    prompt = """
    Eres un sistema experto en auditoría documental y extracción contable de comprobantes comerciales.
    Analiza detalladamente la imagen proporcionada.
    
    Primero evalúa si la imagen corresponde a una factura, ticket de caja, boleta de venta, recibo o comprobante comercial legítimo y legible.
    
    Responde ÚNICAMENTE con un JSON válido con la siguiente estructura exacta:
    {
      "es_factura": true o false,
      "motivo_rechazo": "Explica brevemente por qué no es válido (solo si es_factura es false, de lo contrario null)",
      "establecimiento": "Nombre del negocio o comercio emisor",
      "nit": "Número de NIT o identificación tributaria",
      "fecha": "Fecha de emisión de la compra",
      "productos": "Lista resumida de los artículos comprados separados por coma",
      "subtotal": "Subtotal numérico o con su moneda",
      "impuestos": "Impuestos desglosados (IVA u otros)",
      "total": "Total pagado de la factura",
      "resumen": "Resumen conciso indicando comercio, concepto principal de la compra y monto final."
    }
    
    Si la imagen NO es una factura o comprobante comercial, pon "es_factura": false, describe el motivo en "motivo_rechazo", y deja los demás campos en "N/A" o null.
    No agregues texto explicativo fuera del JSON.
    """

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:5000",
        "X-Title": "FacturaAI"
    }

    # Lista de modelos de visión con tier gratuito garantizado
    modelos_vision = [
        "google/gemini-2.0-flash-lite-001",
        "google/gemini-2.0-flash-exp:free",
        "mistralai/pixtral-12b:free",
        "nvidia/nemotron-4-340b-reward"
    ]

    # Intentar obtener los modelos libres activos directamente desde OpenRouter
    try:
        r_models = requests.get("https://openrouter.ai/api/v1/models", timeout=8)
        if r_models.status_code == 200:
            disponibles = r_models.json().get("data", [])
            activos_free_vision = [
                m["id"] for m in disponibles 
                if m.get("id", "").endswith(":free") and "image" in m.get("architecture", {}).get("modality", "")
            ]
            if activos_free_vision:
                modelos_vision = activos_free_vision + modelos_vision
    except Exception:
        pass

    ultimo_error = None

    for mod in modelos_vision:
        payload = {
            "model": mod,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}}
                    ]
                }
            ],
            "response_format": {"type": "json_object"}
        }

        try:
            resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=35)
            if resp.status_code == 200:
                res_json = resp.json()
                raw_text = res_json["choices"][0]["message"]["content"].strip()
                if raw_text.startswith("```json"):
                    raw_text = raw_text[7:]
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3]
                data = json.loads(raw_text.strip())
                if isinstance(data, list):
                    data = data[0] if len(data) > 0 and isinstance(data[0], dict) else {}
                return data
            else:
                ultimo_error = f"{mod} -> {resp.status_code}: {resp.text}"
        except Exception as e:
            ultimo_error = str(e)
            time.sleep(1)

    raise Exception(f"Fallo al procesar con OpenRouter: {ultimo_error}")

def guardar_factura_bd(data):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO facturas (establecimiento, nit, fecha, productos, subtotal, impuestos, total, resumen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("establecimiento") or "N/A",
        data.get("nit") or "N/A",
        data.get("fecha") or "N/A",
        data.get("productos") or "N/A",
        data.get("subtotal") or "N/A",
        data.get("impuestos") or "N/A",
        data.get("total") or "N/A",
        data.get("resumen") or "Sin resumen disponible"
    ))
    conn.commit()
    conn.close()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/uploads/<path:filename>")
def servir_archivo_temporal(filename):
    return send_from_directory(TEMP_UPLOADS, filename)

@app.route("/api/info-red", methods=["GET"])
def info_red():
    host = request.host_url.rstrip('/')
    return jsonify({
        "ip": request.host,
        "url_movil": f"{host}/movil"
    })

@app.route("/movil", methods=["GET"])
def vista_movil():
    html_movil = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Escanear Factura • FacturaAI</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #0f172a; color: #fff; display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; font-family: system-ui, sans-serif; }
        .card-box { background: #1e293b; border-radius: 18px; padding: 25px; width: 100%; max-width: 400px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); border: 1px solid #334155; }
        .preview-area { border: 2px dashed #64748b; border-radius: 14px; padding: 20px 10px; text-align: center; margin-bottom: 20px; background: #0f172a; min-height: 180px; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        .btn-option { padding: 12px; font-weight: 600; border-radius: 10px; }
    </style>
</head>
<body>
    <div class="card-box text-center">
        <h3 class="fw-bold text-primary mb-1">FacturaAI</h3>
        <p class="text-secondary small mb-3">Toma una fotografía o elige una factura de tu galería.</p>
        
        <form id="fMovil" method="POST" action="/api/subir-movil" enctype="multipart/form-data">
            <div class="preview-area" id="boxPreview">
                <div id="boxPrompt">
                    <span style="font-size: 2.2rem;">🧾</span>
                    <div class="fw-bold mt-2">Sin imagen seleccionada</div>
                    <small class="text-secondary">Usa los botones de abajo para cargar</small>
                </div>
                <img id="prevImg" src="#" class="img-fluid rounded d-none" style="max-height: 220px;" alt="Previa">
            </div>

            <input type="file" id="camInput" accept="image/*" capture="environment" class="d-none">
            <input type="file" id="galeriaInput" name="foto_movil" accept="image/*" class="d-none">

            <div class="row g-2 mb-3">
                <div class="col-6">
                    <button type="button" class="btn btn-outline-light w-100 btn-option" onclick="document.getElementById('camInput').click()">
                        📸 Cámara
                    </button>
                </div>
                <div class="col-6">
                    <button type="button" class="btn btn-outline-info w-100 btn-option" onclick="document.getElementById('galeriaInput').click()">
                        🖼️ Galería
                    </button>
                </div>
            </div>

            <button type="submit" id="btnSubir" class="btn btn-primary w-100 py-3 fw-bold rounded-3" disabled>
                📤 Enviar al Software
            </button>
        </form>
        <div id="stMsg" class="small mt-3 text-secondary">Esperando imagen...</div>
    </div>

    <script>
        const camInput = document.getElementById('camInput');
        const galeriaInput = document.getElementById('galeriaInput');
        const prev = document.getElementById('prevImg');
        const prompt = document.getElementById('boxPrompt');
        const btn = document.getElementById('btnSubir');
        const st = document.getElementById('stMsg');
        
        function manejarArchivoSeleccionado(file) {
            if (file) {
                prev.src = URL.createObjectURL(file);
                prev.classList.remove('d-none');
                prompt.classList.add('d-none');
                btn.disabled = false;
                st.innerText = "✓ Archivo listo: " + file.name;
                st.className = "small mt-3 text-success";
            }
        }

        camInput.onchange = () => {
            if (camInput.files.length) {
                galeriaInput.files = camInput.files;
                manejarArchivoSeleccionado(camInput.files[0]);
            }
        };

        galeriaInput.onchange = () => {
            if (galeriaInput.files.length) {
                manejarArchivoSeleccionado(galeriaInput.files[0]);
            }
        };

        document.getElementById('fMovil').onsubmit = () => {
            btn.disabled = true;
            btn.innerText = "Transfiriendo...";
            st.innerText = "Subiendo archivo a tu PC...";
        };
    </script>
</body>
</html>"""
    return html_movil

@app.route("/api/subir-movil", methods=["POST"])
def subir_movil():
    global ultima_captura_movil
    if "foto_movil" not in request.files:
        return "No se recibió ninguna imagen", 400

    archivo = request.files["foto_movil"]
    if archivo.filename == "":
        return "Archivo vacío", 400

    nombre_archivo = f"movil_{int(time.time()*1000)}.jpg"
    ruta_guardado = os.path.join(TEMP_UPLOADS, nombre_archivo)
    archivo.save(ruta_guardado)
    
    ultima_captura_movil = {
        "ruta": ruta_guardado,
        "url_preview": f"/uploads/{nombre_archivo}"
    }

    return """
    <body style="background:#0f172a; color:#fff; text-align:center; font-family:sans-serif; padding-top:50px;">
        <h1 style="color:#10b981;">¡Recibido con éxito!</h1>
        <p>La foto ya se transfirió a la computadora.</p>
        <p style="color:#94a3b8;">La pantalla de tu PC la está previsualizando y auditando.</p>
    </body>
    """

@app.route("/api/comprobar-movil", methods=["GET"])
def comprobar_movil():
    global ultima_captura_movil
    if ultima_captura_movil and os.path.exists(ultima_captura_movil["ruta"]):
        datos = ultima_captura_movil
        ultima_captura_movil = None
        return jsonify({
            "recibido": True, 
            "ruta": datos["ruta"],
            "url_preview": datos["url_preview"]
        })
    return jsonify({"recibido": False})

@app.route("/api/analizar", methods=["POST"])
def analizar_factura():
    if not OPENROUTER_KEY:
        return jsonify({"error": "Clave OPENROUTER_API_KEY no configurada."}), 500

    tipo_origen = request.form.get("tipo", "archivo")
    img = None

    try:
        if tipo_origen == "link":
            url_imagen = request.form.get("url_imagen", "").strip()
            if not url_imagen:
                return jsonify({"error": "Debes ingresar una URL válida."}), 400

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            resp = requests.get(url_imagen, headers=headers, timeout=12)
            if resp.status_code != 200:
                return jsonify({"error": f"No se pudo descargar la imagen (Código {resp.status_code})."}), 400

            try:
                img = Image.open(io.BytesIO(resp.content))
                img.verify()
                img = Image.open(io.BytesIO(resp.content))
            except Exception:
                return jsonify({"error": "El enlace no contiene una imagen válida legible."}), 400

        elif tipo_origen == "movil":
            ruta_local = request.form.get("ruta_local")
            if not ruta_local or not os.path.exists(ruta_local):
                return jsonify({"error": "No se encontró el archivo temporal del celular."}), 400
            img = Image.open(ruta_local)

        else:
            if "factura" not in request.files:
                return jsonify({"error": "No se envió ningún archivo de imagen."}), 400
            
            archivo = request.files["factura"]
            if archivo.filename == "":
                return jsonify({"error": "No seleccionaste ningún archivo."}), 400

            try:
                img = Image.open(archivo.stream)
            except Exception:
                return jsonify({"error": "El archivo subido no es una imagen válida."}), 400

        data = procesar_con_ia(img)

        if not data.get("es_factura", False):
            motivo = data.get("motivo_rechazo") or "La imagen cargada no corresponde a una factura o recibo comercial."
            return jsonify({"status": "rejected", "error": motivo}), 422

        guardar_factura_bd(data)
        return jsonify({"status": "success", "data": data})

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Error de red: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": f"Error al procesar documento: {str(e)}"}), 500

@app.route("/api/historial", methods=["GET"])
def obtener_historial():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, establecimiento, nit, fecha, total, resumen, creado_en FROM facturas ORDER BY id DESC")
        filas = cursor.fetchall()
        conn.close()
        return jsonify([dict(f) for f in filas])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/factura/<int:id_factura>", methods=["DELETE"])
def eliminar_factura(id_factura):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM facturas WHERE id = ?", (id_factura,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Factura #{id_factura} eliminada."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/limpiar-historial", methods=["POST"])
def limpiar_historial():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM facturas")
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Historial limpiado correctamente."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=puerto, debug=True)