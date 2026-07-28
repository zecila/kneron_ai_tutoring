import os
import json
import sqlite3
from datetime import datetime, timezone

from lesson_paths import BACKEND_DIR
DB_PATH = os.path.join(BACKEND_DIR, "data", "app.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_db():
    """One connection per call — cheap enough for SQLite, avoids sharing
    a connection across threads (the pipeline already runs in a background
    thread per lesson)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row   # lets us access columns by name
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT NOT NULL UNIQUE,
            password_hash   TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lessons (
            lesson_id       TEXT PRIMARY KEY,
            session_id      TEXT NOT NULL,
            user_id         INTEGER REFERENCES users(id),
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lesson_progress (
            session_id          TEXT NOT NULL,
            user_id             INTEGER REFERENCES users(id),
            lesson_id           TEXT NOT NULL,
            last_viewed_slide   INTEGER DEFAULT 0,
            completed           INTEGER DEFAULT 0,
            updated_at          TEXT NOT NULL,
            PRIMARY KEY (session_id, lesson_id)
        );
                       
        CREATE TABLE IF NOT EXISTS quiz_questions (
            question_id     TEXT PRIMARY KEY,   -- e.g. "c001_q007"
            concept_id      TEXT NOT NULL,
            lesson_id       TEXT NOT NULL,
            question_text   TEXT NOT NULL,
            type            TEXT NOT NULL,      -- multiple_choice | true_false
            choices         TEXT,               -- JSON array, null for true_false
            answer          TEXT NOT NULL,
            explanation     TEXT,
            generation_batch INTEGER NOT NULL,  -- increments each regen
            active          INTEGER DEFAULT 1,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quiz_attempts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id      TEXT NOT NULL,
                    user_id         INTEGER REFERENCES users(id),
                    lesson_id       TEXT NOT NULL,
                    concept_id      TEXT NOT NULL,
                    question_id     TEXT NOT NULL REFERENCES quiz_questions(question_id),
                    question_text   TEXT NOT NULL,
                    answer_given    TEXT,
                    correct_answer  TEXT NOT NULL,
                    explanation     TEXT,
                    is_correct      INTEGER NOT NULL,
                    submitted_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS saved_items (
            session_id      TEXT NOT NULL,
            user_id         INTEGER REFERENCES users(id),
            lesson_id       TEXT NOT NULL,
            item_id         TEXT NOT NULL,
            item_type       TEXT NOT NULL,     -- keyterm | formula | flashcard | quiz
            content         TEXT,              -- JSON snapshot, only needed for quiz (frozen at star-time)
            created_at      TEXT NOT NULL,
            PRIMARY KEY (session_id, lesson_id, item_id)
        );

    """)
    conn.commit()

    # migration: add user_id if the table predates it
    for table in ("lesson_progress", "lessons", "quiz_attempts", "saved_items"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "user_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER REFERENCES users(id)")
            conn.commit()

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_attempts)").fetchall()}
    if "question_id" not in cols:
        conn.execute("ALTER TABLE quiz_attempts ADD COLUMN question_id TEXT REFERENCES quiz_questions(question_id)")
        conn.commit()
    if "explanation" not in cols:
        conn.execute("ALTER TABLE quiz_attempts ADD COLUMN explanation TEXT")
        conn.commit()
    if "question_index" in cols:
        # question_index predates question_id-based tracking and is now
        # unused/dead weight — but it's still NOT NULL, so any insert that
        # doesn't supply it (all current code) fails. SQLite 3.35+ supports
        # DROP COLUMN directly.
        conn.execute("ALTER TABLE quiz_attempts DROP COLUMN question_index")
        conn.commit()

    # add first_name/last_name/role if the table predates it
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "first_name" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
        conn.commit()
    if "last_name" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_name TEXT")
        conn.commit()
    if "role" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('student','teacher'))")
        conn.commit()

    conn.close()


