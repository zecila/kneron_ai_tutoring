import os
import json
import sqlite3
import secrets
import string
from datetime import datetime, timezone, timedelta

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

def _add_column_if_missing(conn, table, column, ddl):
     """Race-safe ALTER TABLE ADD COLUMN — tolerates concurrent gunicorn workers
     all running init_db() at boot."""
     try:
         conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
         conn.commit()
         return True
     except sqlite3.OperationalError as e:
         if "duplicate column name" in str(e):
             return False
         raise

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT NOT NULL UNIQUE,
            password_hash   TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS password_resets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            token      TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS classes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id      INTEGER NOT NULL REFERENCES users(id),
            name            TEXT NOT NULL,
            archived        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS enrollments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id        INTEGER NOT NULL REFERENCES classes(id),
            student_id      INTEGER NOT NULL REFERENCES users(id),
            joined_at       TEXT NOT NULL,
            UNIQUE (class_id, student_id)
        );

        CREATE TABLE IF NOT EXISTS join_codes (
            code            TEXT PRIMARY KEY,
            class_id        INTEGER NOT NULL REFERENCES classes(id),
            expires_at      TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS assignments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id        INTEGER NOT NULL REFERENCES classes(id),
            lesson_id       TEXT NOT NULL REFERENCES lessons(lesson_id),
            teacher_id      INTEGER NOT NULL REFERENCES users(id),
            status          TEXT NOT NULL DEFAULT 'draft',  -- draft | published | archived
            title           TEXT,               -- teacher override; falls back to lesson's LLM-generated title, nullable
            due_at          TEXT,               -- soft due date, nullable
            max_attempts    INTEGER,            -- per-concept quiz attempt cap, nullable = unlimited
            created_at      TEXT NOT NULL,
            published_at    TEXT
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

        CREATE TABLE IF NOT EXISTS student_quiz_batches (
            user_id             INTEGER NOT NULL REFERENCES users(id),
            lesson_id           TEXT NOT NULL,
            concept_id          TEXT NOT NULL,
            generation_batch    INTEGER NOT NULL,
            attempt_number      INTEGER NOT NULL,
            updated_at          TEXT NOT NULL,
            PRIMARY KEY (user_id, lesson_id, concept_id)
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
            _add_column_if_missing(conn, table, "user_id", "INTEGER REFERENCES users(id)")

    # Signed-in progress belongs to the account, not to the browser session.
    # Keep the newest legacy row before enforcing that invariant.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """DELETE FROM lesson_progress AS older
           WHERE older.user_id IS NOT NULL
             AND EXISTS (
                 SELECT 1
                 FROM lesson_progress AS newer
                 WHERE newer.user_id = older.user_id
                   AND newer.lesson_id = older.lesson_id
                   AND (
                       newer.updated_at > older.updated_at
                       OR (newer.updated_at = older.updated_at AND newer.rowid > older.rowid)
                   )
             )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_lesson_progress_user_lesson
           ON lesson_progress(user_id, lesson_id)
           WHERE user_id IS NOT NULL"""
    )
    conn.commit()

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_attempts)").fetchall()}
    if "question_id" not in cols:
        _add_column_if_missing(conn, "quiz_attempts", "question_id", "TEXT REFERENCES quiz_questions(question_id)")
    if "explanation" not in cols:
        _add_column_if_missing(conn, "quiz_attempts", "explanation", "TEXT")
    if _add_column_if_missing(conn, "quiz_attempts", "attempt_number", "INTEGER"):
         # only backfill if this worker was the one that actually added the column
         conn.execute(
             """UPDATE quiz_attempts SET attempt_number = ranked.rnk
                FROM (
                    SELECT id, DENSE_RANK() OVER (
                        PARTITION BY COALESCE(user_id, session_id), lesson_id, concept_id
                        ORDER BY submitted_at
                    ) AS rnk
                    FROM quiz_attempts
                ) AS ranked
                WHERE quiz_attempts.id = ranked.id"""
         )
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
    _add_column_if_missing(conn, "assignments", "title", "TEXT")

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


def create_password_reset(user_id, ttl_minutes=30):
    conn = get_db()
    conn.execute("UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0", (user_id,))
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
    conn.execute(
        "INSERT INTO password_resets (user_id, token, expires_at, used, created_at) VALUES (?, ?, ?, 0, ?)",
        (user_id, token, expires_at, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    return token


def get_valid_reset(token):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM password_resets WHERE token = ? AND used = 0", (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        return None
    return row


def consume_reset_token(token):
    conn = get_db()
    conn.execute("UPDATE password_resets SET used = 1 WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def claim_session(session_id, user_id):
    """Called once at signup. Reassigns everything this anonymous session
    created — quiz history, lesson progress, and lesson ownership — to the
    new account. Idempotent: only touches rows not already claimed, so
    calling it twice (e.g. a retried request) is harmless."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        anonymous_progress = conn.execute(
            """SELECT rowid, * FROM lesson_progress
               WHERE session_id = ? AND user_id IS NULL""",
            (session_id,),
        ).fetchall()
        for progress in anonymous_progress:
            account_progress = conn.execute(
                """SELECT rowid, * FROM lesson_progress
                   WHERE user_id = ? AND lesson_id = ?
                   ORDER BY updated_at DESC, rowid DESC
                   LIMIT 1""",
                (user_id, progress["lesson_id"]),
            ).fetchone()
            if account_progress:
                if progress["updated_at"] >= account_progress["updated_at"]:
                    conn.execute(
                        """UPDATE lesson_progress
                           SET last_viewed_slide = ?, completed = ?, updated_at = ?
                           WHERE rowid = ?""",
                        (
                            progress["last_viewed_slide"],
                            progress["completed"],
                            progress["updated_at"],
                            account_progress["rowid"],
                        ),
                    )
                conn.execute("DELETE FROM lesson_progress WHERE rowid = ?", (progress["rowid"],))
            else:
                conn.execute(
                    "UPDATE lesson_progress SET user_id = ? WHERE rowid = ?",
                    (user_id, progress["rowid"]),
                )

        conn.execute("UPDATE quiz_attempts SET user_id = ? WHERE session_id = ? AND user_id IS NULL", (user_id, session_id))
        conn.execute("UPDATE lessons SET user_id = ? WHERE session_id = ? AND user_id IS NULL", (user_id, session_id))
        conn.execute("UPDATE saved_items SET user_id = ? WHERE session_id = ? AND user_id IS NULL", (user_id, session_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



def create_class(teacher_id, name):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO classes (teacher_id, name, archived, created_at) VALUES (?, ?, 0, ?)",
        (teacher_id, name, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    class_id = cur.lastrowid
    conn.close()
    return class_id


def archive_class(class_id, teacher_id):
    conn = get_db()
    conn.execute(
        "UPDATE classes SET archived = 1 WHERE id = ? AND teacher_id = ?",
        (class_id, teacher_id)
    )
    conn.commit()
    conn.close()


def update_class_name(class_id, teacher_id, name):
    conn = get_db()
    conn.execute(
        "UPDATE classes SET name = ? WHERE id = ? AND teacher_id = ?",
        (name, class_id, teacher_id)
    )
    conn.commit()
    conn.close()
    

def get_classes_for_teacher(teacher_id, include_archived=False):
    conn = get_db()
    query = "SELECT * FROM classes WHERE teacher_id = ?"
    if not include_archived:
        query += " AND archived = 0"
    rows = conn.execute(query, (teacher_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_classes_for_student(student_id):
    conn = get_db()
    rows = conn.execute(
        """SELECT classes.*, users.first_name AS teacher_first_name, users.last_name AS teacher_last_name
           FROM classes
           JOIN enrollments ON enrollments.class_id = classes.id
           JOIN users ON users.id = classes.teacher_id
           WHERE enrollments.student_id = ? AND classes.archived = 0""",
        (student_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_role(user_id):
    conn = get_db()
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row["role"] if row else None


def generate_join_code(class_id, ttl_minutes=30, length=6):
    conn = get_db()
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(length))
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=ttl_minutes)).isoformat()
    conn.execute(
        "INSERT INTO join_codes (code, class_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (code, class_id, expires_at, now.isoformat())
    )
    conn.commit()
    conn.close()
    return {"code": code, "expires_at": expires_at}


def get_valid_join_code_for_class(class_id):
    """Returns the row for the latest code, only if it's still unexpired."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM join_codes WHERE class_id = ? ORDER BY created_at DESC LIMIT 1",
        (class_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        return None
    return row


def resolve_join_code(code):
    """Looks up a code and confirms it's the class's current (latest) code, not just any past one."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM join_codes WHERE code = ?", (code,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        return None
    latest = get_valid_join_code_for_class(row["class_id"])
    if not latest or latest["code"] != code:
        return None  # a newer code has since been generated
    return dict(row)


def join_class(class_id, student_id):
    conn = get_db()
    conn.execute(
        """INSERT INTO enrollments (class_id, student_id, joined_at) VALUES (?, ?, ?)
           ON CONFLICT(class_id, student_id) DO NOTHING""",
        (class_id, student_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    return changed > 0  # False means already enrolled — caller can surface "already joined"


def leave_class(class_id, student_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM enrollments WHERE class_id = ? AND student_id = ?",
        (class_id, student_id)
    )
    conn.commit()
    conn.close()


def get_enrollments_for_class(class_id):
    conn = get_db()
    rows = conn.execute(
        """SELECT u.id, u.email, u.first_name, u.last_name, e.joined_at
           FROM enrollments e JOIN users u ON u.id = e.student_id
           WHERE e.class_id = ?""",
        (class_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_assignment(class_id, lesson_id, teacher_id):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO assignments (class_id, lesson_id, teacher_id, status, created_at)
           VALUES (?, ?, ?, 'draft', ?)""",
        (class_id, lesson_id, teacher_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    return cur.lastrowid


def get_assignment(assignment_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    return dict(row) if row else None


def get_assignments_for_class(class_id, include_archived=False):
    conn = get_db()
    query = "SELECT * FROM assignments WHERE class_id = ?"
    if not include_archived:
        query += " AND status != 'archived'"
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, (class_id,)).fetchall()
    return [dict(r) for r in rows]


def is_enrolled(class_id, student_id):
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM enrollments WHERE class_id = ? AND student_id = ?",
        (class_id, student_id)
    ).fetchone()
    conn.close()
    return row is not None

# sorts by due date
def get_published_assignments_for_student(class_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM assignments WHERE class_id = ? AND status = 'published' ORDER BY due_at IS NULL, due_at ASC",
        (class_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_assigned_lessons_for_student(student_id):
    """Published assignments this student is enrolled to receive, with
    enough class context for the progress page to label them."""
    conn = get_db()
    rows = conn.execute(
        """SELECT a.lesson_id, a.id AS assignment_id, a.due_at, a.title,
                  a.status AS assignment_status, c.archived AS class_archived,
                  c.id AS class_id, c.name AS class_name
           FROM assignments a
           JOIN enrollments e ON e.class_id = a.class_id
           JOIN classes c ON c.id = a.class_id
           WHERE a.status IN ('published', 'archived') AND e.student_id = ?""",
        (student_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_assigned_lessons_for_student_by_teacher(student_id, teacher_id):
    """Published assignments this student can see, restricted to classes
    owned by this teacher — used for the teacher's view into a student's
    progress, so a teacher never sees a student's personal lessons or
    assignments from another teacher's class."""
    conn = get_db()
    rows = conn.execute(
        """SELECT a.lesson_id, a.id AS assignment_id, a.due_at, a.title,
                  a.status AS assignment_status, c.archived AS class_archived,
                  c.id AS class_id, c.name AS class_name
           FROM assignments a
           JOIN enrollments e ON e.class_id = a.class_id
           JOIN classes c ON c.id = a.class_id
           WHERE a.status IN ('published', 'archived') AND e.student_id = ? AND c.teacher_id = ?""",
        (student_id, teacher_id)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# view draft assignment
def get_assignment_for_lesson(lesson_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM assignments WHERE lesson_id = ? ORDER BY created_at DESC LIMIT 1",
        (lesson_id,)
    ).fetchone()
    return dict(row) if row else None


def publish_assignment(assignment_id, due_at=None, max_attempts=None, title=None):
    conn = get_db()
    conn.execute(
        """UPDATE assignments SET status = 'published', due_at = ?, max_attempts = ?, title = ?, published_at = ?
           WHERE id = ? AND status = 'draft'""",
        (due_at, max_attempts, title, datetime.now(timezone.utc).isoformat(), assignment_id)
    )
    conn.commit()


def archive_assignment(assignment_id):
    conn = get_db()
    conn.execute("UPDATE assignments SET status = 'archived' WHERE id = ?", (assignment_id,))
    conn.commit()


def delete_assignment(assignment_id):
    conn = get_db()
    conn.execute("DELETE FROM assignments WHERE id = ? AND status = 'draft'", (assignment_id,))
    conn.commit()


def update_assignment(assignment_id, due_at=None, max_attempts=None, title=None):
    conn = get_db()
    conn.execute(
        "UPDATE assignments SET due_at = ?, max_attempts = ?, title = ? WHERE id = ? AND status = 'published'",
        (due_at, max_attempts, title, assignment_id)
    )
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


def delete_lesson(lesson_id, preserve_history=False):
    conn = get_db()
    conn.execute("DELETE FROM student_quiz_batches WHERE lesson_id = ?", (lesson_id,))
    conn.execute("DELETE FROM lessons WHERE lesson_id = ?", (lesson_id,))
    if not preserve_history:
        conn.execute("DELETE FROM quiz_attempts WHERE lesson_id = ?", (lesson_id,))
        conn.execute("DELETE FROM lesson_progress WHERE lesson_id = ?", (lesson_id,))
    conn.commit()
    conn.close()


def resolve_lesson_access(lesson_id, user_id, session_id):
    """Returns None (no access), or a dict describing how this identity may
    access the lesson: {"role": "owner"} for the teacher who created it
    (including personal, non-assignment lessons), or
    {"role": "student", "assignment_id": ...} for an enrolled student on a
    published assignment."""
    conn = get_db()
    lesson = conn.execute(
        "SELECT user_id, session_id FROM lessons WHERE lesson_id = ?", (lesson_id,)
    ).fetchone()
    if not lesson:
        conn.close()
        return None
    if lesson["user_id"] is not None:
        if user_id is not None and lesson["user_id"] == user_id:
            conn.close()
            return {"role": "owner"}
    elif lesson["session_id"] == session_id:
        conn.close()
        return {"role": "owner"}

    if user_id is None:
        conn.close()
        return None
    assignment = conn.execute(
        """SELECT a.id, a.class_id FROM assignments a
           JOIN enrollments e ON e.class_id = a.class_id
           WHERE a.lesson_id = ? AND a.status = 'published' AND e.student_id = ?""",
        (lesson_id, user_id)
    ).fetchone()
    conn.close()
    if assignment:
        return {"role": "student", "assignment_id": assignment["id"]}
    return None


def get_lesson_ids_for_user(user_id):
    conn = get_db()
    rows = conn.execute("SELECT lesson_id FROM lessons WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()
    return [r["lesson_id"] for r in rows]

def delete_user(user_id):
    """Delete an account and all database rows it owns in one transaction.

    Lesson folders on disk aren't touched here; the caller removes them after
    this transaction succeeds.
    """
    conn = get_db()
    try:
        owned_lessons = "SELECT lesson_id FROM lessons WHERE user_id = ?"
        owned_classes = "SELECT id FROM classes WHERE teacher_id = ?"

        # Assignments reference both classes and lessons, so they must go
        # before either owner row. This also covers an inconsistent legacy row
        # whose teacher/class/lesson ownership does not all agree.
        conn.execute(
            f"""DELETE FROM assignments
                WHERE teacher_id = ?
                   OR class_id IN ({owned_classes})
                   OR lesson_id IN ({owned_lessons})""",
            (user_id, user_id, user_id),
        )

        # Removing an owned lesson removes every learner's activity for that
        # lesson. Removing a student account also removes their activity on
        # lessons owned by somebody else.
        for table in ("quiz_attempts", "lesson_progress", "saved_items", "student_quiz_batches"):
            conn.execute(
                f"DELETE FROM {table} WHERE user_id = ? OR lesson_id IN ({owned_lessons})",
                (user_id, user_id),
            )
        conn.execute(
            f"DELETE FROM quiz_questions WHERE lesson_id IN ({owned_lessons})",
            (user_id,),
        )

        conn.execute(
            f"DELETE FROM join_codes WHERE class_id IN ({owned_classes})",
            (user_id,),
        )
        conn.execute(
            f"DELETE FROM enrollments WHERE student_id = ? OR class_id IN ({owned_classes})",
            (user_id, user_id),
        )
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM lessons WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM classes WHERE teacher_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_quiz_question(question_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM quiz_questions WHERE question_id = ?", (question_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_active_quiz_questions(lesson_id, concept_id, user_id=None):
    conn = get_db()
    generation_batch = None
    if user_id is not None:
        selection = conn.execute(
            """SELECT generation_batch FROM student_quiz_batches
               WHERE user_id = ? AND lesson_id = ? AND concept_id = ?""",
            (user_id, lesson_id, concept_id),
        ).fetchone()
        if selection:
            generation_batch = selection["generation_batch"]

    if generation_batch is None:
        rows = conn.execute(
            """SELECT * FROM quiz_questions
               WHERE lesson_id = ? AND concept_id = ? AND active = 1
               ORDER BY question_id""",
            (lesson_id, concept_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM quiz_questions
               WHERE lesson_id = ? AND concept_id = ? AND generation_batch = ?
               ORDER BY question_id""",
            (lesson_id, concept_id, generation_batch),
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


def _validate_quiz_choices(q):
    choices = q.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Quiz choices must be a non-empty list")
    if q.get("answer") not in choices:
        raise ValueError(f"Quiz answer is not among choices: {q.get('answer')!r}")


def insert_quiz_questions(lesson_id, concept_id, questions, generation_batch):
    for question in questions:
        _validate_quiz_choices(question)
    conn = get_db()
    try:
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
    finally:
        conn.close()


def replace_active_quiz_questions(
        lesson_id, concept_id, questions, user_id=None, attempt_number=None):
    """Insert a regenerated batch and activate it canonically or for one student."""
    for question in questions:
        _validate_quiz_choices(question)
    if not questions:
        raise ValueError("Cannot activate an empty quiz batch")
    if user_id is not None and attempt_number is None:
        raise ValueError("A student quiz batch requires an attempt number")

    conn = get_db()
    try:
        # Serialize batch allocation across workers and never expose a state
        # where both the old and new batches are active.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT MAX(generation_batch) AS max_batch FROM quiz_questions WHERE lesson_id = ? AND concept_id = ?",
            (lesson_id, concept_id),
        ).fetchone()
        generation_batch = (row["max_batch"] if row["max_batch"] is not None else -1) + 1
        now = datetime.now(timezone.utc).isoformat()

        if user_id is None:
            conn.execute(
                """UPDATE quiz_questions SET active = 0
                   WHERE lesson_id = ? AND concept_id = ? AND active = 1""",
                (lesson_id, concept_id),
            )
        active = int(user_id is None)
        for i, q in enumerate(questions):
            question_id = f"{lesson_id}_{concept_id}_b{generation_batch}_q{i:03d}"
            conn.execute(
                """INSERT INTO quiz_questions
                   (question_id, concept_id, lesson_id, question_text, type, choices,
                    answer, explanation, generation_batch, active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (question_id, concept_id, lesson_id, q["question"], q["type"],
                 json.dumps(q["choices"], ensure_ascii=False), q["answer"], q["explanation"],
                 generation_batch, active, now),
            )
        if user_id is not None:
            conn.execute(
                """INSERT INTO student_quiz_batches
                       (user_id, lesson_id, concept_id, generation_batch, attempt_number, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, lesson_id, concept_id) DO UPDATE SET
                     generation_batch = excluded.generation_batch,
                     attempt_number   = excluded.attempt_number,
                     updated_at       = excluded.updated_at
                   WHERE excluded.attempt_number >= student_quiz_batches.attempt_number""",
                (user_id, lesson_id, concept_id, generation_batch, attempt_number, now),
            )
        conn.commit()
        return generation_batch
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_quiz_questions_for_lesson(lesson_id):
    """Clears any quiz questions already generated for this lesson — used
    before a curriculum-stage retry, which regenerates the curriculum (and
    therefore quiz questions) from scratch and would otherwise collide with
    the deterministic question_id of anything inserted before the failure."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM student_quiz_batches WHERE lesson_id = ?", (lesson_id,))
        conn.execute("DELETE FROM quiz_questions WHERE lesson_id = ?", (lesson_id,))
        conn.commit()
    finally:
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
                         attempt_number, user_id=None, submitted_at=None, explanation=None):
    conn = get_db()
    conn.execute(
        """INSERT INTO quiz_attempts
           (session_id, user_id, lesson_id, concept_id, question_id, question_text,
            answer_given, correct_answer, explanation, is_correct, submitted_at, attempt_number)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, user_id, lesson_id, concept_id, question_id, question_text,
         answer_given, correct_answer, explanation, int(is_correct),
         submitted_at or datetime.now(timezone.utc).isoformat(), attempt_number)
    )
    conn.commit()
    conn.close()

def get_attempt_count(user_id, session_id, lesson_id, concept_id):
    conn = get_db()
    if user_id:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) AS n FROM quiz_attempts WHERE user_id = ? AND lesson_id = ? AND concept_id = ?",
            (user_id, lesson_id, concept_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) AS n FROM quiz_attempts WHERE session_id = ? AND user_id IS NULL AND lesson_id = ? AND concept_id = ?",
            (session_id, lesson_id, concept_id)
        ).fetchone()
    conn.close()
    return row["n"]


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
    if user_id is not None:
        conn.execute(
            """INSERT INTO lesson_progress
                   (session_id, lesson_id, user_id, last_viewed_slide, completed, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, lesson_id) WHERE user_id IS NOT NULL DO UPDATE SET
                 last_viewed_slide = COALESCE(excluded.last_viewed_slide, lesson_progress.last_viewed_slide),
                 completed         = COALESCE(excluded.completed, lesson_progress.completed),
                 updated_at        = excluded.updated_at""",
            (session_id, lesson_id, user_id, last_viewed_slide,
             int(completed) if completed is not None else None, now),
        )
    else:
        conn.execute(
            """INSERT INTO lesson_progress
                   (session_id, lesson_id, user_id, last_viewed_slide, completed, updated_at)
               VALUES (?, ?, NULL, ?, ?, ?)
               ON CONFLICT(session_id, lesson_id) DO UPDATE SET
                 last_viewed_slide = COALESCE(excluded.last_viewed_slide, lesson_progress.last_viewed_slide),
                 completed         = COALESCE(excluded.completed, lesson_progress.completed),
                 updated_at        = excluded.updated_at""",
            (session_id, lesson_id, last_viewed_slide,
             int(completed) if completed is not None else None, now),
        )
    conn.commit()
    conn.close()


def get_lesson_progress(user_id=None, session_id=None, lesson_id=None):
    conn = get_db()
    if user_id:
        row = conn.execute(
            """SELECT * FROM lesson_progress
               WHERE user_id = ? AND lesson_id = ?
               ORDER BY updated_at DESC, rowid DESC
               LIMIT 1""",
            (user_id, lesson_id),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT * FROM lesson_progress
               WHERE session_id = ? AND user_id IS NULL AND lesson_id = ?
               LIMIT 1""",
            (session_id, lesson_id)
        ).fetchone()
    conn.close()
    return dict(row) if row else None
