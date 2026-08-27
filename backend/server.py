import json
import io
import os
import re
import uuid
import hashlib
import logging
import requests
import threading
import traceback
import time
import glob
import wave
from contextlib import contextmanager
from queue import Empty, Queue
from datetime import timedelta, datetime, timezone
from flask import Flask, jsonify, send_from_directory, request, Response, session
from openai import OpenAI, APIConnectionError, RateLimitError, InternalServerError
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from redis import Redis
from redis.exceptions import RedisError

from dotenv import load_dotenv
load_dotenv()

from lesson_paths import (
    new_lesson_id, lesson_dir, lesson_path, write_meta, read_meta, LESSONS_DIR, Status, STATUS_INFO
)
from db import (
    init_db, record_quiz_attempt, get_quiz_history, update_lesson_progress, 
    get_lesson_progress, create_user, get_user_by_email, claim_session, 
    create_lesson_owner, get_lessons_for_owner, owns_lesson, resolve_lesson_access,
    get_db, update_user_password, owns_lesson, get_db, delete_lesson,
    delete_user, get_lesson_ids_for_user, get_quiz_question, insert_quiz_questions, 
    delete_quiz_questions_for_lesson, get_attempt_count, get_max_batch, deactivate_batch, 
    get_active_quiz_questions, save_item, unsave_item, get_saved_items, update_user_name,
    create_password_reset, get_valid_reset, consume_reset_token, 
    create_class, archive_class, get_classes_for_teacher, get_classes_for_student,
    get_user_role, generate_join_code, get_valid_join_code_for_class, 
    resolve_join_code, join_class, leave_class, get_enrollments_for_class,
    create_assignment, get_assignment, get_assignments_for_class, 
    publish_assignment, archive_assignment, delete_assignment, get_assignment_for_lesson,
    resolve_lesson_access, is_enrolled, get_published_assignments_for_student,
    get_assigned_lessons_for_student, get_assigned_lessons_for_student_by_teacher,
    update_assignment, update_class_name
)

from pipeline.text_extraction import run_text_extraction
from pipeline.llm_curriculum_graph import run_curriculum_extraction
from pipeline.quiz_generation import generate_quiz_batch
from pipeline.slideshow_generation import run_slideshow_generation
from pipeline.animation_generation import run_animation_generation
from pipeline_queue import enqueue_job, start_workers

# ── Paths ─────────────────────────────────────────────────────────────────────
from lesson_paths import FRONTEND_DIR, UPLOAD_DIR, LESSONS_DIR
ALLOWED_EXTS  = {".pdf", ".docx", ".pptx"} 

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB upload cap
app.secret_key = os.environ["FLASK_SECRET_KEY"]
# uncomment before production; rn testing locally (no TLS)
#app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax")
app.config.update(
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
    SESSION_COOKIE_SAMESITE="Lax"
)
app.permanent_session_lifetime = timedelta(days=365)

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Defined up here, before any routes, since route decorators reference
# `limiter` at import time.
def _rate_limit_key():
    """Prefer logged-in user, then anonymous session_id, then IP as a last
    resort (e.g. requests that somehow arrive before ensure_session_id runs,
    such as the very first request of a new browser session).
    Keying on session rather than raw IP means users behind a shared/corporate
    IP don't share a limit, and switching networks doesn't reset someone's."""
    user_id = session.get("user_id")
    if user_id:
        return f"user:{user_id}"
    session_id = session.get("session_id")
    if session_id:
        return f"session:{session_id}"
    return get_remote_address()

limiter = Limiter(
    app=app,
    key_func=_rate_limit_key,
    storage_uri=os.environ.get("REDIS_URL", "memory://"),  # Redis in prod (shared across gunicorn workers); memory:// fallback for local dev without Redis running
    default_limits=["200 per hour"],  # generous fallback for routes without an explicit limit
)

class _SkipStatusPolling(logging.Filter):
    def filter(self, record):
        return "/status" not in record.getMessage()

logging.getLogger("werkzeug").addFilter(_SkipStatusPolling())

init_db()

@app.errorhandler(413)
def file_too_large(e):
    return jsonify({"error": "File is too large. Please upload a file under 50MB."}), 413

@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": "Too many requests. Please slow down and try again shortly."}), 429

def current_identity():
    """Returns (user_id, session_id). user_id is None for anonymous
    visitors — routes decide per-endpoint whether that's allowed."""
    return session.get("user_id"), session["session_id"]

@app.route("/api/auth/signup", methods=["POST"])
@limiter.limit("5 per hour", key_func=get_remote_address)
def signup():
    body = request.get_json(force=True)
    email, password = body.get("email", "").strip().lower(), body.get("password", "")
    role = body.get("role", "student")
    first_name = (body.get("first_name") or "").strip()
    last_name = (body.get("last_name") or "").strip()
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    if "@" not in email or len(password) < 6:
        return jsonify({"error": "Invalid email or password too short"}), 400
    if role not in ("student", "teacher"):
        return jsonify({"error": "Invalid role"}), 400
    if not first_name or not last_name:
        return jsonify({"error": "First and last name required"}), 400
    if get_user_by_email(email):
        return jsonify({"error": "Account already exists"}), 409

    user_id = create_user(email, generate_password_hash(password), role, first_name, last_name)
    claim_session(session["session_id"], user_id)
    session["user_id"] = user_id
    return jsonify({"ok": True, "email": email, "role": role}), 201


@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("50 per hour", key_func=get_remote_address)
def login():
    body = request.get_json(force=True)
    user = get_user_by_email(body.get("email", "").strip().lower())
    if not user or not check_password_hash(user["password_hash"], body.get("password", "")):
        return jsonify({"error": "Invalid credentials"}), 401
    session["user_id"] = user["id"]
    claim_session(session["session_id"], user["id"])  # picks up any anonymous activity from this session

    return jsonify({"ok": True, "email": user["email"], "role": user["role"]})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return jsonify({"ok": True})

# logged in user with known password
@app.route("/api/auth/change-password", methods=["POST"])
def change_password():
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    body = request.get_json(force=True)
    current_password = body.get("current_password", "")
    new_password = body.get("new_password", "")
    if len(new_password) < 6:
        return jsonify({"error": "New password must be at least 6 characters"}), 400

    conn = get_db()
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not check_password_hash(row["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 400

    update_user_password(user_id, generate_password_hash(new_password))
    return jsonify({"ok": True})

@app.route("/api/auth/forgot-password", methods=["POST"])
@limiter.limit("100 per hour", key_func=get_remote_address)
def forgot_password():
    body = request.get_json(force=True)
    email = (body.get("email") or "").strip().lower()
    user = get_user_by_email(email) if email else None
    if user:
        token = create_password_reset(user["id"])
        reset_link = f"http://localhost:5000/?token={token}"
        print(f"[DEV] Password reset link for {email}: {reset_link}", flush=True)
    # Always return the same response, whether or not the email exists —
    # this avoids letting the endpoint be used to check which emails are registered.
    return jsonify({"ok": True, "message": "If that email is registered, a reset link has been sent."})

# user locked out with forgotten password
@app.route("/api/auth/reset-password", methods=["POST"])
def reset_password():
    body = request.get_json(force=True)
    token = body.get("token", "")
    new_password = body.get("password", "")
    if not token or len(new_password) < 6:
        return jsonify({"error": "Invalid token or password too short"}), 400

    reset = get_valid_reset(token)
    if not reset:
        return jsonify({"error": "This reset link is invalid or has expired"}), 400

    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(new_password), reset["user_id"]))
    conn.commit()
    conn.close()
    consume_reset_token(token)
    return jsonify({"ok": True})

@app.route("/api/auth/me")
def whoami():
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"logged_in": False})
    conn = get_db()
    row = conn.execute("SELECT email, role, first_name, last_name, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return jsonify({
        "logged_in": True, "email": row["email"], "role": row["role"],
        "first_name": row["first_name"], "last_name": row["last_name"],
        "member_since": row["created_at"]
    })

@app.route("/api/auth/update-name", methods=["POST"])
def update_name():
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    body = request.get_json(force=True)
    first_name = (body.get("first_name") or "").strip() or None
    last_name = (body.get("last_name") or "").strip() or None
    if not first_name or not last_name:
        return jsonify({"error": "First and last name required"}), 400
    update_user_name(user_id, first_name, last_name)
    return jsonify({"ok": True, "first_name": first_name, "last_name": last_name})

@app.before_request
def ensure_session_id():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
        #app.logger.info(f"New session created: {session['session_id']}")
    session.permanent = True
    #app.logger.info(f"Request from session: {session['session_id']}")

# ── Frontend ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")

# ── LLM client ────────────────────────────────────────────────────────────────
api_key = os.environ["OPENAI_API_KEY"]
llm_client = OpenAI(
    api_key=api_key,
    base_url=os.environ["LLM_BASE_URL"],
    default_headers={"x-goog-api-key": api_key}
)

# ── In-memory cache ───────────────────────────────────────────────────────────
from collections import OrderedDict

_info_cache = OrderedDict()
_INFO_CACHE_MAX = 500

def _cache_key(*parts):
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()

def _cache_get(key):
    if key in _info_cache:
        _info_cache.move_to_end(key)  # mark as recently used
        return _info_cache[key]
    return None

def _cache_set(key, value):
    _info_cache[key] = value
    _info_cache.move_to_end(key)
    if len(_info_cache) > _INFO_CACHE_MAX:
        _info_cache.popitem(last=False)  # evict least-recently-used

# ── Tool definitions ──────────────────────────────────────────────────────────
DEFINITION_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_definition_card",
        "description": "Generate a structured info card for a defined term from a course slide.",
        "parameters": {
            "type": "object",
            "properties": {
                "term": {
                    "type": "string",
                    "description": "The term exactly as it should appear in the card title."
                },
                "part_of_speech": {
                    "type": "string",
                    "description": "Grammatical role: noun, verb, adjective, etc. For mathematical terms use 'noun'."
                },
                "definition": {
                    "type": "string",
                    "description": "A clear, slightly expanded definition of the term. 1-3 sentences. Do not repeat the slide text verbatim."
                },
                "examples": {
                    "type": "array",
                    "description": "Exactly 3 full sentences using the term in context. Relevant to the course. Inline math must use $...$ delimiters.",
                    "items": {"type": "string"},
                    "minItems": 3,
                    "maxItems": 3
                }
            },
            "required": ["term", "part_of_speech", "definition", "examples"]
        }
    }
}

