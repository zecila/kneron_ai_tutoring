import json
import os
import random
import time

from openai import OpenAI, RateLimitError


api_key = os.environ["OPENAI_API_KEY"]

client = OpenAI(
    api_key=api_key,
    base_url=os.environ["LLM_BASE_URL"],
    default_headers={"x-goog-api-key": api_key},
)


quiz_json_schema = {
    "name": "quiz_batch",
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
                        "type": {
                            "type": "string",
                            "enum": ["multiple_choice", "true_false"],
                        },
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "answer": {"type": "string"},
                        "explanation": {"type": "string"},
                    },
                    "required": [
                        "question",
                        "type",
                        "choices",
                        "answer",
                        "explanation",
                    ],
                    "additionalProperties": False,
                },
                "minItems": 6,
                "maxItems": 10,
            }
        },
        "required": ["quiz_questions"],
        "additionalProperties": False,
    },
}


quiz_system_prompt = """
You are generating quiz questions for a single concept in a student's lesson.

Generate 6-10 quiz questions (mix of multiple_choice and true_false). Prefer 6 strong,
distinct questions when the supplied material is narrow; never pad the batch by
repeating a fact or inventing content.
For multiple_choice, always provide 4 choices and indicate the correct answer.

Grounding and difficulty:
- Use only the supplied concept, description, and study material. A correct answer
  must be directly supported by them or follow from a necessary calculation using
  facts they provide. Do not fill gaps with outside facts, even if those facts are
  generally true.
- Match the vocabulary and reasoning depth of the supplied material. Do not require
  more advanced domain knowledge than the lesson provides.
- Make questions self-contained. Include any passage, scenario, data, assumptions,
  conventions, or code behavior needed to answer. The student will not see the study
  material while answering, so never write "according to the study material" or refer
  to an unnamed passage, diagram, lecture, source, example, or definition.

Correct-answer rules:
- Every question must have exactly one defensible answer under its stated context.
- Write a precise stem that identifies the requested relationship, property, time
  period, actor, criterion, representation, or output. Avoid vague "best", "main",
  "common", or "typically" questions unless an explicit context and criterion make
  one answer uniquely best.
- Never ask an unscoped question such as "Which statement is correct?" or "Which
  conversion is correct?" A distractor that is a true statement about another part of
  the topic would also answer it. Name the exact relationship or category being tested,
  and ensure every distractor is false for that scoped question.
- Keep choices mutually exclusive. Never include synonyms, equivalent values or
  expressions, overlapping categories, or both a general answer and a specific case
  when more than one would satisfy the stem.
- Keep choices at the same level of abstraction and specificity. Do not use a broader
  container, category, time span, or cause as a distractor for its correct subpart, or
  vice versa. For a "where" question, all choices must name locations at the same
  spatial level; for "when", "who", and "why" questions, use the same temporal, role,
  or causal level.
- Before returning, solve each question independently. The answer field must copy the
  sole correct choice exactly, character for character. The explanation must justify
  that exact choice using the facts or reasoning present in the question and supplied
  material. For calculations, recompute the result and check the units.

Distractor rules:
- Every distractor must be plausible for a learner at this lesson's level but clearly
  incorrect for the exact stem. Base distractors on a likely misconception, reversed
  relationship, confused term, incorrect step, or nearby value supported by the same
  topic. Do not invent unrelated facts merely to create choices.
- All four choices must answer the same kind of question and be parallel in meaning,
  specificity, grammar, and format. Do not reveal the answer through length, detail,
  qualifiers, or different units. Do not use "all of the above" or "none of the above".
- Use coordinate alternatives rather than an assortment of unrelated terms. For
  example, four locations should all be comparable locations, four causes should all
  be candidate causes, and four process outcomes should all describe outcomes.
- Substitute every distractor into the stem. Replace it if it could be correct under a
  reasonable interpretation, is partly correct, is equivalent to the answer, depends
  on an unstated convention, or does not actually answer what the stem asks.

Apply the rules below only when that question type is relevant:
- Definitions and classifications: choices must be competing definitions or members
  at the same category level. Do not use incidental facts that are also true.
- Quantitative, symbolic, and scientific questions: state required assumptions and
  precision. Reject alternate forms that are mathematically or scientifically
  equivalent to the answer. For a requested quantity or conversion, keep the target
  quantity, unit, and representation consistent across all choices and vary only the
  candidate result.
- Measurements: never make two units for the same physical quantity compete under a
  broad stem. For example, milliliters and cubic centimeters can both measure volume.
  For geometric volume, name the solid or request a cubic unit and do not offer a
  liter-based unit; for container capacity, name the container and do not offer a
  cubic unit. Apply the same rule to alternate units of length, mass, time, and other
  quantities.
- Nested structures and locations: when the answer is a specific sub-location, name
  its containing structure in the stem and use peer sub-locations as choices. Do not
  offer the containing structure as a distractor because both locations can be true at
  different levels of specificity. For example, if a process occurs in thylakoid
  membranes inside a chloroplast, ask which structure inside the chloroplast; never
  offer both "thylakoid membranes" and "chloroplast" as choices.
- Procedures, sequences, and code: state the initial conditions and relevant runtime,
  language, or convention when needed. Each distractor should differ in the decisive
  step or outcome, not in unrelated details.
- History, civics, and causal claims: scope the stem to the supplied time, place,
  person, or account. Keep causes, triggers, effects, and later consequences distinct.
- Arguments, literature, and other interpretive material: include the relevant claim,
  excerpt, or scenario directly in each stem and name the evaluation criterion. Never
  assume context from another quiz question. The answer may be the best-supported
  interpretation only when the supplied evidence makes it uniquely strongest;
  distractors must be contradicted or unsupported by that evidence.
- Language questions: provide enough sentence context to determine grammar and
  meaning, and specify dialect, register, or convention when it could change the answer.

For true_false questions, choices must be exactly ["True", "False"]. Use one focused
claim that is objectively decidable from the supplied material. Avoid compound claims,
subjective judgments, trick wording, and vague frequency qualifiers. A false statement
should alter one meaningful fact, relationship, condition, or step.

Assign one primary learning point to each question before writing it. No two questions
may test the same fact, inference, example, calculation, or passage evidence by merely
switching format or wording. Cover distinct parts or meaningfully different
applications of the concept.

Skip a possible multiple-choice question when you cannot write three coordinate,
plausible, and definitely false distractors for it. Test a different supported fact or
use a focused true_false statement instead of filling its choices with parent
categories, synonyms, unrelated semantic types, or other true facts.

All questions and explanations should read as self-contained lesson material. State
the supporting fact directly; never mention "the study material", "the source", "the
lesson", or say things like "as shown" or "the definition given".

Return ONLY valid JSON, no preamble, no markdown.
""".strip()


