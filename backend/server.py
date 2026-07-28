import json
import os
import uuid
import hashlib
import logging
import requests
import threading
import traceback
import time
import glob
from datetime import timedelta, datetime, timezone
from flask import Flask, jsonify, send_from_directory, request, Response, session
from openai import OpenAI, APIConnectionError, RateLimitError, InternalServerError
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from dotenv import load_dotenv
load_dotenv()

from lesson_paths import (
    new_lesson_id, lesson_dir, lesson_path, write_meta, read_meta, LESSONS_DIR, Status, STATUS_INFO
)
from db import (
    init_db, record_quiz_attempt, get_quiz_history, update_lesson_progress, 
    get_lesson_progress, create_user, get_user_by_email, claim_session, 
    create_lesson_owner, get_lessons_for_owner, owns_lesson, get_db,
    update_user_password, owns_lesson, get_db, delete_lesson,
    delete_user, get_lesson_ids_for_user, get_quiz_question, insert_quiz_questions, 
    get_max_batch, deactivate_batch, get_active_quiz_questions, 
    save_item, unsave_item, get_saved_items, update_user_name,
    create_password_reset, get_valid_reset, consume_reset_token, 
    create_class, archive_class, get_classes_for_teacher, generate_join_code,
    get_valid_join_code_for_class, resolve_join_code, join_class, leave_class,
    get_enrollments_for_class
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

    return jsonify({"ok": True, "email": user["email"]})


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
    return jsonify({"classes": get_classes_for_teacher(user_id)})


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


@app.route("/api/classes/<int:class_id>/invite-code", methods=["POST"])
def generate_invite_code_route(class_id):
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    code = generate_join_code(class_id)
    return jsonify({"ok": True, "code": code})


@app.route("/api/classes/<int:class_id>/roster", methods=["GET"])
def class_roster(class_id):
    user_id, _ = current_identity()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not require_teacher_owns_class(class_id, user_id):
        return jsonify({"error": "Class not found"}), 404
    return jsonify({"students": get_enrollments_for_class(class_id)})


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

@app.route("/api/lessons/<lesson_id>/slideshow")
def get_slideshow(lesson_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404  # 404, not 403 — don't leak existence
    try:
        with open(lesson_path(lesson_id, "slideshow.json"), "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (FileNotFoundError, ValueError):
        return jsonify({"error": "Lesson not found or not finished generating."}), 404
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid JSON: {str(e)}"}), 500

@app.route("/api/lessons/<lesson_id>/curriculum")
def get_curriculum(lesson_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    try:
        with open(lesson_path(lesson_id, "extracted_concepts.json"), "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (FileNotFoundError, ValueError):
        return jsonify({"error": "Lesson not found or not finished generating."}), 404
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid JSON: {str(e)}"}), 500
    
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
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    body = request.get_json(force=True)
    is_review = bool(body.get("review"))

    batch_timestamp = datetime.now(timezone.utc).isoformat()
    touched_concept_ids = set()
    for a in body["attempts"]:
        question = get_quiz_question(a["question_id"])
        if question is None or question["lesson_id"] != lesson_id:
            continue
        concept_id = question["concept_id"]
        correct_answer = question["answer"]
        answer_given = a.get("answer_given")
        is_correct = str(answer_given).strip().lower() == str(correct_answer).strip().lower()
        touched_concept_ids.add(concept_id)

        if is_review:
            continue

        record_quiz_attempt(
            session_id=session_id, user_id=user_id, lesson_id=lesson_id, concept_id=concept_id,
            question_id=a["question_id"], question_text=question["question_text"],
            answer_given=answer_given, correct_answer=correct_answer,
            explanation=question["explanation"], is_correct=is_correct,
            submitted_at=batch_timestamp,
        )

    if not is_review:
        for concept_id in touched_concept_ids:
            threading.Thread(
                target=_regenerate_quiz_batch, args=(lesson_id, concept_id), daemon=True
            ).start()

    return jsonify({"ok": True})


@app.route("/api/lessons/<lesson_id>/concepts/<concept_id>/quiz")
def get_concept_quiz(lesson_id, concept_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    questions = get_active_quiz_questions(lesson_id, concept_id)
    # choices is stored as a JSON string in the DB column — decode before sending
    for q in questions:
        q["choices"] = json.loads(q["choices"]) if q["choices"] else None
    return jsonify(questions)


@app.route("/api/lessons/<lesson_id>/quiz-history")
def quiz_history(lesson_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    concept_id = request.args.get("concept_id")
    history = get_quiz_history(user_id=user_id, session_id=session_id, lesson_id=lesson_id, concept_id=concept_id)
    return jsonify(history)

@app.route("/api/lessons/<lesson_id>/saved-items", methods=["GET"])
def list_saved_items(lesson_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    return jsonify(get_saved_items(session_id, lesson_id, user_id))


@app.route("/api/lessons/<lesson_id>/saved-items/<item_id>", methods=["POST"])
def add_saved_item(lesson_id, item_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    body = request.get_json(force=True)
    save_item(session_id, lesson_id, item_id, body["item_type"], user_id, body.get("content"))
    return jsonify({"ok": True})


@app.route("/api/lessons/<lesson_id>/saved-items/<item_id>", methods=["DELETE"])
def remove_saved_item(lesson_id, item_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
        return jsonify({"error": "Lesson not found"}), 404
    unsave_item(session_id, lesson_id, item_id)
    return jsonify({"ok": True})
    
# ── Info card routes ──────────────────────────────────────────────────────────
@app.route("/api/lessons/<lesson_id>/info/definition", methods=["POST"])
@limiter.limit("60 per hour")
def info_definition(lesson_id):
    user_id, session_id = current_identity()
    if not owns_lesson(lesson_id, user_id, session_id):
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
    if not owns_lesson(lesson_id, user_id, session_id):
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
TTS_BASE = os.environ["TTS_BASE_URL"]
TTS_MODEL = "indextts1.5"
TTS_VERSION = "kneo350"

def init_tts():
    try:
        r = requests.post(f"{TTS_BASE}/init_model", json={
            "model_name": TTS_MODEL,
            "version": TTS_VERSION
        })
        if not r.ok:
            print(f"TTS init failed: {r.status_code} - {r.text}")
        r.raise_for_status()
        print("TTS model initialized")
    except Exception as e:
        print(f"TTS init failed: {e}")

@app.route("/api/tts", methods=["POST"])
@limiter.limit("120 per hour")
def tts():
    text = request.json.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400
    try:
        r = requests.post(
            f"{TTS_BASE}/audio/tts",
            json={
                "text": text,
                "output_format": "audio",
                "model_name": TTS_MODEL,
                "version": TTS_VERSION
            },
            timeout=30
        )
        r.raise_for_status()
        return Response(r.content, mimetype="audio/wav")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    allowed_ids = set(get_lessons_for_owner(user_id, session_id))
    lessons = [
        read_meta(lid) for lid in sorted(os.listdir(LESSONS_DIR), reverse=True)
        if lid in allowed_ids and os.path.isdir(os.path.join(LESSONS_DIR, lid))
    ]
    result = []
    for meta in lessons:
        if meta.get("status") != "ready":
            continue
        history = get_quiz_history(user_id=user_id, session_id=session_id, lesson_id=meta["lesson_id"])
        progress = get_lesson_progress(user_id=user_id, session_id=session_id, lesson_id=meta["lesson_id"]) or {}

        concept_names = {}
        curriculum_file = lesson_path(meta["lesson_id"], "extracted_concepts.json")
        if os.path.exists(curriculum_file):
            with open(curriculum_file, "r", encoding="utf-8") as f:
                curriculum = json.load(f)
            concept_names = {c["concept_id"]: c["name"] for c in curriculum["curriculum_graph"]["concepts"]}
        for h in history:
            h["concept_name"] = concept_names.get(h["concept_id"], h["concept_id"])

        result.append({"lesson": meta, "progress": progress, "quiz_history": history})
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
    if not owns_lesson(lesson_id, user_id, session_id):
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
    if not owns_lesson(lesson_id, user_id, session_id):
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
    if not owns_lesson(lesson_id, user_id, session_id):
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
    if not owns_lesson(lesson_id, user_id, session_id):
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

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        init_tts()
    app.run(debug=True, port=5000, threaded=True)