EQUATION_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_equation_card",
        "description": "Generate a structured info card for a LaTeX equation from a course slide.",
        "parameters": {
            "type": "object",
            "properties": {
                "latex": {
                    "type": "string",
                    "description": "The equation in LaTeX. Use single backslashes (\\frac, not \\\\frac)."
                },
                "description": {
                    "type": "string",
                    "description": "1-2 plain English sentences: what this equation expresses and when you would use it."
                },
                "variables": {
                    "type": "array",
                    "description": "Each variable or symbol in the equation with its meaning in this course context.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string", "description": "LaTeX for the symbol, single backslashes."},
                            "meaning": {"type": "string", "description": "Plain English meaning of the symbol."}
                        },
                        "required": ["symbol", "meaning"]
                    }
                },
                "constraints": {
                    "type": "array",
                    "description": "Any domain restrictions, assumptions, or conditions. Empty array if none.",
                    "items": {"type": "string"}
                },
                "examples": {
                    "type": "array",
                    "description": "Exactly 3 worked examples. Each has a problem statement and step-by-step solution. Math in steps uses $...$ for inline.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "problem": {"type": "string"},
                            "steps":   {"type": "array", "items": {"type": "string"}}
                        },
                        "required": ["problem", "steps"]
                    },
                    "minItems": 3,
                    "maxItems": 3
                }
            },
            "required": ["latex", "description", "variables", "constraints", "examples"]
        }
    }
}

# ── LLM call helper ───────────────────────────────────────────────────────────
def _call_tool(system: str, user: str, tool: dict, max_retries: int = 2) -> dict:
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            response = llm_client.chat.completions.create(
                model="gpt-5.4-mini",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user}
                ],
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": tool["function"]["name"]}},
                temperature=0.2,
                max_tokens=2000
            )
            args_str = response.choices[0].message.tool_calls[0].function.arguments
            return json.loads(args_str)
        except (InternalServerError, APIConnectionError, RateLimitError) as e:
            print(f"Tool call failed: {repr(e)}")
            last_err = e
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))  # 1.5s, then 3s
                continue
    raise last_err

# ── Data routes ───────────────────────────────────────────────────────────────
def require_teacher_owns_class(class_id, teacher_id):
    """Returns the class row if it exists and belongs to this teacher, else None."""
    conn = get_db()
    row = conn.execute("SELECT * FROM classes WHERE id = ? AND teacher_id = ?", (class_id, teacher_id)).fetchone()
    conn.close()
    return dict(row) if row else None


@app.route("/api/classes", methods=["GET"])
def list_classes():
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if get_user_role(user_id) == "teacher":
        return jsonify({"classes": get_classes_for_teacher(user_id)})
    return jsonify({"classes": get_classes_for_student(user_id)})


@app.route("/api/classes", methods=["POST"])
def create_class_route():
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Class name required"}), 400
    if len(get_classes_for_teacher(user_id)) >= 10:
        return jsonify({"error": "Class limit reached (10 max)"}), 400
    class_id = create_class(user_id, name)
    return jsonify({"ok": True, "class_id": class_id}), 201


@app.route("/api/classes/<int:class_id>/archive", methods=["POST"])
def archive_class_route(class_id):
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    archive_class(class_id, user_id)
    return jsonify({"ok": True})


@app.route("/api/classes/<int:class_id>", methods=["PATCH"])
def update_class_route(class_id):
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Class name required"}), 400
    update_class_name(class_id, user_id, name)
    return jsonify({"ok": True})


@app.route("/api/classes/<int:class_id>/invite-code", methods=["POST"])
def generate_invite_code_route(class_id):
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    result = generate_join_code(class_id)
    return jsonify({"ok": True, "code": result["code"], "expires_at": result["expires_at"]})


@app.route("/api/classes/<int:class_id>/invite-code", methods=["GET"])
def get_current_invite_code(class_id):
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    row = get_valid_join_code_for_class(class_id)
    return jsonify({"code": row["code"] if row else None, "expires_at": row["expires_at"] if row else None})


@app.route("/api/classes/<int:class_id>/roster", methods=["GET"])
def class_roster(class_id):
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    return jsonify({"students": get_enrollments_for_class(class_id)})


@app.route("/api/classes/<int:class_id>/students/<int:student_id>/remove", methods=["POST"])
def remove_student_route(class_id, student_id):
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    leave_class(class_id, student_id)
    return jsonify({"ok": True})


@app.route("/api/classes/join", methods=["POST"])
def join_class_route():
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    body = request.get_json(force=True)
    code = (body.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "Invite code required"}), 400
    resolved = resolve_join_code(code)
    if not resolved:
        return jsonify({"error": "Invalid or expired invite code"}), 400
    joined = join_class(resolved["class_id"], user_id)
    if not joined:
        return jsonify({"ok": True, "already_joined": True})
    return jsonify({"ok": True, "already_joined": False})


@app.route("/api/classes/<int:class_id>/leave", methods=["POST"])
def leave_class_route(class_id):
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    leave_class(class_id, user_id)
    return jsonify({"ok": True})


def _create_assignment_lesson(class_id, teacher_id, f):
    """Shared with create_lesson(): validates/saves the upload and enqueues
    the pipeline job. Only the ownership row differs (assignment vs personal)."""
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return None, jsonify({"error": f"Unsupported file type: {ext}"}), 400

    lesson_id = new_lesson_id()
    _, session_id = current_identity()
    create_lesson_owner(lesson_id, session_id=session_id, user_id=teacher_id)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    saved_path = os.path.join(UPLOAD_DIR, secure_filename(f"{lesson_id}{ext}"))
    f.save(saved_path)
    write_meta(lesson_id, status=Status.QUEUED, source_filename=f.filename)
    enqueue_job(lesson_id, saved_path, f.filename)
    return lesson_id, None, None


@app.route("/api/classes/<int:class_id>/assignments", methods=["POST"])
@limiter.limit("10 per hour")
def create_assignment_route(class_id):
    user_id, _ = current_identity()
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    lesson_id, err_response, code = _create_assignment_lesson(class_id, user_id, request.files["file"])
    if err_response:
        return err_response, code

    assignment_id = create_assignment(class_id, lesson_id, user_id)
    return jsonify({"assignment_id": assignment_id, "lesson_id": lesson_id}), 202


@app.route("/api/classes/<int:class_id>/assignments", methods=["GET"])
def list_assignments_route(class_id):
    user_id, _ = current_identity()
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404

    assignments = get_assignments_for_class(class_id)
    for a in assignments:
        try:
            meta = read_meta(a["lesson_id"])
            meta.update(STATUS_INFO.get(meta.get("status"), {}))
            a["lesson"] = meta
        except (FileNotFoundError, ValueError):
            a["lesson"] = None
    return jsonify(assignments)


