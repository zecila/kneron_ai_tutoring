import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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


def quiz_question(question_id="lesson-1_c001_b0_q000", concept_id="c001"):
    return {
        "question_id": question_id,
        "lesson_id": "lesson-1",
        "concept_id": concept_id,
        "question_text": "Question?",
        "answer": "A",
        "explanation": "Explanation.",
    }


class QuizSubmissionRegenerationTest(unittest.TestCase):
    def setUp(self):
        server.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

    def submit(self, access, review=False, max_attempts=None, attempts_used=0):
        question = quiz_question()
        with patch.object(server, "current_identity", return_value=(7, "browser-7")), \
             patch.object(server, "resolve_lesson_access", return_value=access), \
             patch.object(server, "get_quiz_question", return_value=question), \
             patch.object(server, "get_assignment", return_value={"max_attempts": max_attempts}), \
             patch.object(server, "get_attempt_count", return_value=attempts_used), \
             patch.object(server, "record_quiz_attempt") as record_attempt, \
             patch.object(server, "_start_quiz_regeneration") as start_regeneration:
            response = server.app.test_client().post(
                "/api/lessons/lesson-1/quiz-attempt-batch",
                json={
                    "attempts": [{"question_id": question["question_id"], "answer_given": "A"}],
                    "review": review,
                },
            )
        return response, record_attempt, start_regeneration

    def test_owner_submission_starts_background_regeneration_without_history(self):
        response, record_attempt, start_regeneration = self.submit({"role": "owner"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["regenerating_concept_ids"], ["c001"])
        record_attempt.assert_not_called()
        start_regeneration.assert_called_once_with("lesson-1", "c001")

    def test_student_submission_records_attempt_and_starts_regeneration(self):
        response, record_attempt, start_regeneration = self.submit(
            {"role": "student", "assignment_id": 12},
            max_attempts=2,
            attempts_used=0,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["regenerating_concept_ids"], ["c001"])
        record_attempt.assert_called_once()
        start_regeneration.assert_called_once_with(
            "lesson-1", "c001", user_id=7, attempt_number=1,
        )

    def test_exhausted_student_submission_does_not_regenerate(self):
        response, record_attempt, start_regeneration = self.submit(
            {"role": "student", "assignment_id": 12},
            max_attempts=1,
            attempts_used=1,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["exhausted_concept_ids"], ["c001"])
        self.assertEqual(response.get_json()["regenerating_concept_ids"], [])
        record_attempt.assert_not_called()
        start_regeneration.assert_not_called()

    def test_student_fetches_only_their_active_quiz_batch(self):
        with patch.object(server, "current_identity", return_value=(7, "browser-7")), \
             patch.object(
                 server,
                 "resolve_lesson_access",
                 return_value={"role": "student", "assignment_id": 12},
             ), \
             patch.object(server, "get_assignment", return_value={"max_attempts": 5}), \
             patch.object(server, "get_attempt_count", return_value=2), \
             patch.object(server, "get_active_quiz_questions", return_value=[]) as get_questions:
            response = server.app.test_client().get(
                "/api/lessons/lesson-1/concepts/c001/quiz",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["attempts_used"], 2)
        get_questions.assert_called_once_with("lesson-1", "c001", user_id=7)

    def test_owner_fetches_the_canonical_quiz_batch(self):
        with patch.object(server, "current_identity", return_value=(3, "browser-3")), \
             patch.object(server, "resolve_lesson_access", return_value={"role": "owner"}), \
             patch.object(server, "get_active_quiz_questions", return_value=[]) as get_questions:
            response = server.app.test_client().get(
                "/api/lessons/lesson-1/concepts/c001/quiz",
            )

        self.assertEqual(response.status_code, 200)
        get_questions.assert_called_once_with("lesson-1", "c001", user_id=None)

    def test_saved_review_does_not_regenerate(self):
        response, record_attempt, start_regeneration = self.submit({"role": "owner"}, review=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["regenerating_concept_ids"], [])
        record_attempt.assert_not_called()
        start_regeneration.assert_not_called()


class QuizBatchReplacementTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp_dir.name) / "app.db")
        db.init_db()
        self.old_questions = [{
            "question": "Old question?",
            "type": "multiple_choice",
            "choices": ["A", "B"],
            "answer": "A",
            "explanation": "Old explanation.",
        }]
        db.insert_quiz_questions("lesson-1", "c001", self.old_questions, generation_batch=0)

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def rows(self):
        conn = db.get_db()
        rows = conn.execute(
            """SELECT question_text, generation_batch, active
               FROM quiz_questions ORDER BY generation_batch"""
        ).fetchall()
        conn.close()
        return [tuple(row) for row in rows]

    def test_replacement_atomically_activates_only_the_new_batch(self):
        new_questions = [{
            "question": "New question?",
            "type": "true_false",
            "choices": ["True", "False"],
            "answer": "True",
            "explanation": "New explanation.",
        }]

        batch = db.replace_active_quiz_questions("lesson-1", "c001", new_questions)

        self.assertEqual(batch, 1)
        self.assertEqual(self.rows(), [
            ("Old question?", 0, 0),
            ("New question?", 1, 1),
        ])

    def test_invalid_replacement_rolls_back_to_the_old_active_batch(self):
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            db.replace_active_quiz_questions(
                "lesson-1",
                "c001",
                [{"question": "Incomplete question"}],
            )

        self.assertEqual(self.rows(), [("Old question?", 0, 1)])
        conn = db.get_db()
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        conn.close()

    def test_replacement_rejects_an_answer_that_is_not_a_choice(self):
        with self.assertRaisesRegex(ValueError, "not among choices"):
            db.replace_active_quiz_questions(
                "lesson-1",
                "c001",
                [{
                    "question": "How many days are in 48 hours?",
                    "type": "multiple_choice",
                    "choices": ["48 days", "24 days", "12 days", "3 days"],
                    "answer": "2 days",
                    "explanation": "There are 24 hours in one day, so 48 hours equals 2 days.",
                }],
            )

        self.assertEqual(self.rows(), [("Old question?", 0, 1)])


class StudentQuizIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp_dir.name) / "app.db")
        db.init_db()
        self.student_one = db.create_user("one@example.com", "hash")
        self.student_two = db.create_user("two@example.com", "hash")
        db.insert_quiz_questions(
            "lesson-1",
            "c001",
            [{
                "question": "Canonical question?",
                "type": "multiple_choice",
                "choices": ["A", "B"],
                "answer": "A",
                "explanation": "Canonical explanation.",
            }],
            generation_batch=0,
        )

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def generated_question(text):
        return [{
            "question": text,
            "type": "multiple_choice",
            "choices": ["A", "B"],
            "answer": "B",
            "explanation": "Generated explanation.",
        }]

    def question_texts(self, user_id):
        return [
            question["question_text"]
            for question in db.get_active_quiz_questions(
                "lesson-1", "c001", user_id=user_id,
            )
        ]

    def test_each_student_has_an_independent_active_retry_batch(self):
        db.replace_active_quiz_questions(
            "lesson-1", "c001", self.generated_question("Student one retry?"),
            user_id=self.student_one, attempt_number=1,
        )

        self.assertEqual(self.question_texts(self.student_one), ["Student one retry?"])
        self.assertEqual(self.question_texts(self.student_two), ["Canonical question?"])
        self.assertEqual(self.question_texts(None), ["Canonical question?"])

        db.replace_active_quiz_questions(
            "lesson-1", "c001", self.generated_question("Student two retry?"),
            user_id=self.student_two, attempt_number=1,
        )

        self.assertEqual(self.question_texts(self.student_one), ["Student one retry?"])
        self.assertEqual(self.question_texts(self.student_two), ["Student two retry?"])

    def test_older_regeneration_cannot_replace_a_newer_attempt(self):
        db.replace_active_quiz_questions(
            "lesson-1", "c001", self.generated_question("Attempt two?"),
            user_id=self.student_one, attempt_number=2,
        )
        db.replace_active_quiz_questions(
            "lesson-1", "c001", self.generated_question("Late attempt one?"),
            user_id=self.student_one, attempt_number=1,
        )

        self.assertEqual(self.question_texts(self.student_one), ["Attempt two?"])

    def test_attempt_numbers_are_counted_per_student(self):
        question = db.get_active_quiz_questions("lesson-1", "c001")[0]
        for attempt_number in (1, 2):
            db.record_quiz_attempt(
                "browser-one", "lesson-1", "c001", question["question_id"],
                question["question_text"], "A", "A", True, attempt_number,
                user_id=self.student_one,
            )
        db.record_quiz_attempt(
            "browser-two", "lesson-1", "c001", question["question_id"],
            question["question_text"], "B", "A", False, 1,
            user_id=self.student_two,
        )

        self.assertEqual(
            db.get_attempt_count(self.student_one, "browser-one", "lesson-1", "c001"),
            2,
        )
        self.assertEqual(
            db.get_attempt_count(self.student_two, "browser-two", "lesson-1", "c001"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
