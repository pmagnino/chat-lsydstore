"""
Panel de revisión de respuestas del chatbot (/admin).

Lee el registro de consultas (CHAT_LOG_FILE) para que una persona vea qué
preguntan los clientes y qué les respondió el bot, marque cada respuesta como
bien o mal y escriba la respuesta correcta. Las revisiones se guardan en
REVIEWS_FILE, y las correcciones marcadas para "enseñar" se suman al prompt
del bot (ver corrections_prompt), así la próxima consulta parecida sale bien.

Se activa poniendo ADMIN_PASSWORD; sin contraseña /admin no existe.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from flask import Response, jsonify, request

try:
    import fcntl  # Linux (Docker): bloquea el archivo entre workers de gunicorn
except ImportError:
    fcntl = None

log = logging.getLogger(__name__)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

MAX_CORRECTION_CHARS = 4000
MAX_NOTE_CHARS = 1000
# Tope de texto de correcciones que se agrega al prompt (las más nuevas
# primero): evita que cada consulta se vuelva cara si se cargan cientos.
MAX_CORRECTIONS_PROMPT_CHARS = 20000

_ADMIN_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin", "index.html")

_reviews_path = ""
_write_lock = threading.Lock()
_reviews_cache = (None, {})


def entry_id(line: str) -> str:
    """Id estable de una línea del registro (no cambia aunque se reinicie el servidor)."""
    return hashlib.sha1(line.encode("utf-8")).hexdigest()[:16]


def load_reviews() -> dict:
    global _reviews_cache
    try:
        mtime = os.path.getmtime(_reviews_path)
    except OSError:
        return {}
    if mtime != _reviews_cache[0]:
        try:
            with open(_reviews_path, encoding="utf-8") as f:
                _reviews_cache = (mtime, json.load(f))
        except (OSError, ValueError):
            log.exception("No se pudo leer %s", _reviews_path)
    return _reviews_cache[1]


def _update_reviews(change) -> dict:
    """Lee, modifica con change(reviews) y guarda reviews de forma atómica."""
    with _write_lock, open(_reviews_path + ".lock", "w") as lock_file:
        if fcntl:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            with open(_reviews_path, encoding="utf-8") as f:
                reviews = json.load(f)
        except FileNotFoundError:
            reviews = {}
        change(reviews)
        tmp = _reviews_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reviews, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _reviews_path)
        return reviews


def corrections_prompt() -> str:
    """Correcciones del equipo para sumar al system prompt ("" si no hay)."""
    teach = [r for r in load_reviews().values() if r.get("teach") and r.get("correction") and r.get("question")]
    teach.sort(key=lambda r: r.get("reviewed_at", ""), reverse=True)
    entries, total = [], 0
    for r in teach:
        entry = f"Consulta: {r['question']}\nRespuesta correcta: {r['correction']}"
        total += len(entry)
        if total > MAX_CORRECTIONS_PROMPT_CHARS:
            break
        entries.append(entry)
    return "\n\n".join(entries)


def _read_chats(log_file: str, days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ""
    reviews = load_reviews()
    entries = []
    try:
        with open(log_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("time", "") < since:
                    continue
                entry.pop("ip", None)  # el panel no necesita la IP del cliente
                entry["id"] = entry_id(line)
                entry["review"] = reviews.get(entry["id"])
                entries.append(entry)
    except FileNotFoundError:
        pass
    entries.reverse()  # las más nuevas primero
    return entries


def _authorized() -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except ValueError:
        return False
    return hmac.compare_digest(user.encode(), ADMIN_USER.encode()) & hmac.compare_digest(
        password.encode(), ADMIN_PASSWORD.encode()
    )


def init_admin(app, limiter, chat_log_file: str) -> None:
    """Registra /admin en la app (solo si hay ADMIN_PASSWORD)."""
    global _reviews_path
    log_dir = os.path.dirname(chat_log_file) if chat_log_file else os.path.join(app.root_path, "logs")
    _reviews_path = os.environ.get("REVIEWS_FILE", os.path.join(log_dir, "reviews.json"))
    if not ADMIN_PASSWORD:
        return
    os.makedirs(os.path.dirname(_reviews_path), exist_ok=True)

    # Un solo límite para todo el panel: frena a quien pruebe contraseñas.
    admin_limit = limiter.shared_limit("60 per minute", scope="admin")

    @app.before_request
    def require_admin_auth():
        if request.path.startswith("/admin") and not _authorized():
            return Response(
                "Necesitás usuario y contraseña.", 401, {"WWW-Authenticate": 'Basic realm="LSyD admin"'}
            )

    @app.route("/admin", methods=["GET"])
    @admin_limit
    def admin_page():
        with open(_ADMIN_HTML, encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}

    @app.route("/admin/api/chats", methods=["GET"])
    @admin_limit
    def admin_chats():
        days = request.args.get("days", "30")
        days = int(days) if days.isdigit() else 30
        return jsonify({"entries": _read_chats(chat_log_file, days)})

    @app.route("/admin/api/reviews", methods=["GET"])
    @admin_limit
    def admin_reviews():
        return jsonify({"reviews": load_reviews()})

    @app.route("/admin/api/reviews/<review_id>", methods=["POST"])
    @admin_limit
    def admin_save_review(review_id):
        # get_json solo acepta Content-Type: application/json, que otro sitio
        # no puede mandar sin un preflight de CORS (que /admin no habilita):
        # así nadie puede guardar revisiones con la sesión del navegador del admin.
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or len(review_id) != 16:
            return jsonify({"error": "Pedido inválido."}), 400

        verdict = data.get("verdict")
        if verdict is None:
            _update_reviews(lambda reviews: reviews.pop(review_id, None))
            return jsonify({"review": None})
        if verdict not in ("ok", "wrong"):
            return jsonify({"error": "Veredicto inválido."}), 400

        review = {
            "verdict": verdict,
            "question": str(data.get("question") or "")[:2000],
            "reply": str(data.get("reply") or "")[:MAX_CORRECTION_CHARS],
            "correction": str(data.get("correction") or "").strip()[:MAX_CORRECTION_CHARS],
            "note": str(data.get("note") or "").strip()[:MAX_NOTE_CHARS],
            "teach": bool(data.get("teach")),
            "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _update_reviews(lambda reviews: reviews.__setitem__(review_id, review))
        return jsonify({"review": review})
