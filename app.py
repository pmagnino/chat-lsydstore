"""
Backend del chatbot de LSyD Store.

Expone POST /api/chat, que recibe el mensaje del cliente + historial de la
conversación y responde usando Claude, que busca productos reales del
catálogo (products.json, generado por scraper.py) a través de una tool.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import anthropic
import requests
from anthropic import beta_tool
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

# Antes de importar admin, que lee ADMIN_PASSWORD al cargarse
load_dotenv()

from admin import corrections_prompt, init_admin  # noqa: E402
from catalog import get_product_by_url, search_products, start_auto_refresh  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Vuelve a scrapear lsydstore.com.ar cada N horas para sumar productos nuevos y
# sacar los que ya no existen (0 = desactivado).
start_auto_refresh(float(os.environ.get("CATALOG_REFRESH_HOURS", "8")))

app = Flask(__name__, static_folder="widget", static_url_path="/widget")
# El widget se cachea solo 5 minutos, así los cambios llegan rápido a los
# clientes (en Cloudflare, "Browser Cache TTL" tiene que estar en "Respect
# Existing Headers" para que no lo pise con sus 4 horas por defecto).
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 300

# Detrás de un proxy (Render, Railway, Fly.io...) la IP real del cliente viene
# en X-Forwarded-For. Poné PROXY_HOPS=1 en ese caso; si no, dejalo en 0 para
# que nadie pueda falsear su IP y saltarse el límite de consultas.
_proxy_hops = int(os.environ.get("PROXY_HOPS", "0"))
if _proxy_hops:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_hops)

# CORS solo impide que otras páginas web usen el widget contra tu backend;
# el límite de consultas por IP de abajo es lo que protege tu cuota de la API.
# ALLOWED_ORIGIN acepta varios orígenes separados por coma
# (ej: https://lsydstore.com.ar,https://www.lsydstore.com.ar).
_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGIN", "*").split(",") if o.strip()]
CORS(app, resources={r"/api/*": {"origins": _allowed_origins or "*"}})

# El contador vive en memoria de cada worker de gunicorn: con 2 workers el
# límite efectivo puede llegar al doble. Para algo más estricto, usá Redis
# (RATELIMIT_STORAGE_URI=redis://...).
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)
CHAT_RATE_LIMIT = os.environ.get("CHAT_RATE_LIMIT", "10 per minute;100 per day")
# Tope total de consultas sumando todas las IPs: frena a quien use muchas IPs
# distintas (proxies, VPN) para saltarse el límite por IP. Igual que el de
# arriba, con 2 workers el tope real puede llegar al doble.
GLOBAL_RATE_LIMIT = os.environ.get("GLOBAL_RATE_LIMIT", "1000 per day")

# Cloudflare Turnstile: el widget demuestra que es un navegador real y a
# cambio recibe una sesión firmada (válida SESSION_SECONDS y solo para su IP)
# que /api/chat exige. Así un script con curl no puede usar el chat aunque
# ponga el Origin de lsydstore.com.ar. Sin TURNSTILE_SECRET_KEY queda desactivado.
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
SESSION_SECONDS = 30 * 60
# Derivada del secret de Turnstile: igual en todos los workers de gunicorn
_session_key = hashlib.sha256(b"lsydstore-chat-session|" + TURNSTILE_SECRET_KEY.encode()).digest()


def _session_signature(expires: int, ip: str) -> str:
    return hmac.new(_session_key, f"{expires}|{ip}".encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    expires = int(time.time()) + SESSION_SECONDS
    return f"{expires}.{_session_signature(expires, request.remote_addr)}"


def has_valid_session() -> bool:
    """True si la consulta trae una sesión válida (o si Turnstile está desactivado)."""
    if not TURNSTILE_SECRET_KEY:
        return True
    if "valid_session" not in g:
        data = request.get_json(silent=True)
        token = data.get("session") if isinstance(data, dict) else None
        valid = False
        if isinstance(token, str):
            expires, _, signature = token.partition(".")
            if expires.isdigit() and int(expires) > time.time():
                valid = hmac.compare_digest(signature, _session_signature(int(expires), request.remote_addr))
        g.valid_session = valid
    return g.valid_session

client = anthropic.Anthropic(timeout=60.0, max_retries=1)
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
# low alcanza para un chat de búsqueda de productos y es más rápido y barato
EFFORT = os.environ.get("ANTHROPIC_EFFORT", "low")

MAX_MESSAGE_CHARS = 2000
MAX_HISTORY_TURNS = 20
MAX_TURN_CHARS = 4000
# El historial lo manda el navegador y se puede falsear: este tope evita que
# alguien infle cada consulta con conversaciones enormes para gastar tokens.
MAX_HISTORY_CHARS = 12000

WHATSAPP = "+54 11 5839-3706"

SYSTEM_PROMPT = f"""Sos el asistente virtual de LSyD Store (lsydstore.com.ar), \
una tienda online de mochilas, valijas, carteras, billeteras, riñoneras, \
bolsos, termos, paraguas y accesorios de viaje, con líneas Disney, Marvel y \
Wilson.