def create_user(email, password_hash, role="student", first_name=None, last_name=None):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, role, first_name, last_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (email, password_hash, role, first_name, last_name, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return user_id


def update_user_name(user_id, first_name, last_name):
    conn = get_db()
    conn.execute("UPDATE users SET first_name = ?, last_name = ? WHERE id = ?", (first_name, last_name, user_id))
    conn.commit()
    conn.close()


def get_user_by_email(email):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_user_password(user_id, password_hash):
    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    conn.commit()
    conn.close()


def claim_session(session_id, user_id):
    """Called once at signup. Reassigns everything this anonymous session
    created — quiz history, lesson progress, and lesson ownership — to the
    new account. Idempotent: only touches rows not already claimed, so
    calling it twice (e.g. a retried request) is harmless."""
    conn = get_db()
    conn.execute("UPDATE quiz_attempts SET user_id = ? WHERE session_id = ? AND user_id IS NULL", (user_id, session_id))
    conn.execute("UPDATE lesson_progress SET user_id = ? WHERE session_id = ? AND user_id IS NULL", (user_id, session_id))
    conn.execute("UPDATE lessons SET user_id = ? WHERE session_id = ? AND user_id IS NULL", (user_id, session_id))
    conn.execute("UPDATE saved_items SET user_id = ? WHERE session_id = ? AND user_id IS NULL", (user_id, session_id))
    conn.commit()
    conn.close()

def create_lesson_owner(lesson_id, session_id, user_id=None):
    conn = get_db()
    conn.execute(
        "INSERT INTO lessons (lesson_id, session_id, user_id, created_at) VALUES (?, ?, ?, ?)",
        (lesson_id, session_id, user_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def get_lessons_for_owner(user_id=None, session_id=None):
    conn = get_db()
    if user_id:
        rows = conn.execute("SELECT lesson_id FROM lessons WHERE user_id = ?", (user_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT lesson_id FROM lessons WHERE session_id = ? AND user_id IS NULL", (session_id,)
        ).fetchall()
    conn.close()
    return [r["lesson_id"] for r in rows]


def owns_lesson(lesson_id, user_id=None, session_id=None):
    conn = get_db()
    row = conn.execute("SELECT user_id, session_id FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone()
    conn.close()
    if not row:
        return False
    if row["user_id"] is not None:
        return user_id is not None and row["user_id"] == user_id
    return row["session_id"] == session_id

def delete_lesson(lesson_id):
    conn = get_db()
    conn.execute("DELETE FROM lessons WHERE lesson_id = ?", (lesson_id,))
    conn.execute("DELETE FROM quiz_attempts WHERE lesson_id = ?", (lesson_id,))
    conn.execute("DELETE FROM lesson_progress WHERE lesson_id = ?", (lesson_id,))
    conn.commit()
    conn.close()

def get_lesson_ids_for_user(user_id):
    conn = get_db()
    rows = conn.execute("SELECT lesson_id FROM lessons WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()
    return [r["lesson_id"] for r in rows]

def delete_user(user_id):
    """Deletes the user row and every row elsewhere keyed to it. Lesson
    folders on disk aren't touched here — the caller (server.py) removes
    those, since db.py doesn't know about the filesystem layout."""
    conn = get_db()
    conn.execute("DELETE FROM quiz_attempts WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM lesson_progress WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM lessons WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_quiz_question(question_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM quiz_questions WHERE question_id = ?", (question_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_active_quiz_questions(lesson_id, concept_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM quiz_questions WHERE lesson_id = ? AND concept_id = ? AND active = 1 ORDER BY question_id",
        (lesson_id, concept_id)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_max_batch(lesson_id, concept_id):
    conn = get_db()
    row = conn.execute(
        "SELECT MAX(generation_batch) AS max_batch FROM quiz_questions WHERE lesson_id = ? AND concept_id = ?",
        (lesson_id, concept_id)
    ).fetchone()
    conn.close()
    return row["max_batch"] if row["max_batch"] is not None else -1


def insert_quiz_questions(lesson_id, concept_id, questions, generation_batch):
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    for i, q in enumerate(questions):
        question_id = f"{lesson_id}_{concept_id}_b{generation_batch}_q{i:03d}"
        conn.execute(
            """INSERT INTO quiz_questions
               (question_id, concept_id, lesson_id, question_text, type, choices,
                answer, explanation, generation_batch, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (question_id, concept_id, lesson_id, q["question"], q["type"],
             json.dumps(q["choices"], ensure_ascii=False), q["answer"], q["explanation"],
             generation_batch, now)
        )
    conn.commit()
    conn.close()


def deactivate_batch(lesson_id, concept_id, generation_batch):
    conn = get_db()
    conn.execute(
        "UPDATE quiz_questions SET active = 0 WHERE lesson_id = ? AND concept_id = ? AND generation_batch = ?",
        (lesson_id, concept_id, generation_batch)
    )
    conn.commit()
    conn.close()

def record_quiz_attempt(session_id, lesson_id, concept_id, question_id,
                         question_text, answer_given, correct_answer, is_correct,
                         user_id=None, submitted_at=None, explanation=None):
    conn = get_db()
    conn.execute(
        """INSERT INTO quiz_attempts
           (session_id, user_id, lesson_id, concept_id, question_id, question_text,
            answer_given, correct_answer, explanation, is_correct, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, user_id, lesson_id, concept_id, question_id, question_text,
         answer_given, correct_answer, explanation, int(is_correct),
         submitted_at or datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def get_quiz_history(user_id=None, session_id=None, lesson_id=None, concept_id=None):
    conn = get_db()
    # Prefer user_id when present — session_id alone would miss history
    # claimed under a different browser session for the same account.
    if user_id:
        query, params = "SELECT * FROM quiz_attempts WHERE user_id = ?", [user_id]
    else:
        query, params = "SELECT * FROM quiz_attempts WHERE session_id = ? AND user_id IS NULL", [session_id]
    if lesson_id:
        query += " AND lesson_id = ?"
        params.append(lesson_id)
    if concept_id:
        query += " AND concept_id = ?"
        params.append(concept_id)
    query += " ORDER BY submitted_at ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_item(session_id, lesson_id, item_id, item_type, user_id=None, content=None):
    conn = get_db()
    conn.execute(
        """INSERT INTO saved_items (session_id, user_id, lesson_id, item_id, item_type, content, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id, lesson_id, item_id) DO NOTHING""",
        (session_id, user_id, lesson_id, item_id, item_type,
         json.dumps(content, ensure_ascii=False) if content is not None else None,
         datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def unsave_item(session_id, lesson_id, item_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM saved_items WHERE session_id = ? AND lesson_id = ? AND item_id = ?",
        (session_id, lesson_id, item_id)
    )
    conn.commit()
    conn.close()


def get_saved_items(session_id, lesson_id, user_id=None):
    conn = get_db()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM saved_items WHERE user_id = ? AND lesson_id = ?", (user_id, lesson_id)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM saved_items WHERE session_id = ? AND user_id IS NULL AND lesson_id = ?",
            (session_id, lesson_id)
        ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["content"] = json.loads(d["content"]) if d["content"] else None
        out.append(d)
    return out

# Single atomic upsert instead of SELECT-then-branch — avoids a race where
# two near-simultaneous requests for the same session/lesson interleave
# and one write silently overwrites the other.
def update_lesson_progress(session_id, lesson_id, user_id=None, last_viewed_slide=None, completed=None):
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO lesson_progress (session_id, lesson_id, user_id, last_viewed_slide, completed, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id, lesson_id) DO UPDATE SET
             user_id            = COALESCE(excluded.user_id, lesson_progress.user_id),
             last_viewed_slide  = COALESCE(excluded.last_viewed_slide, lesson_progress.last_viewed_slide),
             completed          = COALESCE(excluded.completed, lesson_progress.completed),
             updated_at         = excluded.updated_at""",
        (session_id, lesson_id, user_id, last_viewed_slide,
         int(completed) if completed is not None else None, now)
    )
    conn.commit()
    conn.close()


def get_lesson_progress(user_id=None, session_id=None, lesson_id=None):
    conn = get_db()
    if user_id:
        row = conn.execute(
            "SELECT * FROM lesson_progress WHERE user_id = ? AND lesson_id = ?", (user_id, lesson_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM lesson_progress WHERE session_id = ? AND user_id IS NULL AND lesson_id = ?",
            (session_id, lesson_id)
        ).fetchone()
    conn.close()
    return dict(row) if row else None