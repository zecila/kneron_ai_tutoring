import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://llm.test/v1")

from pipeline import quiz_generation


def completion(questions):
    message = SimpleNamespace(content=json.dumps({"quiz_questions": questions}))
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def multiple_choice(index, answer=None, choices=None):
    choices = choices or ["Choice 1", "Choice 2", "Choice 3", "Choice 4"]
    answer = answer or choices[0]
    return {
        "question": f"Multiple choice question {index}?",
        "type": "multiple_choice",
        "choices": choices,
        "answer": answer,
        "explanation": f"{answer} is correct.",
    }


def true_false(index, answer="True"):
    return {
        "question": f"True or false question {index}?",
        "type": "true_false",
        "choices": ["True", "False"],
        "answer": answer,
        "explanation": f"The statement is {answer.lower()}.",
    }


class QuizGenerationQualityTest(unittest.TestCase):
    def test_generation_uses_one_mixed_question_request(self):
        generated = [
            multiple_choice(1),
            multiple_choice(2, answer="Choice 2"),
            multiple_choice(3, answer="Choice 3"),
            multiple_choice(4, answer="Choice 4"),
            true_false(1),
            true_false(2, answer="False"),
        ]

        with patch.object(
            quiz_generation.client.chat.completions,
            "create",
            return_value=completion(generated),
        ) as create, patch.object(quiz_generation.random, "shuffle") as shuffle:
            questions = quiz_generation.generate_quiz_batch(
                "Time and money",
                "Convert common units of time and money.",
                {
                    "key_terms": ["day", "hour", "cent", "dollar"],
                    "formulas": [{"latex": "1 day = 24 hours"}],
                    "flashcards": [],
                },
                "lesson-1",
                "c001",
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(questions, generated)
        self.assertEqual(shuffle.call_count, 4)
        request = create.call_args.kwargs
        self.assertEqual(
            request["response_format"]["json_schema"],
            quiz_generation.quiz_json_schema,
        )
        self.assertEqual(request["max_tokens"], 3000)
        self.assertIn("exactly one defensible answer", request["messages"][0]["content"])
        self.assertIn(
            "Use only the supplied concept, description, and study material",
            request["messages"][0]["content"],
        )
        self.assertIn(
            "Quantitative, symbolic, and scientific questions",
            request["messages"][0]["content"],
        )
        self.assertIn(
            "Arguments, literature, and other interpretive material",
            request["messages"][0]["content"],
        )
        self.assertIn(
            "same level of abstraction and specificity",
            request["messages"][0]["content"],
        )
        self.assertIn(
            'Never ask an unscoped question such as "Which statement is correct?"',
            request["messages"][0]["content"],
        )
        self.assertIn(
            "milliliters and cubic centimeters can both measure volume",
            request["messages"][0]["content"],
        )
        self.assertIn(
            "Nested structures and locations",
            request["messages"][0]["content"],
        )
        self.assertIn(
            'offer both "thylakoid membranes" and "chloroplast" as choices',
            request["messages"][0]["content"],
        )
        self.assertIn(
            "Skip a possible multiple-choice question",
            request["messages"][0]["content"],
        )
        self.assertIn(
            "The student will not see the study",
            request["messages"][0]["content"],
        )
        self.assertIn(
            "Assign one primary learning point to each question",
            request["messages"][0]["content"],
        )
        self.assertIn(
            "Language questions",
            request["messages"][0]["content"],
        )
        self.assertIn("1 day = 24 hours", request["messages"][1]["content"])
        self.assertIn(
            "Silently audit the finished batch",
            request["messages"][1]["content"],
        )
        self.assertIn(
            "Confirm every distractor answers the stem",
            request["messages"][1]["content"],
        )
        self.assertIn(
            "exactly one true and three false",
            request["messages"][1]["content"],
        )
        self.assertIn(
            "all four choices have the requested semantic type",
            request["messages"][1]["content"],
        )
        self.assertIn(
            '"Thylakoid membranes" and',
            request["messages"][1]["content"],
        )
        self.assertIn(
            "room/building, city/country, event/era",
            request["messages"][1]["content"],
        )

    def test_validation_rejects_duplicate_choices(self):
        questions = [
            multiple_choice(
                1,
                choices=["Equivalent", "equivalent", "Other 1", "Other 2"],
            ),
            multiple_choice(2),
            multiple_choice(3),
            multiple_choice(4),
            true_false(1),
            true_false(2),
        ]

        with self.assertRaisesRegex(ValueError, "distinct"):
            quiz_generation._validate_questions(questions)

    def test_validation_rejects_answer_missing_from_choices(self):
        questions = [
            multiple_choice(1, answer="Missing answer"),
            multiple_choice(2),
            multiple_choice(3),
            multiple_choice(4),
            true_false(1),
            true_false(2),
        ]

        with self.assertRaisesRegex(ValueError, "Answer not among choices"):
            quiz_generation._validate_questions(questions)

    def test_generation_retries_one_structurally_invalid_batch(self):
        valid = [
            multiple_choice(1),
            multiple_choice(2),
            multiple_choice(3),
            multiple_choice(4),
            true_false(1),
            true_false(2),
        ]
        invalid = [{**valid[0], "answer": "A"}, *valid[1:]]

        with patch.object(
            quiz_generation.client.chat.completions,
            "create",
            side_effect=[completion(invalid), completion(valid)],
        ) as create, patch.object(quiz_generation.random, "shuffle"):
            result = quiz_generation.generate_quiz_batch(
                "Measurement",
                "Measurement facts.",
                {"key_terms": [], "formulas": [], "flashcards": []},
                "lesson-1",
                "c001",
            )

        self.assertEqual(result, valid)
        self.assertEqual(create.call_count, 2)
        self.assertIn(
            "previous batch was rejected",
            create.call_args_list[1].kwargs["messages"][1]["content"],
        )

    def test_validation_requires_canonical_true_false_choices(self):
        questions = [
            multiple_choice(1),
            multiple_choice(2),
            multiple_choice(3),
            multiple_choice(4),
            true_false(1),
            {
                **true_false(2),
                "choices": ["False", "True"],
            },
        ]

        with self.assertRaisesRegex(ValueError, "true_false choices"):
            quiz_generation._validate_questions(questions)


if __name__ == "__main__":
    unittest.main()
