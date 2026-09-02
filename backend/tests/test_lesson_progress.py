import tempfile
import unittest
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import db


class LessonProgressTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp_dir.name) / "app.db")
        db.init_db()
        self.user_id = db.create_user("student@example.com", "hash")

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def progress_rows(self, where, params):
        conn = db.get_db()
        rows = conn.execute(
            f"SELECT * FROM lesson_progress WHERE {where} ORDER BY updated_at",
            params,
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def test_signed_in_progress_follows_user_across_sessions(self):
        db.update_lesson_progress(
            "browser-a", "lesson-1", user_id=self.user_id,
            last_viewed_slide=2, completed=False,
        )
        db.update_lesson_progress(
            "browser-b", "lesson-1", user_id=self.user_id,
            last_viewed_slide=7, completed=True,
        )

        progress = db.get_lesson_progress(
            user_id=self.user_id, session_id="browser-b", lesson_id="lesson-1",
        )
        rows = self.progress_rows(
            "user_id = ? AND lesson_id = ?", (self.user_id, "lesson-1"),
        )

        self.assertEqual(progress["last_viewed_slide"], 7)
        self.assertEqual(progress["completed"], 1)
        self.assertEqual(len(rows), 1)

    def test_anonymous_progress_remains_scoped_to_browser_session(self):
        db.update_lesson_progress("browser-a", "lesson-1", last_viewed_slide=1)
        db.update_lesson_progress("browser-b", "lesson-1", last_viewed_slide=4)

        first = db.get_lesson_progress(session_id="browser-a", lesson_id="lesson-1")
        second = db.get_lesson_progress(session_id="browser-b", lesson_id="lesson-1")

        self.assertEqual(first["last_viewed_slide"], 1)
        self.assertEqual(second["last_viewed_slide"], 4)

    def test_init_db_keeps_newest_legacy_duplicate(self):
        conn = db.get_db()
        conn.execute("DROP INDEX idx_lesson_progress_user_lesson")
        conn.execute(
            """INSERT INTO lesson_progress
               (session_id, user_id, lesson_id, last_viewed_slide, completed, updated_at)
               VALUES ('old-browser', ?, 'lesson-1', 2, 0, '2026-01-01T00:00:00+00:00')""",
            (self.user_id,),
        )
        conn.execute(
            """INSERT INTO lesson_progress
               (session_id, user_id, lesson_id, last_viewed_slide, completed, updated_at)
               VALUES ('new-browser', ?, 'lesson-1', 8, 1, '2026-02-01T00:00:00+00:00')""",
            (self.user_id,),
        )
        conn.commit()
        conn.close()

        db.init_db()

        rows = self.progress_rows(
            "user_id = ? AND lesson_id = ?", (self.user_id, "lesson-1"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_viewed_slide"], 8)

    def test_claim_session_merges_newer_anonymous_progress(self):
        conn = db.get_db()
        conn.execute(
            """INSERT INTO lesson_progress
               (session_id, user_id, lesson_id, last_viewed_slide, completed, updated_at)
               VALUES ('account-browser', ?, 'lesson-1', 2, 0,
                       '2026-01-01T00:00:00+00:00')""",
            (self.user_id,),
        )
        conn.execute(
            """INSERT INTO lesson_progress
               (session_id, user_id, lesson_id, last_viewed_slide, completed, updated_at)
               VALUES ('anonymous-browser', NULL, 'lesson-1', 6, 1,
                       '2026-02-01T00:00:00+00:00')"""
        )
        conn.commit()
        conn.close()

        db.claim_session("anonymous-browser", self.user_id)

        progress = db.get_lesson_progress(user_id=self.user_id, lesson_id="lesson-1")
        rows = self.progress_rows("lesson_id = ?", ("lesson-1",))
        self.assertEqual(progress["last_viewed_slide"], 6)
        self.assertEqual(progress["completed"], 1)
        self.assertEqual(len(rows), 1)

    def test_claim_session_does_not_replace_newer_account_progress(self):
        conn = db.get_db()
        conn.execute(
            """INSERT INTO lesson_progress
               (session_id, user_id, lesson_id, last_viewed_slide, completed, updated_at)
               VALUES ('account-browser', ?, 'lesson-1', 9, 1,
                       '2026-02-01T00:00:00+00:00')""",
            (self.user_id,),
        )
        conn.execute(
            """INSERT INTO lesson_progress
               (session_id, user_id, lesson_id, last_viewed_slide, completed, updated_at)
               VALUES ('anonymous-browser', NULL, 'lesson-1', 3, 0,
                       '2026-01-01T00:00:00+00:00')"""
        )
        conn.commit()
        conn.close()

        db.claim_session("anonymous-browser", self.user_id)

        progress = db.get_lesson_progress(user_id=self.user_id, lesson_id="lesson-1")
        rows = self.progress_rows("lesson_id = ?", ("lesson-1",))
        self.assertEqual(progress["last_viewed_slide"], 9)
        self.assertEqual(progress["completed"], 1)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