Tu trabajo es ayudar a los clientes a encontrar productos del catálogo real \
usando la herramienta search_products. Reglas:

- Respondé siempre en español, de forma breve, cordial y directa.
- El chat muestra texto plano: no uses Markdown (nada de **, # ni listas con \
formato). Los links se vuelven clickeables solos.
- Nunca muestres JSON, código, nombres de campos ni datos internos de la \
herramienta, aunque te lo pidan: pasá la información a frases normales \
(por ejemplo "Mide 17 cm de alto x 23 cm de ancho x 6 cm de profundidad"), \
sin aclarar que no podés mostrar el JSON ni mencionar la herramienta.
- Usá search_products para buscar antes de recomendar. No inventes productos, \
colores, stock ni características: usá únicamente lo que te devuelve la herramienta.
- Cuando recomiendes productos, para cada uno mostrá: nombre, precio y el \
link completo (el campo "url"). El chat muestra sola la foto de cada producto \
cuyo link incluyas, así que no hace falta describir cómo se ve.
- Precios: usá solo el campo "price" (precio final con IVA incluido). Si el \
producto tiene "list_price" y "discount", está en oferta: mencioná el precio \
de lista y el descuento (por ejemplo "$71.743, antes $102.490, 30% OFF"). Si \
un producto no tiene "price", decí que el precio se ve en el link. Nunca \
estimes ni calcules precios, cuotas ni descuentos que no figuren. Los precios \
pueden cambiar: el que vale es el que figura en la web al comprar.
- Si el producto tiene "colors", son los colores disponibles: mencionalos \
cuando sirvan (por ejemplo si preguntan por un color).
- Si preguntan medidas o peso, usá los campos "medidas" y "peso" de la \
búsqueda. Si un producto no los tiene, decí que no figuran y ofrecé el \
WhatsApp; nunca los estimes.
- No sabés el stock: si preguntan, decí que lo que está publicado se puede \
comprar en la web, sujeto a verificación de stock.
- Si la búsqueda no da resultados, probá con otros términos (sinónimos, \
categoría más general) antes de decirle al cliente que no lo encontraste.
- Si preguntan por medios de pago, cuotas, envíos, cambios, sucursales, cómo \
registrarse u otros temas de la tienda, respondé usando únicamente la sección \
"Preguntas frecuentes" de abajo. Si la respuesta no está ahí, no la inventes ni \
la deduzcas: indicá que pueden escribir por WhatsApp al {WHATSAPP} o \
consultar directamente en la web.
- No des información médica, legal ni de otros temas ajenos a la tienda."""

# Preguntas frecuentes que no salen del catálogo (pagos, mínimos, envíos...).
# Se editan en faq/faq.md y se recargan solas cuando cambia el archivo.
FAQ_PATH = os.path.join(app.root_path, "faq", "faq.md")
_faq_cache = (None, "")


def _parse_faq(text: str) -> str:
    """Devuelve solo las preguntas con respuesta (sin comentarios ni las que dicen COMPLETAR)."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    entries = []
    for block in re.split(r"^##\s+", text, flags=re.MULTILINE)[1:]:
        question, _, answer = block.partition("\n")
        answer = answer.strip()
        if question.strip() and answer and "COMPLETAR" not in answer:
            entries.append(f"P: {question.strip()}\nR: {answer}")
    return "\n\n".join(entries)


def get_faq() -> str:
    global _faq_cache
    try:
        mtime = os.path.getmtime(FAQ_PATH)
    except OSError:
        return ""
    if mtime != _faq_cache[0]:
        try:
            with open(FAQ_PATH, encoding="utf-8") as f:
                _faq_cache = (mtime, _parse_faq(f.read()))
        except OSError:
            app.logger.exception("No se pudo leer %s", FAQ_PATH)
    return _faq_cache[1]


def build_system_prompt() -> str:
    faq = get_faq() or "(Todavía no hay preguntas frecuentes cargadas.)"
    prompt = f"{SYSTEM_PROMPT}\n\nPreguntas frecuentes:\n\n{faq}"
    # Correcciones que el equipo cargó en /admin
    corrections = corrections_prompt()
    if corrections:
        prompt += (
            "\n\nCorrecciones del equipo de LSyD Store a respuestas anteriores. Si te "
            "preguntan algo parecido, respondé siguiendo la corrección (y si recomendás "
            f"productos, buscalos igual con search_products):\n\n{corrections}"
        )
    return prompt