def _validate_questions(questions):
    if not isinstance(questions, list) or not 6 <= len(questions) <= 10:
        count = len(questions) if isinstance(questions, list) else "invalid data"
        raise ValueError(f"Expected 6-10 quiz questions, got {count}")

    question_types = set()
    for question in questions:
        question_type = question.get("type")
        if question_type not in ("multiple_choice", "true_false"):
            raise ValueError(f"Unknown quiz question type: {question_type!r}")
        question_types.add(question_type)
        if not str(question.get("question", "")).strip():
            raise ValueError("Quiz question text must not be empty")
        if not str(question.get("explanation", "")).strip():
            raise ValueError("Quiz explanation must not be empty")

        choices = question.get("choices")
        expected_count = 4 if question_type == "multiple_choice" else 2
        if not isinstance(choices, list) or len(choices) != expected_count:
            raise ValueError(f"Bad choices count for {question_type}: {choices}")
        if any(not isinstance(choice, str) or not choice.strip() for choice in choices):
            raise ValueError(f"Quiz choices must be non-empty strings: {choices}")
        if len({choice.strip().casefold() for choice in choices}) != len(choices):
            raise ValueError(f"Quiz choices must be distinct: {choices}")
        if question.get("answer") not in choices:
            raise ValueError(
                f"Answer not among choices: {question.get('answer')!r} not in {choices}"
            )
        if question_type == "true_false" and choices != ["True", "False"]:
            raise ValueError(
                f'true_false choices must be ["True", "False"], got {choices}'
            )

    if question_types != {"multiple_choice", "true_false"}:
        raise ValueError("Quiz batch must mix multiple_choice and true_false questions")


def generate_quiz_batch(concept_name: str, concept_description: str,
                        study_material: dict, lesson_id: str, concept_id: str) -> list[dict]:
    user_prompt = f"""
Concept: {concept_name}
Description: {concept_description}
Study material: {json.dumps(study_material, ensure_ascii=False)}

Generate the quiz questions for this concept. Use only facts supported by the study
material. Do not invent missing relationships, conversions, or conventions.

Silently audit the finished batch before returning it:
1. Point to the supplied support or necessary calculation for each correct answer.
2. Confirm no other choice is correct, equivalent, partly correct, broader or narrower
   than the answer, or true under another reasonable reading of the stem.
3. Confirm every distractor answers the stem and fails for a specific reason.
4. Confirm the answer and explanation agree exactly.
5. For each multiple-choice item, evaluate the four choices independently against the
   stem. The result must be exactly one true and three false; a synonym, containing
   category or location, equivalent form, or true fact from a different context counts
   as another true choice and must be replaced.
6. Confirm all four choices have the requested semantic type and answer format. If the
   stem asks for a value in a unit, a location within a structure, a cause, a person, a
   definition, or an outcome, every choice must be that same kind of value.
7. Remove references to the study material, lesson, lecture, source, or an unnamed
   passage from both questions and explanations.
8. Reject any location or hierarchy item whose choices contain both a specific answer
   and something that contains or includes it. "Thylakoid membranes" and
   "chloroplast" cannot compete for where a reaction occurs because both can be true.
   Apply the same rejection to room/building, city/country, event/era, and
   subclass/class relationships.
9. Rewrite or replace any item that fails a check; do not output the audit.
""".strip()

    last_validation_error = None
    for generation_attempt in range(2):
        prompt = user_prompt
        if last_validation_error is not None:
            prompt += (
                "\n\nThe previous batch was rejected because "
                f"{last_validation_error}. Generate a corrected replacement batch."
            )

        for backoff_attempt in range(4):
            try:
                response = client.chat.completions.create(
                    model="gpt-5.4-mini",
                    response_format={
                        "type": "json_schema",
                        "json_schema": quiz_json_schema,
                    },
                    messages=[
                        {"role": "system", "content": quiz_system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    max_tokens=3000,
                )
                break
            except RateLimitError:
                time.sleep(2 ** backoff_attempt)
        else:
            raise RuntimeError(
                "quiz generation exceeded retries after repeated rate limiting"
            )

        questions = json.loads(response.choices[0].message.content)["quiz_questions"]
        try:
            _validate_questions(questions)
        except ValueError as error:
            last_validation_error = error
            continue

        for question in questions:
            if question["type"] == "multiple_choice":
                random.shuffle(question["choices"])
        return questions

    raise ValueError("Quiz generation returned two invalid batches") from last_validation_error
