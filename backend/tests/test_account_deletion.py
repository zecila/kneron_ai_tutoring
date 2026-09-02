import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://llm.test/v1")
os.environ.setdefault("TTS_BASE_URL", "http://tts.test")
os.environ.setdefault("REDIS_URL", "memory://")

import db
import server
from werkzeug.security import generate_password_hash


class AccountDeletionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp_dir.name) / "app.db")
        db.init_db()
        server.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def create_user(self, email, role):
        return db.create_user(email, "hash", role, role.title(), "User")

    def seed_lesson(self, lesson_id, owner_id, session_id):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO lessons (lesson_id, session_id, user_id, created_at) VALUES (?, ?, ?, ?)",
            (lesson_id, session_id, owner_id, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            """INSERT INTO quiz_questions
               (question_id, concept_id, lesson_id, question_text, type, choices,
                answer, explanation, generation_batch, active, created_at)
               VALUES (?, 'concept-1', ?, 'Question?', 'true_false', NULL,
                       'True', 'Because.', 1, 1, '2026-01-01T00:00:00+00:00')""",
            (f"{lesson_id}-question", lesson_id),
        )
        conn.commit()
        conn.close()

    def seed_activity(self, lesson_id, user_id, session_id):
        conn = db.get_db()
        conn.execute(
            """INSERT INTO lesson_progress
               (session_id, user_id, lesson_id, last_viewed_slide, completed, updated_at)
               VALUES (?, ?, ?, 1, 0, '2026-01-01T00:00:00+00:00')""",
            (session_id, user_id, lesson_id),
        )
        conn.execute(
            """INSERT INTO quiz_attempts
               (session_id, user_id, lesson_id, concept_id, question_id,
                question_text, answer_given, correct_answer, explanation,
                is_correct, submitted_at, attempt_number)
               VALUES (?, ?, ?, 'concept-1', ?, 'Question?', 'True', 'True',
                       'Because.', 1, '2026-01-01T00:00:00+00:00', 1)""",
            (session_id, user_id, lesson_id, f"{lesson_id}-question"),
        )
        conn.execute(
            """INSERT INTO saved_items
               (session_id, user_id, lesson_id, item_id, item_type, content, created_at)
               VALUES (?, ?, ?, 'keyterm:1', 'keyterm', NULL, '2026-01-01T00:00:00+00:00')""",
            (session_id, user_id, lesson_id),
        )
        conn.execute(
            """INSERT INTO student_quiz_batches
               (user_id, lesson_id, concept_id, generation_batch, attempt_number, updated_at)
               VALUES (?, ?, 'concept-1', 1, 1, '2026-01-01T00:00:00+00:00')""",
            (user_id, lesson_id),
        )
        conn.commit()
        conn.close()

    def count(self, table, where="1 = 1", params=()):
        conn = db.get_db()
        value = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
        conn.close()
        return value

    def test_deleting_teacher_removes_class_and_owned_lesson_dependencies(self):
        teacher_id = self.create_user("teacher@example.com", "teacher")
        student_id = self.create_user("student@example.com", "student")
        self.seed_lesson("teacher-lesson", teacher_id, "teacher-session")
        self.seed_activity("teacher-lesson", student_id, "student-session")

        conn = db.get_db()
        class_id = conn.execute(
            """INSERT INTO classes (teacher_id, name, archived, created_at)
               VALUES (?, 'Math', 0, '2026-01-01T00:00:00+00:00')""",
            (teacher_id,),
        ).lastrowid
        conn.execute(
            """INSERT INTO enrollments (class_id, student_id, joined_at)
               VALUES (?, ?, '2026-01-01T00:00:00+00:00')""",
            (class_id, student_id),
        )
        conn.execute(
            """INSERT INTO join_codes (code, class_id, expires_at, created_at)
               VALUES ('ABC123', ?, '2027-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')""",
            (class_id,),
        )
        conn.execute(
            """INSERT INTO assignments
               (class_id, lesson_id, teacher_id, status, created_at)
               VALUES (?, 'teacher-lesson', ?, 'published', '2026-01-01T00:00:00+00:00')""",
            (class_id, teacher_id),
        )
        conn.execute(
            """INSERT INTO password_resets
               (user_id, token, expires_at, used, created_at)
               VALUES (?, 'teacher-token', '2027-01-01T00:00:00+00:00', 0,
                       '2026-01-01T00:00:00+00:00')""",
            (teacher_id,),
        )
        conn.commit()
        conn.close()

        db.delete_user(teacher_id)

        self.assertEqual(self.count("users", "id = ?", (teacher_id,)), 0)
        self.assertEqual(self.count("users", "id = ?", (student_id,)), 1)
        for table in (
            "classes", "assignments", "enrollments", "join_codes", "password_resets",
            "lessons", "quiz_questions", "quiz_attempts", "lesson_progress", "saved_items",
            "student_quiz_batches",
        ):
            self.assertEqual(self.count(table), 0, table)

    def test_deleting_student_removes_enrollment_but_keeps_teacher_class(self):
        teacher_id = self.create_user("teacher@example.com", "teacher")
        student_id = self.create_user("student@example.com", "student")
        conn = db.get_db()
        class_id = conn.execute(
            """INSERT INTO classes (teacher_id, name, archived, created_at)
               VALUES (?, 'Math', 0, '2026-01-01T00:00:00+00:00')""",
            (teacher_id,),
        ).lastrowid
        conn.execute(
            """INSERT INTO enrollments (class_id, student_id, joined_at)
               VALUES (?, ?, '2026-01-01T00:00:00+00:00')""",
            (class_id, student_id),
        )
        conn.commit()
        conn.close()

        db.delete_user(student_id)

        self.assertEqual(self.count("users", "id = ?", (student_id,)), 0)
        self.assertEqual(self.count("users", "id = ?", (teacher_id,)), 1)
        self.assertEqual(self.count("classes", "id = ?", (class_id,)), 1)
        self.assertEqual(self.count("enrollments"), 0)

    def test_failed_deletion_rolls_back_and_releases_database_lock(self):
        user_id = self.create_user("blocked@example.com", "student")
        self.seed_lesson("blocked-lesson", user_id, "blocked-session")
        conn = db.get_db()
        conn.execute("CREATE TABLE deletion_blocker (user_id INTEGER REFERENCES users(id))")
        conn.execute("INSERT INTO deletion_blocker (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            db.delete_user(user_id)

        self.assertEqual(self.count("users", "id = ?", (user_id,)), 1)
        self.assertEqual(self.count("lessons", "lesson_id = 'blocked-lesson'"), 1)

        # A second connection can write immediately; the failed transaction
        # did not leak a lock.
        other_id = self.create_user("other@example.com", "student")
        self.assertEqual(self.count("users", "id = ?", (other_id,)), 1)

    def test_missing_session_user_is_treated_as_logged_out(self):
        with server.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["session_id"] = "stale-session"
                browser_session["user_id"] = 999

            response = client.get("/api/auth/me")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"logged_in": False})
            with client.session_transaction() as browser_session:
                self.assertNotIn("user_id", browser_session)

    def test_deleted_account_credentials_return_unauthorized(self):
        password = "correct-password"
        user_id = db.create_user(
            "delete-me@example.com",
            generate_password_hash(password),
            "student",
            "Delete",
            "Me",
        )

        with server.app.test_client() as client:
            with client.session_transaction() as browser_session:
                browser_session["session_id"] = "delete-session"
                browser_session["user_id"] = user_id

            delete_response = client.post("/api/auth/delete-account")
            login_response = client.post(
                "/api/auth/login",
                json={"email": "delete-me@example.com", "password": password},
            )

        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(login_response.status_code, 401)
        self.assertEqual(login_response.get_json(), {"error": "Invalid credentials"})


if __name__ == "__main__":
    unittest.main()