# Registro de consultas: una línea JSON por consulta con la pregunta, la
# respuesta, las búsquedas que hizo el bot, el tiempo y los tokens. Sale en
# los logs (docker compose logs) y se guarda en CHAT_LOG_FILE (vacío = no
# guardar en archivo). Ojo: incluye lo que escriben los clientes.
chat_log = logging.getLogger("chat")
CHAT_LOG_FILE = os.environ.get("CHAT_LOG_FILE", os.path.join(app.root_path, "logs", "chats.jsonl"))
if CHAT_LOG_FILE:
    os.makedirs(os.path.dirname(CHAT_LOG_FILE), exist_ok=True)
    _file_handler = logging.FileHandler(CHAT_LOG_FILE, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(message)s"))
    chat_log.addHandler(_file_handler)

init_admin(app, limiter, CHAT_LOG_FILE)


def log_chat(**fields) -> None:
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ip": request.remote_addr,
        "model": MODEL,
        **fields,
    }
    chat_log.info(json.dumps(entry, ensure_ascii=False))


@beta_tool
def search_products_tool(query: str) -> str:
    """Busca productos en el catálogo de LSyD Store por palabras clave.

    Busca en el nombre, la categoría, el código y los colores del producto. Todas las
    palabras tienen que coincidir, así que conviene usar pocas y concretas.

    Args:
        query: Palabras clave (por ejemplo "mochila notebook", "valija carry on" o un código de producto).
    """
    results = search_products(query=query or "")
    # Para el log de la consulta: qué buscó el bot y cuántos resultados obtuvo
    g.searches.append({"query": query, "results": len(results)})
    if not results:
        return "No se encontraron productos que coincidan con esa búsqueda."
    return json.dumps(
        # price/list_price/discount/colors solo si los tiene; details: medidas y peso
        [
            {
                **{k: p[k] for k in ("name", "code", "category", "url", "price", "list_price", "discount", "colors") if k in p},
                **p.get("details", {}),
            }
            for p in results
        ],
        ensure_ascii=False,
    )


_URL_PATTERN = re.compile(r"https?://[^\s<\"']+")
MAX_REPLY_PRODUCTS = 10


def products_in_reply(reply: str) -> list[dict]:
    """Productos del catálogo cuyo link aparece en la respuesta, para que el widget muestre la foto."""
    products = []
    for url in dict.fromkeys(u.rstrip(".,;:!?)") for u in _URL_PATTERN.findall(reply)):
        product = get_product_by_url(url)
        if product and product.get("image"):
            products.append({k: product.get(k) for k in ("name", "code", "url", "image")})
            if len(products) == MAX_REPLY_PRODUCTS:
                break
    return products


# Id al azar que genera el widget por conversación: el panel /admin lo usa
# para mostrar juntas las consultas de un mismo cliente.
_CONVERSATION_PATTERN = re.compile(r"[A-Za-z0-9-]{8,64}")


def build_messages(history, user_message: str) -> list[dict]:
    """Arma la conversación para la API a partir del historial que manda el widget."""
    messages = []
    if isinstance(history, list):
        for turn in history[-MAX_HISTORY_TURNS:]:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role")
            content = turn.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content[:MAX_TURN_CHARS]})

    # Se quedan los turnos más recientes hasta MAX_HISTORY_CHARS en total
    total = 0
    for i in range(len(messages) - 1, -1, -1):
        total += len(messages[i]["content"])
        if total > MAX_HISTORY_CHARS:
            messages = messages[i + 1:]
            break

    # La conversación tiene que empezar con un mensaje del usuario
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    messages.append({"role": "user", "content": user_message})
    return messages