@app.route("/api/classes/<int:class_id>/student-assignments", methods=["GET"])
def list_student_assignments_route(class_id):
    user_id, _ = current_identity()
    if not user_id or not is_enrolled(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404

    assignments = get_published_assignments_for_student(class_id)
    for a in assignments:
        try:
            meta = read_meta(a["lesson_id"])
            meta.update(STATUS_INFO.get(meta.get("status"), {}))
            a["lesson"] = meta
        except (FileNotFoundError, ValueError):
            a["lesson"] = None
    return jsonify(assignments)


@app.route("/api/lessons/<lesson_id>/assignment")
def get_lesson_assignment(lesson_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    assignment = get_assignment_for_lesson(lesson_id)
    if not assignment:
        return jsonify({}), 200
    return jsonify(assignment)


@app.route("/api/classes/<int:class_id>/assignments/<int:assignment_id>", methods=["DELETE"])
def delete_assignment_route(class_id, assignment_id):
    user_id, _ = current_identity()
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    assignment = get_assignment(assignment_id)
    if not assignment or assignment["class_id"] != class_id:
        return jsonify({"error": "Assignment not found"}), 404
    if assignment["status"] != "draft":
        return jsonify({"error": "Only draft assignments can be deleted"}), 400

    delete_assignment(assignment_id)
    delete_lesson(assignment["lesson_id"])
    return jsonify({"ok": True})


@app.route("/api/classes/<int:class_id>/assignments/<int:assignment_id>/regenerate", methods=["POST"])
@limiter.limit("10 per hour")
def regenerate_assignment_route(class_id, assignment_id):
    user_id, _ = current_identity()
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    assignment = get_assignment(assignment_id)
    if not assignment or assignment["class_id"] != class_id or assignment["status"] != "draft":
        return jsonify({"error": "Assignment not found"}), 404

    lesson_id = assignment["lesson_id"]
    try:
        meta = read_meta(lesson_id)
    except (FileNotFoundError, ValueError):
        return jsonify({"error": "Lesson not found"}), 404
    if meta.get("status") not in (Status.READY, Status.FAILED):
        return jsonify({"error": "Lesson is still processing; wait for it to finish"}), 400

    matches = glob.glob(os.path.join(UPLOAD_DIR, f"{lesson_id}.*"))
    if not matches:
        return jsonify({"error": "Original uploaded file not found; cannot regenerate"}), 404
    file_path = matches[0]

    write_meta(lesson_id, status=Status.QUEUED, error=None, failed_stage=None)
    enqueue_job(lesson_id, file_path, meta.get("source_filename", ""), priority=0)
    return jsonify({"status": "regenerating"}), 202


@app.route("/api/classes/<int:class_id>/assignments/<int:assignment_id>/publish", methods=["POST"])
def publish_assignment_route(class_id, assignment_id):
    user_id, _ = current_identity()
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    assignment = get_assignment(assignment_id)
    if not assignment or assignment["class_id"] != class_id or assignment["status"] != "draft":
        return jsonify({"error": "Assignment not found"}), 404

    body = request.get_json(force=True) or {}
    title = (body.get("title") or "").strip() or None
    publish_assignment(assignment_id, due_at=body.get("due_at"), max_attempts=body.get("max_attempts"), title=title)
    return jsonify({"ok": True})


@app.route("/api/classes/<int:class_id>/assignments/<int:assignment_id>", methods=["PATCH"])
def update_assignment_route(class_id, assignment_id):
    user_id, _ = current_identity()
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    assignment = get_assignment(assignment_id)
    if not assignment or assignment["class_id"] != class_id or assignment["status"] != "published":
        return jsonify({"error": "Assignment not found"}), 404

    body = request.get_json(force=True) or {}
    title = (body.get("title") or "").strip() or None
    update_assignment(assignment_id, due_at=body.get("due_at"), max_attempts=body.get("max_attempts"), title=title)
    return jsonify({"ok": True})


@app.route("/api/classes/<int:class_id>/assignments/<int:assignment_id>/archive", methods=["POST"])
def archive_assignment_route(class_id, assignment_id):
    user_id, _ = current_identity()
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    assignment = get_assignment(assignment_id)
    if not assignment or assignment["class_id"] != class_id:
        return jsonify({"error": "Assignment not found"}), 404

    archive_assignment(assignment_id)
    return jsonify({"ok": True})


@app.route("/api/lessons/<lesson_id>/slideshow")
def get_slideshow(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    try:
        with open(lesson_path(lesson_id, "slideshow.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return jsonify({"error": "Lesson not found or not finished generating."}), 404
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid JSON: {str(e)}"}), 500

    assignment = get_assignment_for_lesson(lesson_id)
    if assignment and assignment.get("title"):
        data["slideshow"]["course"] = assignment["title"]
    return jsonify(data)

@app.route("/api/lessons/<lesson_id>/curriculum")
def get_curriculum(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    try:
        with open(lesson_path(lesson_id, "extracted_concepts.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return jsonify({"error": "Lesson not found or not finished generating."}), 404
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid JSON: {str(e)}"}), 500

    assignment = get_assignment_for_lesson(lesson_id)
    if assignment and assignment.get("title"):
        data["course"] = assignment["title"]
    return jsonify(data)
    
@app.route("/api/lessons/<lesson_id>/retry", methods=["POST"])
def retry_lesson(lesson_id):
    """Resumes a failed lesson from its last failed stage rather than
    restarting the whole pipeline from scratch."""
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    try:
        meta = read_meta(lesson_id)
    except (FileNotFoundError, ValueError):
        return jsonify({"error": "Lesson not found"}), 404
    if meta.get("status") != Status.FAILED:
        return jsonify({"error": "Lesson is not in a failed state"}), 400

    failed_stage = meta.get("failed_stage", Status.EXTRACTING)

    # Glob rather than reconstruct the filename/extension — safer than trusting
    # secure_filename() + source_filename's extension to exactly match what
    # was actually saved to disk at upload time.
    matches = glob.glob(os.path.join(UPLOAD_DIR, f"{lesson_id}.*"))
    if not matches:
        return jsonify({"error": "Original uploaded file not found; cannot retry"}), 404
    file_path = matches[0]

    write_meta(lesson_id, status=failed_stage, error=None)
    enqueue_job(lesson_id, file_path, meta.get("source_filename", ""), resume_from=failed_stage, priority=0)
    return jsonify({"status": "retrying"}), 202

def _regenerate_quiz_batch(lesson_id, concept_id):
    try:
        with open(lesson_path(lesson_id, "extracted_concepts.json"), "r", encoding="utf-8") as f:
            curriculum = json.load(f)
        concept = next(
            c for c in curriculum["curriculum_graph"]["concepts"] if c["concept_id"] == concept_id
        )
        questions = generate_quiz_batch(
            concept["name"], concept["description"], concept["study"]["key_terms"],
            lesson_id, concept_id
        )
        next_batch = get_max_batch(lesson_id, concept_id) + 1
        insert_quiz_questions(lesson_id, concept_id, questions, generation_batch=next_batch)
        deactivate_batch(lesson_id, concept_id, next_batch - 1)
    except Exception as e:
        traceback.print_exc()
        # regen failure just means the student keeps seeing the batch they
        # already took — not ideal, but no route is left in a broken state


@app.route("/api/lessons/<lesson_id>/quiz-attempt-batch", methods=["POST"])
def submit_quiz_batch(lesson_id):
    user_id, session_id = current_identity()
    access = resolve_lesson_access(lesson_id, user_id, session_id)
    if not access:
        return jsonify({"error": "Lesson not found"}), 404
    is_owner_testing = access["role"] == "owner"

    max_attempts = None
    if access["role"] == "student":
        assignment = get_assignment(access["assignment_id"])
        max_attempts = assignment["max_attempts"] if assignment else None

    body = request.get_json(force=True)
    is_review = bool(body.get("review"))

    batch_timestamp = datetime.now(timezone.utc).isoformat()
    touched_concept_ids = set()
    next_attempt_number = {}   # concept_id -> attempt number for this batch
    exhausted_concept_ids = set()   # concepts skipped this batch because limit was already hit
    for a in body["attempts"]:
        question = get_quiz_question(a["question_id"])
        if question is None or question["lesson_id"] != lesson_id:
            continue
        concept_id = question["concept_id"]
        correct_answer = question["answer"]
        answer_given = a.get("answer_given")
        is_correct = str(answer_given).strip().lower() == str(correct_answer).strip().lower()
        touched_concept_ids.add(concept_id)

        if is_review or is_owner_testing:
            continue

        if concept_id in exhausted_concept_ids:
            continue

        if concept_id not in next_attempt_number:
            used = get_attempt_count(user_id, session_id, lesson_id, concept_id)
            if max_attempts is not None and used >= max_attempts:
                exhausted_concept_ids.add(concept_id)
                continue
            next_attempt_number[concept_id] = used + 1

        record_quiz_attempt(
            session_id=session_id, user_id=user_id, lesson_id=lesson_id, concept_id=concept_id,
            question_id=a["question_id"], question_text=question["question_text"],
            answer_given=answer_given, correct_answer=correct_answer,
            explanation=question["explanation"], is_correct=is_correct,
            submitted_at=batch_timestamp, attempt_number=next_attempt_number[concept_id],
        )

    return jsonify({
        "ok": True,
        "touched_concept_ids": list(touched_concept_ids),
        "exhausted_concept_ids": list(exhausted_concept_ids),
    })


@app.route("/api/lessons/<lesson_id>/concepts/<concept_id>/quiz")
def get_concept_quiz(lesson_id, concept_id):
    user_id, session_id = current_identity()
    access = resolve_lesson_access(lesson_id, user_id, session_id)
    if not access:
        return jsonify({"error": "Lesson not found"}), 404
    questions = get_active_quiz_questions(lesson_id, concept_id)
    for q in questions:
        q["choices"] = json.loads(q["choices"]) if q["choices"] else None

    max_attempts, attempts_used = None, 0
    if access["role"] == "student":
        assignment = get_assignment(access["assignment_id"])
        max_attempts = assignment["max_attempts"] if assignment else None
        attempts_used = get_attempt_count(user_id, session_id, lesson_id, concept_id)

    return jsonify({"questions": questions, "max_attempts": max_attempts, "attempts_used": attempts_used})


@app.route("/api/lessons/<lesson_id>/quiz-history")
def quiz_history(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    concept_id = request.args.get("concept_id")
    history = get_quiz_history(user_id=user_id, session_id=session_id, lesson_id=lesson_id, concept_id=concept_id)
    return jsonify(history)

@app.route("/api/lessons/<lesson_id>/saved-items", methods=["GET"])
def list_saved_items(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    return jsonify(get_saved_items(session_id, lesson_id, user_id))


@app.route("/api/lessons/<lesson_id>/saved-items/<item_id>", methods=["POST"])
def add_saved_item(lesson_id, item_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    body = request.get_json(force=True)
    save_item(session_id, lesson_id, item_id, body["item_type"], user_id, body.get("content"))
    return jsonify({"ok": True})


@app.route("/api/lessons/<lesson_id>/saved-items/<item_id>", methods=["DELETE"])
def remove_saved_item(lesson_id, item_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    unsave_item(session_id, lesson_id, item_id)
    return jsonify({"ok": True})
    
# ── Info card routes ──────────────────────────────────────────────────────────
@app.route("/api/lessons/<lesson_id>/info/definition", methods=["POST"])
@limiter.limit("60 per hour")
def info_definition(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    body = request.get_json(force=True)
    term                = body.get("term", "").strip()
    definition_on_slide = body.get("definition_on_slide", "").strip()
    context             = body.get("context", "").strip()
    slide_text          = body.get("slide_text", "").strip()

    if not term:
        return jsonify({"error": "term is required"}), 400

    key = _cache_key("def", lesson_id, term, context)
    cached = _cache_get(key)
    if cached is not None:
        return jsonify(cached)

    system = (
    "You are an expert educator creating a concise info card for a student who clicked "
    "on a term while viewing a course slideshow. "
    "Generate content that is curriculum-aware and directly relevant to the course context provided. "
    "For mathematical terms, treat part_of_speech as 'noun'. "
    "Examples must be full sentences that show the term used naturally. "
    "In each example sentence, wrap the term (or its inflected form) in [[double brackets]] "
    "so it can be highlighted. Example: 'The [[eigenvalue]] scales the vector.' "
    "If the term has mathematical meaning, inline math in examples must use $...$ delimiters."
    )  
    user = (
        f"Course context: {context}\n"
        f"Slide text: {slide_text}\n"
        f"Term: {term}\n"
        f"Definition as shown on slide: {definition_on_slide}\n\n"
        "Generate the definition info card."
    )

    try:
        result = _call_tool(system, user, DEFINITION_TOOL)
        _cache_set(key, result)
        return jsonify(result)
    except (InternalServerError, APIConnectionError, RateLimitError) as e:
        print(f"Definition endpoint failed: {repr(e)}")
        return jsonify({"error": "The AI service is temporarily unavailable. Please try again in a moment."}), 503
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/lessons/<lesson_id>/info/equation", methods=["POST"])
@limiter.limit("60 per hour")
def info_equation(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    body = request.get_json(force=True)
    latex      = body.get("latex", "").strip()
    context    = body.get("context", "").strip()
    slide_text = body.get("slide_text", "").strip()

    if not latex:
        return jsonify({"error": "latex is required"}), 400

    key = _cache_key("eq", lesson_id, latex, context)
    cached = _cache_get(key)
    if cached is not None:
        return jsonify(cached)

    system = (
        "You are an expert educator creating a concise info card for a student who clicked "
        "on an equation while viewing a course slideshow. "
        "The equation is provided in LaTeX. Explain it in the context of the course. "
        "Use single backslashes in all LaTeX output (\\frac, not \\\\frac). "
        "Inline math in steps and descriptions must use $...$ delimiters. "
        "Worked examples should be realistic for the course level implied by the context."
    )
    user = (
        f"Course context: {context}\n"
        f"Slide text: {slide_text}\n"
        f"Equation (LaTeX): {latex}\n\n"
        "Generate the equation info card."
    )

    try:
        result = _call_tool(system, user, EQUATION_TOOL)
        _cache_set(key, result)
        return jsonify(result)
    except (InternalServerError, APIConnectionError, RateLimitError) as e:
        print(f"Definition endpoint failed: {repr(e)}")
        return jsonify({"error": "The AI service is temporarily unavailable. Please try again in a moment."}), 503
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    
@app.route('/.well-known/appspecific/com.chrome.devtools.json')
def _devtools_probe():
    return '', 404

@app.route('/favicon.ico')
def _favicon():
    return '', 204
    
@app.errorhandler(Exception)
def handle_error(e):
    if request.path.startswith("/api/"):
        app.logger.exception(e)
        return jsonify({"error": "Internal server error"}), 500
    if isinstance(e, HTTPException):
        return e
    raise e

    
# ── Text to Speech ───────────────────────────────────────────────────────────────
TTS_BASE = os.environ["TTS_BASE_URL"].rstrip("/")
TTS_MODEL = os.environ.get("TTS_MODEL", "indextts1.5")
TTS_VERSION = os.environ.get("TTS_VERSION", "kneo350")
TUTOR_TTS_MODEL = os.environ.get("TUTOR_TTS_MODEL", TTS_MODEL)
TUTOR_TTS_VERSION = os.environ.get("TUTOR_TTS_VERSION", TTS_VERSION)
TTS_MAX_CHARS_PER_REQUEST = max(100, int(os.environ.get("TTS_MAX_CHARS_PER_REQUEST", "500")))
TTS_MAX_RETRIES = 2


class TTSServiceError(Exception):
    def __init__(self, message, upstream=None, status_code=None):
        super().__init__(message)
        self.upstream = upstream
        self.status_code = status_code


class TutorSpeechSuperseded(Exception):
    pass


def _configured_tts_models():
    models = []
    seen = set()
    for model_name, version in (
        (TTS_MODEL, TTS_VERSION),
        (TUTOR_TTS_MODEL, TUTOR_TTS_VERSION),
    ):
        key = (model_name, version)
        if key not in seen:
            seen.add(key)
            models.append(key)
    return models


def _split_tts_text(text, max_chars=None):
    max_chars = max_chars or TTS_MAX_CHARS_PER_REQUEST
    remaining = re.sub(r"\s+", " ", text).strip()
    chunks = []

    while len(remaining) > max_chars:
        window = remaining[:max_chars + 1]
        sentence_cuts = [
            match.end()
            for match in re.finditer(r"[.!?](?:\s+|$)", window)
            if match.end() <= max_chars
        ]
        cut = sentence_cuts[-1] if sentence_cuts and sentence_cuts[-1] >= max_chars // 2 else None
        if cut is None:
            word_cuts = [match.start() for match in re.finditer(r"\s+", window) if match.start() <= max_chars]
            cut = word_cuts[-1] if word_cuts else max_chars

        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks


def _request_tts_wav_chunk(text, model_name, version, chunk_number, chunk_count):
    last_err = None

    for attempt in range(TTS_MAX_RETRIES + 1):
        try:
            r = requests.post(
                f"{TTS_BASE}/audio/tts",
                json={
                    "text": text,
                    "output_format": "audio",
                    "model_name": model_name,
                    "version": version
                },
                timeout=30
            )
            r.raise_for_status()
            return r.content
        except requests.exceptions.ChunkedEncodingError as e:
            last_err = e
            retrying = attempt < TTS_MAX_RETRIES
            app.logger.warning(
                f"TTS chunk {chunk_number}/{chunk_count} attempt {attempt + 1} "
                f"dropped mid-stream{', retrying' if retrying else ''}: {e}"
            )
            continue
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            upstream = e.response.text if e.response is not None else None
            app.logger.error(f"TTS upstream error: {status_code} - {upstream}")
            raise TTSServiceError("TTS service unavailable", upstream=upstream, status_code=status_code) from e
        except requests.RequestException as e:
            app.logger.error(f"TTS request failed: {type(e).__name__}: {e}")
            raise TTSServiceError("TTS service unavailable") from e

    app.logger.error(
        f"TTS chunk {chunk_number}/{chunk_count} failed after "
        f"{TTS_MAX_RETRIES + 1} attempts: {last_err}"
    )
    raise TTSServiceError("TTS service unstable") from last_err


def _merge_tts_wav_chunks(wav_chunks):
    if len(wav_chunks) == 1:
        return wav_chunks[0]

    audio_format = None
    frame_chunks = []
    try:
        for wav_bytes in wav_chunks:
            with wave.open(io.BytesIO(wav_bytes), "rb") as source:
                chunk_format = (
                    source.getnchannels(),
                    source.getsampwidth(),
                    source.getframerate(),
                    source.getcomptype(),
                    source.getcompname(),
                )
                if audio_format is None:
                    audio_format = chunk_format
                elif chunk_format != audio_format:
                    raise TTSServiceError("TTS chunks returned incompatible WAV formats")
                frame_chunks.append(source.readframes(source.getnframes()))
    except (EOFError, wave.Error) as e:
        raise TTSServiceError("TTS service returned an invalid WAV file") from e

    output = io.BytesIO()
    with wave.open(output, "wb") as merged:
        channels, sample_width, frame_rate, compression_type, compression_name = audio_format
        merged.setnchannels(channels)
        merged.setsampwidth(sample_width)
        merged.setframerate(frame_rate)
        merged.setcomptype(compression_type, compression_name)
        for frames in frame_chunks:
            merged.writeframes(frames)
    return output.getvalue()


def _synthesize_tts_wav(text, model_name=None, version=None, should_continue=None):
    model_name = model_name or TTS_MODEL
    version = version or TTS_VERSION
    text_chunks = _split_tts_text(text)
    if not text_chunks:
        raise TTSServiceError("No text provided for TTS")

    if len(text_chunks) > 1:
        app.logger.info(f"Splitting {len(text)} TTS characters into {len(text_chunks)} WAV chunks")

    wav_chunks = []
    for index, chunk in enumerate(text_chunks, start=1):
        if should_continue is not None and not should_continue():
            raise TutorSpeechSuperseded()
        wav_chunks.append(
            _request_tts_wav_chunk(chunk, model_name, version, index, len(text_chunks))
        )
        if should_continue is not None and not should_continue():
            raise TutorSpeechSuperseded()
    return _merge_tts_wav_chunks(wav_chunks)


def init_tts():
    for model_name, version in _configured_tts_models():
        try:
            r = requests.post(f"{TTS_BASE}/init_model", json={
                "model_name": model_name,
                "version": version
            })
            if not r.ok:
                print(f"TTS init failed: {r.status_code} - {r.text}")
            r.raise_for_status()
            print(f"TTS model initialized: {model_name}/{version}")
        except Exception as e:
            print(f"TTS init failed for {model_name}/{version}: {e}")

@app.route("/api/tts", methods=["POST"])
@limiter.limit("120 per hour")
def tts():
    text = request.json.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400
    app.logger.info(f"TTS request: {len(text)} chars")

    try:
        return Response(_synthesize_tts_wav(text), mimetype="audio/wav")
    except TTSServiceError as e:
        body = {"error": str(e)}
        if e.upstream is not None:
            body["upstream"] = e.upstream
        return jsonify(body), 502


# ── Avatar (OpenAvatarChat) LLM proxy ──────────────────────────────────────────
# OpenAvatarChat's LLMOpenAICompatible handler talks to this route as if it were
# an OpenAI-compatible endpoint. Lesson context will be added in a later step;
# for now this route is just a thin relay to llm_client, re-streamed as SSE in
# the shape the OpenAI SDK expects.
#
# Note: OAC's handler hardcodes a 5s client timeout (see llm_handler_openai_compatible.py).
# Send OpenAI-shaped empty chunks while the real LLM is thinking so OAC's streaming
# client does not time out before the first visible token arrives.
# @app.route("/api/avatar/v1/chat/completions", methods=["POST"])
# @limiter.limit("120 per hour")
# def avatar_chat_completions():
#     body = request.get_json(force=True, silent=True) or {}
#     messages = body.get("messages")
#     if not messages:
#         return jsonify({"error": "No messages provided"}), 400
#
#     model = body.get("model") or "gpt-5.4-mini"
#
#     def empty_chunk():
#         return {
#             "id": f"chatcmpl-keepalive-{uuid.uuid4().hex}",
#             "object": "chat.completion.chunk",
#             "created": int(time.time()),
#             "model": model,
#             "choices": [
#                 {
#                     "index": 0,
#                     "delta": {"role": "assistant", "content": ""},
#                     "finish_reason": None,
#                 }
#             ],
#         }
#
#     def generate():
#         chunks = Queue()
#
#         def run_llm_stream():
#             try:
#                 stream = llm_client.chat.completions.create(
#                     model=model,
#                     messages=messages,
#                     stream=True,
#                     stream_options={"include_usage": True},
#                 )
#                 for chunk in stream:
#                     chunks.put(("chunk", chunk))
#                 chunks.put(("done", None))
#             except (InternalServerError, APIConnectionError, RateLimitError) as e:
#                 app.logger.error(f"Avatar LLM proxy upstream error: {repr(e)}")
#                 chunks.put(("error", {"message": str(e), "type": "upstream_error"}))
#             except Exception as e:
#                 app.logger.error(f"Avatar LLM proxy failed: {repr(e)}")
#                 chunks.put(("error", {"message": str(e), "type": "proxy_error"}))
#
#         threading.Thread(target=run_llm_stream, daemon=True).start()
#
#         yield f"data: {json.dumps(empty_chunk())}\n\n"
#         while True:
#             try:
#                 kind, payload = chunks.get(timeout=2)
#             except Empty:
#                 yield f"data: {json.dumps(empty_chunk())}\n\n"
#                 continue
#
#             if kind == "chunk":
#                 yield f"data: {payload.model_dump_json()}\n\n"
#             elif kind == "error":
#                 yield f"data: {json.dumps({'error': payload})}\n\n"
#                 yield "data: [DONE]\n\n"
#                 return
#             else:
#                 yield "data: [DONE]\n\n"
#                 return
#
#     return Response(generate(), mimetype="text/event-stream")


# ── Pipeline orchestration ─────────────────────────────────────────────────────  ← new section

# concurrency cap
PIPELINE_CONCURRENCY = 3
_pipeline_semaphore = threading.Semaphore(PIPELINE_CONCURRENCY)

def _run_pipeline(lesson_id: str, file_path: str, original_filename: str, resume_from: str | None = None):
    """
    Runs all three stages in sequence on a background thread. Each stage
    writes its own output file into lessons/<id>/ as it completes — meta.json
    tracks status so the frontend can poll progress instead of holding one
    long HTTP request open.

    `stage` is tracked separately from the status we *attempt* to write so
    that on failure meta.json records which stage actually broke (status
    itself gets overwritten to FAILED, so without this we'd lose that info).
    """
    stage = resume_from or Status.EXTRACTING
    # with _pipeline_semaphore:
    try:
        if stage == Status.EXTRACTING:
            write_meta(lesson_id, status=stage, source_filename=original_filename)
            normalized = run_text_extraction(file_path, lesson_id)
        else:
            normalized = json.load(open(lesson_path(lesson_id, "normalized_output.json"), encoding="utf-8"))

        if stage in (Status.EXTRACTING, Status.BUILDING_CURRICULUM):
            stage = Status.BUILDING_CURRICULUM
            write_meta(lesson_id, status=stage)
            delete_quiz_questions_for_lesson(lesson_id)
            curriculum = run_curriculum_extraction(normalized, lesson_id)
            for concept in curriculum["curriculum_graph"]["concepts"]:
                questions = generate_quiz_batch(
                    concept["name"], concept["description"], concept["study"]["key_terms"],
                    lesson_id, concept["concept_id"]
                )
                insert_quiz_questions(lesson_id, concept["concept_id"], questions, generation_batch=0)
        else:
            curriculum = json.load(open(lesson_path(lesson_id, "extracted_concepts.json"), encoding="utf-8"))
        course_name = curriculum.get("curriculum_graph", {}).get("course", "Untitled course")

        if stage in (Status.EXTRACTING, Status.BUILDING_CURRICULUM, Status.GENERATING_SLIDES):
            stage = Status.GENERATING_SLIDES
            write_meta(lesson_id, status=stage, course=course_name)
            slideshow = run_slideshow_generation(curriculum, lesson_id)
            if "error" in slideshow:
                raise RuntimeError(f"Slideshow generation failed: {slideshow['error']}")
            slideshow = run_animation_generation(slideshow, lesson_id)
        else:
            slideshow = json.load(open(lesson_path(lesson_id, "slideshow.json"), encoding="utf-8"))

        slide_count = slideshow.get("slideshow", {}).get("total_slides", 0)
        write_meta(lesson_id, status=Status.READY, slide_count=slide_count)

    except Exception as e:
        traceback.print_exc()
        write_meta(lesson_id, status=Status.FAILED, failed_stage=stage, error=str(e))

start_workers(_run_pipeline, num_workers=PIPELINE_CONCURRENCY)

# summary endpoint for whole student Progress page
@app.route("/api/progress")
def all_progress():
    user_id, session_id = current_identity()

    owned_ids = set(get_lessons_for_owner(user_id, session_id))
    assigned = get_assigned_lessons_for_student(user_id) if user_id else []
    assigned_by_lesson = {a["lesson_id"]: a for a in assigned}

    all_ids = owned_ids | assigned_by_lesson.keys()

    result = []
    for lid in sorted(os.listdir(LESSONS_DIR), reverse=True):
        if lid not in all_ids or not os.path.isdir(os.path.join(LESSONS_DIR, lid)):
            continue
        try:
            meta = read_meta(lid)
        except (FileNotFoundError, ValueError):
            continue
        if meta.get("status") != "ready":
            continue

        history = get_quiz_history(user_id=user_id, session_id=session_id, lesson_id=lid)
        progress = get_lesson_progress(user_id=user_id, session_id=session_id, lesson_id=lid) or {}

        concept_names = {}
        curriculum_file = lesson_path(lid, "extracted_concepts.json")
        if os.path.exists(curriculum_file):
            with open(curriculum_file, "r", encoding="utf-8") as f:
                curriculum = json.load(f)
            concept_names = {c["concept_id"]: c["name"] for c in curriculum["curriculum_graph"]["concepts"]}
        for h in history:
            h["concept_name"] = concept_names.get(h["concept_id"], h["concept_id"])

        source_info = assigned_by_lesson.get(lid)
        result.append({
            "lesson": meta,
            "progress": progress,
            "quiz_history": history,
            "source": {
                "type": "class",
                "class_name": source_info["class_name"],
                "due_at": source_info["due_at"],
                "title": source_info["title"],
                "archived": bool(source_info["class_archived"]) or source_info["assignment_status"] == "archived",
            } if source_info else {"type": "personal", "archived": False},
        })
    return jsonify(result)


@app.route("/api/classes/<int:class_id>/students/<int:student_id>/progress")
def student_progress_for_teacher(class_id, student_id):
    user_id, _ = current_identity()
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    if not is_enrolled(class_id, student_id):
        return jsonify({"error": "Student not found"}), 404

    assigned = get_assigned_lessons_for_student_by_teacher(student_id, user_id)
    assigned_by_lesson = {a["lesson_id"]: a for a in assigned}

    result = []
    for lid in sorted(os.listdir(LESSONS_DIR), reverse=True):
        info = assigned_by_lesson.get(lid)
        if not info or not os.path.isdir(os.path.join(LESSONS_DIR, lid)):
            continue
        try:
            meta = read_meta(lid)
        except (FileNotFoundError, ValueError):
            continue
        if meta.get("status") != "ready":
            continue

        history = get_quiz_history(user_id=student_id, lesson_id=lid)
        progress = get_lesson_progress(user_id=student_id, lesson_id=lid) or {}

        concept_names = {}
        curriculum_file = lesson_path(lid, "extracted_concepts.json")
        if os.path.exists(curriculum_file):
            with open(curriculum_file, "r", encoding="utf-8") as f:
                curriculum = json.load(f)
            concept_names = {c["concept_id"]: c["name"] for c in curriculum["curriculum_graph"]["concepts"]}
        for h in history:
            h["concept_name"] = concept_names.get(h["concept_id"], h["concept_id"])

        result.append({
            "lesson": meta,
            "progress": progress,
            "quiz_history": history,
            "source": {
                "type": "class",
                "class_name": info["class_name"],
                "due_at": info["due_at"],
                "title": info["title"],
                "archived": bool(info["class_archived"]) or info["assignment_status"] == "archived",
            },
        })
    return jsonify(result)


@app.route("/api/lessons", methods=["POST"])
@limiter.limit("10 per hour")
def create_lesson():
    """Accepts the uploaded file, kicks off the pipeline in the background,
    and immediately returns a lesson_id the frontend can poll."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    lesson_id = new_lesson_id()
    user_id, session_id = current_identity()

    if get_user_role(user_id) != "teacher":
        existing = get_lessons_for_owner(user_id, session_id)
        if len(existing) >= 10:
            return jsonify({"error": "Lesson limit reached (10 max). Delete a lesson to add a new one."}), 400
    
    create_lesson_owner(lesson_id, session_id, user_id)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    saved_name = f"{lesson_id}{ext}"
    saved_path = os.path.join(UPLOAD_DIR, secure_filename(saved_name))
    f.save(saved_path)

    write_meta(lesson_id, status=Status.QUEUED, source_filename=f.filename)

    """
    thread = threading.Thread(
        target=_run_pipeline, args=(lesson_id, saved_path, f.filename), daemon=True
    )
    thread.start()
    """
    enqueue_job(lesson_id, saved_path, f.filename)

    return jsonify({"lesson_id": lesson_id}), 202


@app.route("/api/lessons/<lesson_id>/status")
@limiter.exempt
def lesson_status(lesson_id):
    """Frontend polls this while the pipeline runs."""
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    try:
        meta = read_meta(lesson_id)
        meta.update(STATUS_INFO.get(meta.get("status"), {}))
        return jsonify(meta)
    except (FileNotFoundError, ValueError):
        return jsonify({"error": "Lesson not found"}), 404
    

@app.route("/api/lessons/<lesson_id>/progress", methods=["POST"])
def update_progress(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    body = request.get_json(force=True)
    update_lesson_progress(
        session_id=session_id, user_id=user_id, lesson_id=lesson_id,
        last_viewed_slide=body.get("last_viewed_slide"), completed=body.get("completed"),
    )
    return jsonify({"ok": True})


@app.route("/api/lessons/<lesson_id>/progress")
def get_progress(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    progress = get_lesson_progress(user_id=user_id, session_id=session_id, lesson_id=lesson_id)
    return jsonify(progress or {})


@app.route("/api/lessons")
def list_lessons():
    """Now scoped to the caller — anonymous visitors see only lessons tied
    to their session_id, logged-in users see only their user_id's lessons."""
    user_id, session_id = current_identity()
    allowed_ids = set(get_lessons_for_owner(user_id, session_id))
    lessons = []
    if os.path.isdir(LESSONS_DIR):
        for entry in sorted(os.listdir(LESSONS_DIR), reverse=True):
            if entry not in allowed_ids:
                continue
            meta_file = os.path.join(LESSONS_DIR, entry, "meta.json")
            if os.path.exists(meta_file):
                with open(meta_file, "r", encoding="utf-8") as f:
                    lessons.append(json.load(f))
    return jsonify(lessons)


@app.route("/api/lessons/<lesson_id>", methods=["DELETE"])
def remove_lesson(lesson_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404

    assignment = get_assignment_for_lesson(lesson_id)
    if assignment and assignment["status"] != "draft":
        archive_assignment(assignment["id"])
        delete_lesson(lesson_id, preserve_history=True)
    else:
        if assignment:  # draft assignment with no real submissions to preserve
            delete_assignment(assignment["id"])
        delete_lesson(lesson_id)

    lesson_folder = lesson_dir(lesson_id)
    if os.path.isdir(lesson_folder):
        import shutil
        shutil.rmtree(lesson_folder)
    return jsonify({"ok": True})
    
@app.route("/api/auth/delete-account", methods=["POST"])
def delete_account():
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    # remove lesson folders from disk before the DB rows disappear —
    # once delete_user runs we lose the list of which lessons were theirs
    import shutil
    for lid in get_lesson_ids_for_user(user_id):
        folder = lesson_dir(lid)
        if os.path.isdir(folder):
            shutil.rmtree(folder)

    delete_user(user_id)
    session.pop("user_id", None)
    session.pop("session_id", None)  # fresh anonymous identity too, so nothing lingers in this browser session
    return jsonify({"ok": True})

# ── MCP  ───────────────────────────────────────────────────────────────
@app.route("/api/lessons/<lesson_id>/media/<path:filename>")
def get_lesson_media(lesson_id, filename):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    try:
        base_dir = lesson_dir(lesson_id)  # raises ValueError on a malformed lesson_id
    except ValueError:
        return jsonify({"error": "Lesson not found"}), 404
    # send_from_directory (werkzeug's safe_join) rejects ../ traversal on its own,
    # but the explicit realpath check below is cheap insurance against symlink escapes
    full_path = os.path.realpath(os.path.join(base_dir, filename))
    if not full_path.startswith(os.path.realpath(base_dir) + os.sep):
        return jsonify({"error": "Invalid path"}), 400
    return send_from_directory(base_dir, filename)

# ── OpenAvatarChat  ───────────────────────────────────────────────────────────────
# OPENAVATARCHAT_BASE_URL = os.environ.get("OPENAVATARCHAT_BASE_URL", "https://localhost:8283").rstrip("/")
# OPENAVATARCHAT_VERIFY_TLS = os.environ.get("OPENAVATARCHAT_VERIFY_TLS", "false").lower() in {"1", "true", "yes"}
# OPENAVATARCHAT_SIGNALING_TIMEOUT = int(os.environ.get("OPENAVATARCHAT_SIGNALING_TIMEOUT", "60"))
#
# @app.route("/api/lessons/<lesson_id>/avatar/webrtc/offer", methods=["POST"])
# @limiter.limit("600 per hour")
# def avatar_webrtc_offer(lesson_id):
#     user_id, session_id = current_identity()
#     if not resolve_lesson_access(lesson_id, user_id, session_id):
#         return jsonify({"error": "Lesson not found"}), 404
#
#     body = request.get_json(force=True, silent=True)
#     if not isinstance(body, dict):
#         return jsonify({"error": "Invalid JSON body"}), 400
#     if body.get("type") not in {"offer", "ice-candidate"}:
#         return jsonify({"error": "Unsupported WebRTC message type"}), 400
#     if not body.get("webrtc_id"):
#         return jsonify({"error": "Missing webrtc_id"}), 400
#
#     try:
#         upstream = requests.post(
#             f"{OPENAVATARCHAT_BASE_URL}/webrtc/offer",
#             json=body,
#             timeout=OPENAVATARCHAT_SIGNALING_TIMEOUT,
#             verify=OPENAVATARCHAT_VERIFY_TLS,
#         )
#     except requests.RequestException as e:
#         app.logger.error(f"OpenAvatarChat signaling proxy failed: {type(e).__name__}: {e}")
#         return jsonify({"error": "OpenAvatarChat signaling unavailable"}), 502
#
#     return Response(
#         upstream.content,
#         status=upstream.status_code,
#         content_type=upstream.headers.get("Content-Type", "application/json"),
#     )

# ── LiveTalking ────────────────────────────────────────────────────────────────
LIVETALKING_BASE_URL = os.environ.get("LIVETALKING_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
LIVETALKING_SIGNALING_TIMEOUT = int(os.environ.get("LIVETALKING_SIGNALING_TIMEOUT", "60"))
LIVETALKING_COMMAND_TIMEOUT = int(os.environ.get("LIVETALKING_COMMAND_TIMEOUT", "15"))
LIVETALKING_MAX_TEXT_LENGTH = 2000
TUTOR_ATTEMPT_TTL_SECONDS = 3600
TUTOR_CHAT_MODEL = os.environ.get("TUTOR_CHAT_MODEL", "gpt-5.4-mini")
TUTOR_CHAT_HISTORY_LIMIT = 12
TUTOR_CHAT_HISTORY_MAX_LENGTH = 12000
TUTOR_CONTEXT_CONCEPT_LIMIT = 5
_tutor_attempt_lock = threading.RLock()
_tutor_attempts = {}
_redis_url = os.environ.get("REDIS_URL", "memory://")
_tutor_attempt_redis = (
    Redis.from_url(_redis_url, decode_responses=True)
    if _redis_url.startswith(("redis://", "rediss://"))
    else None
)
TUTOR_CHAT_SYSTEM_PROMPT = (
    "You are the tutoring agent for the learner's current lesson. Ground answers in the supplied lesson "
    "context and prioritize the current slide or active study concept when the learner uses words like "
    "this, that, here, or it. For broader questions, connect the answer to the most relevant concepts in "
    "the lesson. You may use general knowledge to clarify the lesson, but do not contradict its material "
    "or invent lesson-specific details. If the available context is insufficient, say so and ask a brief "
    "clarifying question. Respond clearly and concisely in plain text that sounds natural when spoken aloud. "
    "Use short sentences and brief paragraphs. Do not use Markdown, bullet points, numbered lists, headings, "
    "tables, or list-marker hyphens. When listing several items, write them as natural prose using commas and "
    "conjunctions."
)
TUTOR_CONTEXT_STOP_WORDS = {
    "about", "after", "again", "also", "and", "are", "because", "been", "before", "being", "can",
    "could", "does", "explain", "from", "have", "help", "here", "how", "into", "just", "lesson", "mean",
    "means", "more", "most", "question", "that", "the", "their", "then", "there", "these", "they",
    "this", "those", "topic", "understand", "what", "when", "where", "which", "why", "with", "work", "works",
    "would", "you", "your",
}


@app.route("/api/lessons/<lesson_id>/avatar/webrtc/offer", methods=["POST"])
@limiter.limit("60 per hour")
def avatar_webrtc_offer(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body"}), 400
    if body.get("type") != "offer":
        return jsonify({"error": "WebRTC message type must be 'offer'"}), 400
    if not isinstance(body.get("sdp"), str) or not body["sdp"].strip():
        return jsonify({"error": "Missing SDP offer"}), 400

    try:
        upstream = requests.post(
            f"{LIVETALKING_BASE_URL}/offer",
            json={"sdp": body["sdp"], "type": body["type"]},
            timeout=LIVETALKING_SIGNALING_TIMEOUT,
        )
    except requests.RequestException as e:
        app.logger.error(f"LiveTalking signaling proxy failed: {type(e).__name__}: {e}")
        return jsonify({"error": "LiveTalking signaling unavailable"}), 502

    return Response(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/json"),
    )


def _tutor_context_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


def _tutor_context_terms(value):
    return {
        term
        for term in re.findall(r"[a-z0-9]+", _tutor_context_text(value).lower())
        if len(term) > 2 and term not in TUTOR_CONTEXT_STOP_WORDS
    }


def _tutor_concept_score(concept, question):
    question_text = question.lower()
    question_terms = _tutor_context_terms(question)
    if not question_terms:
        return 0

    name = _tutor_context_text(concept.get("name"))
    description = _tutor_context_text(concept.get("description"))
    study = concept.get("study") or {}
    key_terms = study.get("key_terms") or []
    formulas = study.get("formulas") or []
    flashcards = study.get("flashcards") or []

    score = 0
    if name and name.lower() in question_text:
        score += 20
    score += 5 * len(question_terms & _tutor_context_terms(name))
    score += 2 * len(question_terms & _tutor_context_terms(description))

    for item in key_terms:
        term = _tutor_context_text(item.get("term"))
        if term and term.lower() in question_text:
            score += 12
        score += 4 * len(question_terms & _tutor_context_terms(term))
        score += len(question_terms & _tutor_context_terms(item.get("definition")))

    supporting_text = [
        *(_tutor_context_text(item.get("latex")) + " " + _tutor_context_text(item.get("explanation")) for item in formulas),
        *(_tutor_context_text(item.get("front")) + " " + _tutor_context_text(item.get("back")) for item in flashcards),
    ]
    score += len(question_terms & _tutor_context_terms(" ".join(supporting_text)))
    return score


def _format_tutor_concept(concept):
    lines = [f"## {_tutor_context_text(concept.get('name'))}"]
    description = _tutor_context_text(concept.get("description"))
    if description:
        lines.append(description)

    study = concept.get("study") or {}
    key_terms = study.get("key_terms") or []
    if key_terms:
        lines.append("Key terms:")
        lines.extend(
            f"- {_tutor_context_text(item.get('term'))}: {_tutor_context_text(item.get('definition'))}"
            for item in key_terms
        )

    formulas = study.get("formulas") or []
    if formulas:
        lines.append("Formulas:")
        lines.extend(
            f"- {_tutor_context_text(item.get('latex'))}: {_tutor_context_text(item.get('explanation'))}"
            for item in formulas
        )

    flashcards = study.get("flashcards") or []
    if flashcards:
        lines.append("Study checks:")
        lines.extend(
            f"- Q: {_tutor_context_text(item.get('front'))} A: {_tutor_context_text(item.get('back'))}"
            for item in flashcards
        )

    return "\n".join(lines)


def _build_tutor_lesson_context(lesson_id, question, current_slide_index=None, active_concept_id=None, scene=None):
    with open(lesson_path(lesson_id, "extracted_concepts.json"), encoding="utf-8") as f:
        curriculum = json.load(f)["curriculum_graph"]
    with open(lesson_path(lesson_id, "slideshow.json"), encoding="utf-8") as f:
        slideshow = json.load(f)["slideshow"]

    concepts = curriculum.get("concepts") or []
    slides = slideshow.get("slides") or []
    concept_lookup = {
        concept.get("concept_id"): concept
        for concept in concepts
        if concept.get("concept_id")
    }

    current_slide = None
    if current_slide_index is not None:
        if current_slide_index < 0 or current_slide_index >= len(slides):
            raise IndexError("Current slide is out of range")
        current_slide = slides[current_slide_index]

    selected_ids = []
    if active_concept_id in concept_lookup:
        selected_ids.append(active_concept_id)

    slide_concept_ids = []
    if current_slide:
        for concept_id in current_slide.get("concept_ids") or []:
            if concept_id in concept_lookup and concept_id not in selected_ids and concept_id not in slide_concept_ids:
                slide_concept_ids.append(concept_id)

    available_slots = TUTOR_CONTEXT_CONCEPT_LIMIT - len(selected_ids)
    if len(slide_concept_ids) <= available_slots:
        selected_ids.extend(slide_concept_ids)
    else:
        ranked_slide_concepts = sorted(
            (_tutor_concept_score(concept_lookup[concept_id], question), concept_id)
            for concept_id in slide_concept_ids
        )
        for score, concept_id in reversed(ranked_slide_concepts):
            if score <= 0 or len(selected_ids) >= TUTOR_CONTEXT_CONCEPT_LIMIT:
                break
            selected_ids.append(concept_id)

    ranked_concepts = sorted(
        (
            (_tutor_concept_score(concept, question), concept.get("concept_id"))
            for concept in concepts
            if concept.get("concept_id") and concept.get("concept_id") not in selected_ids
        ),
        reverse=True,
    )
    for score, concept_id in ranked_concepts:
        if score <= 0 or len(selected_ids) >= TUTOR_CONTEXT_CONCEPT_LIMIT:
            break
        selected_ids.append(concept_id)

    assignment = get_assignment_for_lesson(lesson_id)
    course_name = (
        assignment.get("title")
        if assignment and assignment.get("title")
        else curriculum.get("course", "Untitled course")
    )
    parts = [
        "# Lesson overview",
        f"Course: {course_name}",
        "Concept map: " + "; ".join(
            f"{concept.get('concept_id')}: {_tutor_context_text(concept.get('name'))}"
            for concept in concepts
        ),
    ]

    location_lines = ["# Student location"]
    if scene in {"slideshow", "study"}:
        location_lines.append(f"View: {scene}")
    if current_slide:
        location_lines.extend([
            f"Current slide: {current_slide_index + 1} of {len(slides)}",
            f"Title: {_tutor_context_text(current_slide.get('title'))}",
            f"Type: {_tutor_context_text(current_slide.get('type'))}",
        ])
        body = current_slide.get("body") or []
        if body:
            location_lines.append("Visible slide content:")
            for item in body:
                item_type = _tutor_context_text(item.get("type")) or "content"
                content = _tutor_context_text(item.get("content"))
                if content:
                    location_lines.append(f"- [{item_type}] {content}")
        speaker_notes = _tutor_context_text(current_slide.get("speaker_notes"))
        if speaker_notes:
            location_lines.append(f"Speaker notes: {speaker_notes}")
    if active_concept_id in concept_lookup:
        location_lines.append(
            "Active study concept: " + _tutor_context_text(concept_lookup[active_concept_id].get("name"))
        )
    parts.append("\n".join(location_lines))

    if selected_ids:
        parts.append(
            "# Detailed relevant concepts\n" + "\n\n".join(
                _format_tutor_concept(concept_lookup[concept_id])
                for concept_id in selected_ids
            )
        )

    return "\n\n".join(parts)


@app.route("/api/lessons/<lesson_id>/tutor/message", methods=["POST"])
@limiter.limit("120 per hour")
def tutor_message(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "No message provided"}), 400
    message = message.strip()
    if len(message) > LIVETALKING_MAX_TEXT_LENGTH:
        return jsonify({"error": f"Message must be {LIVETALKING_MAX_TEXT_LENGTH} characters or fewer"}), 400

    history = body.get("history", [])
    if not isinstance(history, list) or len(history) > 50:
        return jsonify({"error": "Invalid conversation history"}), 400

    normalized_history = []
    history_length = 0
    for item in history[-TUTOR_CHAT_HISTORY_LIMIT:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            return jsonify({"error": "Invalid conversation history"}), 400
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            return jsonify({"error": "Invalid conversation history"}), 400
        content = content.strip()
        if len(content) > LIVETALKING_MAX_TEXT_LENGTH:
            return jsonify({"error": "Conversation message is too long"}), 400
        history_length += len(content)
        normalized_history.append({"role": item["role"], "content": content})

    if history_length > TUTOR_CHAT_HISTORY_MAX_LENGTH:
        return jsonify({"error": "Conversation history is too long"}), 400

    current_slide_index = body.get("current_slide_index")
    if current_slide_index is not None and (
        isinstance(current_slide_index, bool) or not isinstance(current_slide_index, int)
    ):
        return jsonify({"error": "Invalid current slide index"}), 400

    active_concept_id = body.get("active_concept_id")
    if active_concept_id is not None and (
        not isinstance(active_concept_id, str) or len(active_concept_id) > 128
    ):
        return jsonify({"error": "Invalid active concept ID"}), 400

    scene = body.get("scene")
    if scene is not None and scene not in {"slideshow", "study"}:
        return jsonify({"error": "Invalid lesson view"}), 400

    try:
        lesson_context = _build_tutor_lesson_context(
            lesson_id,
            message,
            current_slide_index=current_slide_index,
            active_concept_id=active_concept_id,
            scene=scene,
        )
    except (FileNotFoundError, KeyError, IndexError):
        return jsonify({"error": "Lesson context is unavailable"}), 404
    except (json.JSONDecodeError, ValueError) as e:
        app.logger.error(f"Tutor lesson context is invalid: {e}")
        return jsonify({"error": "Lesson context is invalid"}), 500

    try:
        completion = llm_client.chat.completions.create(
            model=TUTOR_CHAT_MODEL,
            messages=[
                {"role": "system", "content": TUTOR_CHAT_SYSTEM_PROMPT},
                {"role": "system", "content": "LESSON CONTEXT\n" + lesson_context},
                *normalized_history,
                {"role": "user", "content": message},
            ],
            max_tokens=500,
        )
        reply = completion.choices[0].message.content
    except (InternalServerError, APIConnectionError, RateLimitError) as e:
        app.logger.error(f"Tutor LLM upstream error: {type(e).__name__}: {e}")
        return jsonify({"error": "Tutor response service unavailable"}), 502
    except Exception as e:
        app.logger.exception(f"Tutor LLM request failed: {type(e).__name__}: {e}")
        return jsonify({"error": "Tutor response could not be generated"}), 502

    if not isinstance(reply, str) or not reply.strip():
        app.logger.error("Tutor LLM returned an empty response")
        return jsonify({"error": "Tutor response was empty"}), 502

    reply = reply.strip()
    if len(reply) > LIVETALKING_MAX_TEXT_LENGTH:
        reply = reply[:LIVETALKING_MAX_TEXT_LENGTH - 3].rsplit(" ", 1)[0].rstrip() + "..."

    return jsonify({"reply": reply})


TUTOR_SPEECH_UNITS = {
    "mm": "millimeters",
    "cm": "centimeters",
    "m": "meters",
    "km": "kilometers",
    "in": "inches",
    "ft": "feet",
    "yd": "yards",
    "mi": "miles",
}
TUTOR_SPEECH_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")


def _normalize_tutor_speech_text(text):
    def replace_unit_power(match):
        power = match.group("power") or match.group("power_unicode")
        dimension = "square" if power in {"2", "²"} else "cubic"
        return f"{dimension} {TUTOR_SPEECH_UNITS[match.group('unit')]}"

    def replace_superscript(match):
        superscript = match.group(0)
        if superscript == "²":
            return " squared"
        if superscript == "³":
            return " cubed"
        exponent = superscript.translate(TUTOR_SPEECH_SUPERSCRIPT_DIGITS)
        if exponent.startswith("-"):
            exponent = "negative " + exponent[1:]
        return " to the power of " + exponent

    speech = text.strip()
    standalone_symbol = {
        "-": "minus",
        "−": "minus",
        "<": "less than",
        ">": "greater than",
    }.get(speech)
    if standalone_symbol:
        return standalone_symbol

    speech = speech.replace("`", "")
    speech = re.sub(
        r"\b(?P<unit>mm|cm|km|m|in|ft|yd|mi)\s*(?:\^(?:\{)?(?P<power>2|3)(?:\})?|(?P<power_unicode>[²³]))",
        replace_unit_power,
        speech,
    )
    speech = re.sub(r"\^\s*(?:\{\s*)?2\s*(?:\})?", " squared", speech)
    speech = re.sub(r"\^\s*(?:\{\s*)?3\s*(?:\})?", " cubed", speech)
    speech = re.sub(
        r"\^\s*(?:\{\s*)?(-?\d+)\s*(?:\})?",
        lambda match: f" to the power of {match.group(1)}",
        speech,
    )
    speech = re.sub(
        r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+",
        replace_superscript,
        speech,
    )
    speech = speech.replace("°C", " degrees Celsius").replace("°F", " degrees Fahrenheit")
    speech = speech.replace("°", " degrees")
    speech = re.sub(r"(?<=\d)\s*[-−]\s*(?=\d)", " minus ", speech)
    speech = re.sub(r"(?<![\w)])[-−]\s*(?=\d)", " negative ", speech)
    speech = re.sub(r"(?<![A-Za-z0-9])[-−](?![A-Za-z0-9])", " minus ", speech)
    for symbol, spoken in {
        "×": " times ",
        "÷": " divided by ",
        "≠": " does not equal ",
        "≤": " is less than or equal to ",
        "≥": " is greater than or equal to ",
        "<=": " is less than or equal to ",
        ">=": " is greater than or equal to ",
        "=": " equals ",
        "<": " is less than ",
        ">": " is greater than ",
        "%": " percent",
    }.items():
        speech = speech.replace(symbol, spoken)
    speech = re.sub(r"[ \t]+", " ", speech)
    speech = re.sub(r"\s*\n+\s*", ". ", speech)
    speech = re.sub(r"\s+([,.;:!?])", r"\1", speech)
    return speech.strip()


class LiveTalkingCommandError(Exception):
    pass


def _parse_livetalking_command_response(upstream):
    try:
        result = upstream.json()
    except ValueError as e:
        raise LiveTalkingCommandError("Invalid response from LiveTalking") from e

    if not isinstance(result, dict) or result.get("code") != 0:
        message = (
            result.get("msg", "LiveTalking command failed")
            if isinstance(result, dict)
            else "LiveTalking command failed"
        )
        raise LiveTalkingCommandError(message)
    return result


def _post_livetalking_command(path, payload):
    upstream = requests.post(
        f"{LIVETALKING_BASE_URL}{path}",
        json=payload,
        timeout=LIVETALKING_COMMAND_TIMEOUT,
    )
    upstream.raise_for_status()
    return _parse_livetalking_command_response(upstream)


def _post_livetalking_audio(avatar_session_id, wav_bytes):
    upstream = requests.post(
        f"{LIVETALKING_BASE_URL}/humanaudio",
        data={"sessionid": avatar_session_id},
        files={"file": ("tutor.wav", wav_bytes, "audio/wav")},
        timeout=LIVETALKING_COMMAND_TIMEOUT,
    )
    upstream.raise_for_status()
    return _parse_livetalking_command_response(upstream)


def _avatar_session_id_from_body(body):
    avatar_session_id = body.get("sessionid") if isinstance(body, dict) else None
    if not isinstance(avatar_session_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", avatar_session_id
    ):
        return None
    return avatar_session_id


def _avatar_attempt_id_from_body(body):
    attempt_id = body.get("attempt_id") if isinstance(body, dict) else None
    if isinstance(attempt_id, bool) or not isinstance(attempt_id, int) or attempt_id < 1:
        return None
    return attempt_id


def _tutor_attempt_key(avatar_session_id):
    return f"kneron:tutor-attempt:{avatar_session_id}"


@contextmanager
def _tutor_attempt_guard(avatar_session_id):
    if _tutor_attempt_redis is not None:
        lock = _tutor_attempt_redis.lock(
            _tutor_attempt_key(avatar_session_id) + ":lock",
            timeout=30,
            blocking_timeout=10,
        )
        if not lock.acquire(blocking=True):
            raise RedisError("Timed out waiting for tutor attempt lock")
        try:
            yield
        finally:
            lock.release()
        return
    with _tutor_attempt_lock:
        yield


def _set_current_tutor_attempt(avatar_session_id, attempt_id):
    value = str(attempt_id)
    if _tutor_attempt_redis is not None:
        _tutor_attempt_redis.set(
            _tutor_attempt_key(avatar_session_id),
            value,
            ex=TUTOR_ATTEMPT_TTL_SECONDS,
        )
        return
    with _tutor_attempt_lock:
        _tutor_attempts[avatar_session_id] = value


def _claim_or_is_current_tutor_attempt(avatar_session_id, attempt_id):
    value = str(attempt_id)
    if _tutor_attempt_redis is not None:
        key = _tutor_attempt_key(avatar_session_id)
        if _tutor_attempt_redis.set(key, value, nx=True, ex=TUTOR_ATTEMPT_TTL_SECONDS):
            return True
        return _tutor_attempt_redis.get(key) == value
    with _tutor_attempt_lock:
        current = _tutor_attempts.setdefault(avatar_session_id, value)
        return current == value


def _is_current_tutor_attempt(avatar_session_id, attempt_id):
    value = str(attempt_id)
    if _tutor_attempt_redis is not None:
        return _tutor_attempt_redis.get(_tutor_attempt_key(avatar_session_id)) == value
    with _tutor_attempt_lock:
        return _tutor_attempts.get(avatar_session_id) == value


@app.route("/api/lessons/<lesson_id>/avatar/speak", methods=["POST"])
@limiter.limit("120 per hour")
def avatar_speak(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    avatar_session_id = _avatar_session_id_from_body(body)
    if avatar_session_id is None:
        return jsonify({"error": "Invalid avatar session ID"}), 400
    attempt_id = _avatar_attempt_id_from_body(body)
    if attempt_id is None:
        return jsonify({"error": "Invalid tutor attempt ID"}), 400

    try:
        if not _claim_or_is_current_tutor_attempt(avatar_session_id, attempt_id):
            return jsonify({"error": "Tutor response was superseded", "superseded": True}), 409
    except RedisError as e:
        app.logger.error(f"Tutor attempt store unavailable: {e}")
        return jsonify({"error": "Tutor coordination service unavailable"}), 503

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "No text provided"}), 400
    text = text.strip()
    if len(text) > LIVETALKING_MAX_TEXT_LENGTH:
        return jsonify({"error": f"Text must be {LIVETALKING_MAX_TEXT_LENGTH} characters or fewer"}), 400
    speech_text = _normalize_tutor_speech_text(text)
    if len(speech_text) > LIVETALKING_MAX_TEXT_LENGTH:
        speech_text = speech_text[:LIVETALKING_MAX_TEXT_LENGTH - 3].rsplit(" ", 1)[0].rstrip() + "..."

    interrupt = body.get("interrupt", True)
    if not isinstance(interrupt, bool):
        return jsonify({"error": "interrupt must be a boolean"}), 400

    try:
        if interrupt:
            with _tutor_attempt_guard(avatar_session_id):
                if not _is_current_tutor_attempt(avatar_session_id, attempt_id):
                    raise TutorSpeechSuperseded()
                _post_livetalking_command("/interrupt_talk", {"sessionid": avatar_session_id})
        wav_bytes = _synthesize_tts_wav(
            speech_text,
            model_name=TUTOR_TTS_MODEL,
            version=TUTOR_TTS_VERSION,
            should_continue=lambda: _is_current_tutor_attempt(avatar_session_id, attempt_id),
        )
        with _tutor_attempt_guard(avatar_session_id):
            if not _is_current_tutor_attempt(avatar_session_id, attempt_id):
                raise TutorSpeechSuperseded()
            _post_livetalking_audio(avatar_session_id, wav_bytes)
    except TutorSpeechSuperseded:
        return jsonify({"error": "Tutor response was superseded", "superseded": True}), 409
    except RedisError as e:
        app.logger.error(f"Tutor attempt store unavailable: {e}")
        return jsonify({"error": "Tutor coordination service unavailable"}), 503
    except TTSServiceError as e:
        app.logger.error(f"Tutor TTS failed: {e}")
        body = {"error": str(e)}
        if e.upstream is not None:
            body["upstream"] = e.upstream
        return jsonify(body), 502
    except requests.RequestException as e:
        app.logger.error(f"LiveTalking speech proxy failed: {type(e).__name__}: {e}")
        return jsonify({"error": "LiveTalking speech service unavailable"}), 502
    except LiveTalkingCommandError as e:
        app.logger.warning(f"LiveTalking rejected speech request: {e}")
        return jsonify({"error": str(e)}), 502

    return jsonify({"ok": True})


@app.route("/api/lessons/<lesson_id>/avatar/interrupt", methods=["POST"])
@limiter.limit("240 per hour")
def avatar_interrupt(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body"}), 400
    avatar_session_id = _avatar_session_id_from_body(body)
    if avatar_session_id is None:
        return jsonify({"error": "Invalid avatar session ID"}), 400
    attempt_id = _avatar_attempt_id_from_body(body)
    if attempt_id is None:
        return jsonify({"error": "Invalid tutor attempt ID"}), 400

    try:
        with _tutor_attempt_guard(avatar_session_id):
            _set_current_tutor_attempt(avatar_session_id, attempt_id)
            _post_livetalking_command("/interrupt_talk", {"sessionid": avatar_session_id})
    except RedisError as e:
        app.logger.error(f"Tutor attempt store unavailable: {e}")
        return jsonify({"error": "Tutor coordination service unavailable"}), 503
    except requests.RequestException as e:
        app.logger.error(f"LiveTalking interrupt proxy failed: {type(e).__name__}: {e}")
        return jsonify({"error": "LiveTalking interrupt service unavailable"}), 502
    except LiveTalkingCommandError as e:
        app.logger.warning(f"LiveTalking rejected interrupt request: {e}")
        return jsonify({"error": str(e)}), 502

    return jsonify({"ok": True})


@app.route("/api/lessons/<lesson_id>/avatar/speaking", methods=["POST"])
@limiter.limit("1200 per hour")
def avatar_speaking(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body"}), 400
    avatar_session_id = _avatar_session_id_from_body(body)
    if avatar_session_id is None:
        return jsonify({"error": "Invalid avatar session ID"}), 400

    try:
        result = _post_livetalking_command("/is_speaking", {"sessionid": avatar_session_id})
    except requests.RequestException as e:
        app.logger.error(f"LiveTalking speaking-status proxy failed: {type(e).__name__}: {e}")
        return jsonify({"error": "LiveTalking speaking-status service unavailable"}), 502
    except LiveTalkingCommandError as e:
        app.logger.warning(f"LiveTalking rejected speaking-status request: {e}")
        return jsonify({"error": str(e)}), 502

    if not isinstance(result.get("data"), bool):
        app.logger.warning("LiveTalking speaking-status response did not contain a boolean state")
        return jsonify({"error": "Invalid speaking status from LiveTalking"}), 502
    return jsonify({"speaking": result["data"]})


@app.route("/api/lessons/<lesson_id>/avatar/disconnect", methods=["POST"])
@limiter.limit("240 per hour")
def avatar_disconnect(lesson_id):
    user_id, session_id = current_identity()
    if not resolve_lesson_access(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body"}), 400
    avatar_session_id = _avatar_session_id_from_body(body)
    if avatar_session_id is None:
        return jsonify({"error": "Invalid avatar session ID"}), 400

    try:
        result = _post_livetalking_command("/close_session", {"sessionid": avatar_session_id})
    except requests.RequestException as e:
        app.logger.error(f"LiveTalking disconnect proxy failed: {type(e).__name__}: {e}")
        return jsonify({"error": "LiveTalking disconnect service unavailable"}), 502
    except LiveTalkingCommandError as e:
        app.logger.warning(f"LiveTalking rejected disconnect request: {e}")
        return jsonify({"error": str(e)}), 502

    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("closed"), bool):
        app.logger.warning("LiveTalking disconnect response did not contain a boolean state")
        return jsonify({"error": "Invalid disconnect response from LiveTalking"}), 502
    return jsonify({"ok": True, "closed": data["closed"]})

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        init_tts()
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)
