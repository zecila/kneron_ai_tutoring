import os
import json
import re
import time
import random
from openai import OpenAI, RateLimitError

api_key = os.environ["OPENAI_API_KEY"]

client = OpenAI(
	api_key = api_key,
	base_url = os.environ["LLM_BASE_URL"],
	default_headers={
		"x-goog-api-key": api_key
	}
)

mc_json_schema = {
    "name": "mc_quiz_batch",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "quiz_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "type": {"type": "string", "enum": ["multiple_choice"]},
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 4,
                            "maxItems": 4
                        },
                        "answer_index": {"type": "integer", "minimum": 0, "maximum": 3},
                        "explanation": {"type": "string"}
                    },
                    "required": ["question", "type", "choices", "answer_index", "explanation"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["quiz_questions"],
        "additionalProperties": False
    }
}

tf_json_schema = {
    "name": "tf_quiz_batch",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "quiz_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "type": {"type": "string", "enum": ["true_false"]},
                        "choices": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["True", "False"]},
                            "minItems": 2,
                            "maxItems": 2
                        },
                        "answer_index": {"type": "integer", "minimum": 0, "maximum": 1},
                        "explanation": {"type": "string"}
                    },
                    "required": ["question", "type", "choices", "answer_index", "explanation"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["quiz_questions"],
        "additionalProperties": False
    }
}

quiz_system_prompt = """
You are generating quiz questions for a single concept in a student's lesson.

Generate 6-10 quiz questions (mix of multiple_choice, true_false).
For multiple_choice, always provide 4 choices and indicate the correct answer.

For multiple choice questions, all four choices must be plausible. Distractors should
reflect common misconceptions or superficially similar ideas — not obviously wrong
answers. All choices should be similar in length, specificity, and grammatical form so
the correct answer cannot be identified by structure alone.

For true_false questions, choices must be exactly ["True", "False"].

The "answer" field must be the exact text of one of the "choices" strings,
character for character. Never return a letter label like "A" or "B" —
always copy the full choice text.

All questions should read as self-contained lesson material. Do not reference the
source document or say things like "as shown" or "in this lesson".

Return ONLY valid JSON, no preamble, no markdown.
""".strip()

def generate_quiz_batch(concept_name: str, concept_description: str,
                         key_terms: list, lesson_id: str, concept_id: str) -> list[dict]:
    user_prompt = f"""
Concept: {concept_name}
Description: {concept_description}
Key terms: {json.dumps(key_terms, ensure_ascii=False)}

Generate the quiz questions for this concept.
""".strip()

    def _call(schema, count_hint):
        prompt = user_prompt + f"\n\nGenerate {count_hint} questions."
        for backoff_attempt in range(4):
            try:
                response = client.chat.completions.create(
                    model="gpt-5.4-mini",
                    response_format={"type": "json_schema", "json_schema": schema},
                    messages=[
                        {"role": "system", "content": quiz_system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0,
                    max_tokens=1800
                )
                return json.loads(response.choices[0].message.content)["quiz_questions"]
            except RateLimitError:
                time.sleep(2 ** backoff_attempt)
        raise RuntimeError("quiz generation exceeded retries after repeated rate limiting")

    questions = _call(mc_json_schema, "4-6 multiple_choice") + _call(tf_json_schema, "2-4 true_false")
    
    for q in questions:
        expected = 4 if q["type"] == "multiple_choice" else 2
        if len(q["choices"]) != expected:
            raise ValueError(f"Bad choices count for {q['type']}: {q['choices']}")
        if q["type"] == "true_false" and set(q["choices"]) != {"True", "False"}:
            raise ValueError(f"true_false choices must be ['True','False'], got {q['choices']}")
        q["answer"] = q["choices"][q.pop("answer_index")]
        if q["type"] == "multiple_choice":
            random.shuffle(q["choices"])
    return questions