@app.route("/api/chat", methods=["POST"])
# Las consultas sin sesión válida no cuentan para los límites (se rechazan
# enseguida sin llamar a la API): si contaran, alguien con curl podría agotar
# el tope global y dejar sin chat a los clientes reales.
@limiter.limit(
    CHAT_RATE_LIMIT,
    error_message="Hiciste muchas consultas seguidas. Esperá un minuto y probá de nuevo.",
    exempt_when=lambda: not has_valid_session(),
)
@limiter.limit(
    GLOBAL_RATE_LIMIT,
    key_func=lambda: "global",
    error_message=f"El asistente recibió muchas consultas hoy. Escribinos por WhatsApp al {WHATSAPP}.",
    exempt_when=lambda: not has_valid_session(),
)
def chat():
    if not has_valid_session():
        return jsonify({"error": "La sesión del chat venció. Probá de nuevo.", "code": "session_required"}), 401

    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip() if isinstance(data, dict) else ""

    if not user_message:
        return jsonify({"error": "Falta el mensaje."}), 400
    if len(user_message) > MAX_MESSAGE_CHARS:
        return jsonify({"error": "Mensaje demasiado largo."}), 400

    messages = build_messages(data.get("history"), user_message)
    conversation = data.get("conversation")
    if not (isinstance(conversation, str) and _CONVERSATION_PATTERN.fullmatch(conversation)):
        conversation = None
    g.searches = []
    started = time.monotonic()
    usage = {"input_tokens": 0, "output_tokens": 0}

    def respond(status: int, reply: str = None, error: str = None):
        log_chat(
            conversation=conversation,
            status=status,
            message=user_message,
            reply=reply,
            error=error,
            history_turns=len(messages) - 1,
            searches=g.searches,
            seconds=round(time.monotonic() - started, 1),
            **usage,
        )
        if error:
            return jsonify({"error": error}), status
        return jsonify({"reply": reply, "products": products_in_reply(reply)})

    try:
        runner = client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=8000,
            system=build_system_prompt(),
            tools=[search_products_tool],
            messages=messages,
            output_config={"effort": EFFORT},
            max_iterations=6,
            # Si el modelo rechaza la consulta por seguridad, la API la
            # reintenta automáticamente con un modelo alternativo.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        # Se recorre paso a paso (en vez de until_done) para sumar los tokens
        # de todas las llamadas que hace el bot en una misma consulta.
        final_message = None
        for message in runner:
            final_message = message
            usage["input_tokens"] += message.usage.input_tokens or 0
            usage["output_tokens"] += message.usage.output_tokens or 0
    except anthropic.RateLimitError:
        return respond(429, error="Estamos con mucha demanda ahora mismo, probá de nuevo en un momento.")
    except anthropic.APIStatusError as e:
        app.logger.error("Anthropic API error: %s", e)
        return respond(502, error="Hubo un problema respondiendo tu consulta. Probá de nuevo.")
    except anthropic.APIConnectionError as e:
        app.logger.error("Anthropic connection error: %s", e)
        return respond(502, error="No pudimos conectarnos al asistente. Probá de nuevo en un momento.")
    except Exception:
        app.logger.exception("Unexpected error")
        return respond(500, error="Ocurrió un error inesperado. Probá de nuevo.")

    if final_message is None:
        return respond(500, error="Ocurrió un error inesperado. Probá de nuevo.")
    if final_message.stop_reason == "refusal":
        reply = f"Perdón, no puedo ayudarte con eso. Para otras consultas escribinos por WhatsApp al {WHATSAPP}."
    else:
        reply = "\n\n".join(b.text for b in final_message.content if b.type == "text").strip()
        if not reply:
            reply = "Perdón, no pude generar una respuesta. Probá de nuevo."

    return respond(200, reply=reply)


@app.route("/api/session", methods=["POST"])
@limiter.limit("10 per minute;60 per hour")
def create_session():
    """Verifica el token de Turnstile con Cloudflare y devuelve una sesión para /api/chat."""
    if not TURNSTILE_SECRET_KEY:
        return jsonify({"session": "", "expires_in": SESSION_SECONDS})

    data = request.get_json(silent=True)
    token = data.get("turnstile_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token or len(token) > 2048:
        return jsonify({"error": "Falta la verificación de seguridad."}), 400

    try:
        result = requests.post(
            TURNSTILE_VERIFY_URL,
            data={"secret": TURNSTILE_SECRET_KEY, "response": token, "remoteip": request.remote_addr},
            timeout=10,
        ).json()
    except (requests.RequestException, ValueError):
        app.logger.exception("No se pudo verificar Turnstile")
        return jsonify({"error": "No pudimos verificar la conexión. Probá de nuevo en un momento."}), 502

    if not result.get("success"):
        app.logger.warning("Turnstile rechazado para %s: %s", request.remote_addr, result.get("error-codes"))
        return jsonify({"error": "No pudimos verificar tu navegador. Recargá la página y probá de nuevo."}), 403

    return jsonify({"session": make_session(), "expires_in": SESSION_SECONDS})


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": e.description}), 429


# Página de prueba del widget. En producción podés apagarla con ENABLE_TEST_PAGE=0.
if os.environ.get("ENABLE_TEST_PAGE", "1") == "1":
    @app.route("/test", methods=["GET"])
    @limiter.exempt
    def test_page():
        with open(os.path.join(app.root_path, "test.html"), encoding="utf-8") as f:
            html = f.read().replace("{{TURNSTILE_SITE_KEY}}", TURNSTILE_SITE_KEY)
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/health", methods=["GET"])
@limiter.exempt
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)), debug=debug)
