import os
import json
import re
import time
from openai import OpenAI, RateLimitError
from lesson_paths import lesson_path

# ── JSON repair helpers ─────────────────────────────────────────

def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()

def attempt_repair(raw: str) -> str:
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    raw = raw.replace("\u201c", '"').replace("\u201d", '"')
    raw = raw.replace("\u2018", "'").replace("\u2019", "'")
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)
    return raw

def close_truncated_json(raw: str) -> str:
    stack = []
    in_str = False
    esc = False
    for ch in raw:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    if stack:
        return raw + "".join(reversed(stack))
    return raw

def find_error_context(raw: str, char_pos: int, window: int = 120) -> str:
    start = max(0, char_pos - window)
    end   = min(len(raw), char_pos + window)
    marker = " " * (char_pos - start) + "^"
    return f"...{raw[start:end]}...\n    {marker}"

def parse_with_repair(raw: str, label: str = "", was_truncated: bool = False) -> dict | None:
    raw = strip_fences(raw)
    if was_truncated:
        raw = close_truncated_json(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[{label}] Initial parse failed: {e}")
        print(f"  Context:\n    {find_error_context(raw, e.pos)}")
    repaired = attempt_repair(raw)
    try:
        result = json.loads(repaired)
        print(f"[{label}] Repair succeeded.")
        return result
    except json.JSONDecodeError as e2:
        print(f"[{label}] Repair also failed: {e2}")
        print(f"  Context:\n    {find_error_context(repaired, e2.pos)}")
        return None
    
# ──────────────────────────────────────────────────────────────────────


api_key = os.environ["OPENAI_API_KEY"]

client = OpenAI(
	api_key = api_key,
	base_url = os.environ["LLM_BASE_URL"],
	default_headers={
		"x-goog-api-key": api_key
	}
)

system_prompt = """
You are an expert curriculum designer and knowledge graph builder.
You will be given extracted text from a lecture slide deck in JSON format. 
Your task is to analyze the content and produce a structured curriculum graph. 

---

## BEFORE EXTRACTING CONCEPTS

### Text quality
Each element has a `quality.text_confidence` score (0.0–1.0) and each page has a
`quality.reading_order_confidence` score (0.0–1.0).

- `text_confidence` < 0.60: the element text is likely garbled. Attempt to correct it
  using surrounding elements on the same page as context before using it.
- `reading_order_confidence` < 0.80: text lines on this page may be interleaved or
  mis-sequenced. Read adjacent elements together and reassemble into logical units
  before interpreting them.
- All other elements: use as-is.

Elements with `ocr_source: true` were produced by optical character recognition and
are likely to contain character-level errors (e.g. "rn" read as "m", "1" as "l",
broken spacing). Clean and restructure these lines using surrounding context before
treating them as content. Do not preserve OCR artifacts.

Do not output any repair notes. Fix internally and proceed.

### Equations
For every element with `type: "equation"`:

1. Determine whether it is mathematical. Treat it as a formula if it expresses any
   quantitative relationship, operation, or equality — including unit conversions,
   geometric formulas, and arithmetic rules written in plain language
   (e.g. "Volume = length × width × height" is a formula).
   Only exclude it as a false positive if it contains no relationship at all —
   for example: navigation arrows, symbol legends, variable name lists, or metadata
   with special characters. When in doubt, treat it as a formula.

2. If it is mathematical, render it in valid LaTeX in the `formulas` field. Fix flat
   formatting only (e.g. superscripts, subscripts, fractions). Do not change any
   variable names, symbols, or constants, and do not infer or substitute.

3. Only if `text_confidence` < 0.60: use surrounding elements on the same page to
   understand what the equation represents, and include that context in the
   `explanation` field. Still do not alter the equation itself.

4. If the equation is unrecoverable, omit it entirely. Do not guess.

### Educational relevance
Include only content a student would be expected to learn and apply:
- Skip title slides, table of contents, instructor names, dates, and copyright notices.
- Skip motivating quotes or anecdotes (they may inform a description but should not
  generate flashcards or quiz questions).
- Extract definitions, theorems, derivations, worked examples, and procedures.

Aim for 8–18 concepts total. Merge concepts that share the same core idea rather than
creating a separate concept for every named term or slide.

---

## CURRICULUM GRAPH

You must identify:
- Key concepts taught in the slides
- Relationships between concepts
- Importance of each concept to the overall curriculum

Importance levels:
- core: fundamental concept the rest of the material depends on
- supporting: builds on or elaborates a core concept
- supplementary: additional context, examples, or tangential info

Relationship types:
- requires: this concept must be understood before the target
- extends: this concept builds on the target
- contrasts: this concept is compared against the target
- example_of: this concept is a concrete instance of the target
- part of: this concept is a component of the target

For each concept also generate:
- 1-4 flashcards (front: question or term, back: answer or definition)
- 1-4 quiz questions (mix of multiple_choice, true_false)
- For multiple_choice, always provide 4 choices and indicate the correct answer

For multiple choice questions, all four choices must be plausible. Distractors should
reflect common misconceptions or superficially similar ideas — not obviously wrong
answers. All choices should be similar in length, specificity, and grammatical form so
the correct answer cannot be identified by structure alone.

All generated text — descriptions, explanations, flashcards, and quiz questions —
should read as self-contained lesson material. Do not reference the source document.
Avoid phrases like "as shown in the slide", "in this lecture", "the presenter states",
or any language that positions the content as derived from an external file.

JSON FORMATTING RULES — follow these exactly:
- Every key-value pair within an object, and every element within an array,
  must be separated by a comma — except the last one in that object/array.
- Do not add a trailing comma after the final element of any object or array.
- Every string must use double quotes, with internal double quotes escaped as \".
- Do not truncate output. If the curriculum is large, reduce the number of
  concepts or trim flashcards/quiz_questions per concept rather than cutting
  the JSON off mid-structure — the response must always be complete, valid,
  and fully closed.
- Before returning, mentally verify every opening {{ or [ has a matching
  closing }} or ], and that no two adjacent values are missing a separating comma.

Return ONLY valid JSON matching this exact schema, no preamble, no markdown:
{{
  "curriculum_graph": {{
    "course": "<inferred course name>",
    "concepts": [
      {{
        "concept_id": "c001",
        "name": "<concept name>",
        "description": "<brief description>",
        "importance": "core | supporting | supplementary",
        "importance_score": 0.0,
        "slide_references": [1, 2],
        "relationships": [
          {{
            "target_concept_id": "c002",
            "type": "requires | extends | contrasts | example_of | part_of",
            "description": "<why they are related>"
          }}
        ],
        "study": {{
          "key_terms": [
            {{
              "term": "...",
              "definition": "..."
            }}
          ],
          "formulas": [
            {{
              "latex": "...",
              "explanation": "..."
            }}
          ],
          "flashcards": [
            {{
              "front": "<question or term>",
              "back": "<answer or definition>"
            }}
          ],
          "quiz_questions": [
            {{
              "question": "<question text>",
              "type": "multiple_choice | true_false",
              "choices": ["A", "B", "C", "D"],
              "answer": "<correct answer>",
              "explanation": "<why this is correct>"
            }}
          ]
        }}
      }}
    ]
  }}
}}
""".strip()

def run_curriculum_extraction(normalized_data: dict, lesson_id: str) -> dict:
    document_text = json.dumps(normalized_data, ensure_ascii=False)
    user_prompt = f"""
Here is the extracted text content:
{document_text}
Analyze this content and return the curriculum graph JSON.
""".strip()

    MAX_RETRIES = 1
    MAX_REDOS = 1

    extracted_concepts_json = None
    for redo in range(MAX_REDOS + 1):

      messages = [
          {"role": "system", "content": system_prompt},
          {"role": "user", "content": user_prompt}
      ]
      
      last_raw = ""
      last_finish = ""

      for attempt in range(1, MAX_RETRIES + 2):
          label = f"redo {redo} attempt {attempt}"
          
          if attempt == 1:
              call_messages = messages
          elif last_finish != "length":
              call_messages = messages + [
                  {"role": "assistant", "content": last_raw},
                  {"role": "user", "content": (
                      "Your previous JSON had a syntax error and could not be parsed. "
                      "Please regenerate the complete curriculum graph JSON. "
                      "Return only valid JSON — no markdown, no trailing commas."
                  )},
              ]
          else:
              call_messages = messages  # truncation case reuses last_raw below, skip re-calling

          if not (attempt > 1 and last_finish == "length"):
              for backoff_attempt in range(4):
                  try:
                      response = client.chat.completions.create(
                          model="gpt-5.4-mini",
                          response_format={"type": "json_object"},
                          messages=call_messages,
                          temperature=0,
                          max_tokens=8000
                      )
                      break
                  except RateLimitError:
                      wait = 2 ** backoff_attempt
                      print(f"[{label}] rate limited, retrying in {wait}s")
                      time.sleep(wait)
              else:
                  raise RuntimeError(f"[{label}] exceeded retries after repeated rate limiting")
              last_raw = response.choices[0].message.content
              last_finish = response.choices[0].finish_reason
              if last_finish == "length":
                  print(f"WARNING [{label}]: output truncated (finish_reason=length).")

          was_truncated = last_finish == "length"
          result = parse_with_repair(last_raw, label, was_truncated=was_truncated)

          if result is not None:
              extracted_concepts_json = result
              print(f"Parsed successfully on {label}.")
              break

          if attempt <= MAX_RETRIES:
                print(f"Will retry ({attempt}/{MAX_RETRIES})...")

      if extracted_concepts_json is not None:
          break
      print(f"[redo {redo}] All attempts exhausted." +
          (" Restarting with a fresh conversation..." if redo < MAX_REDOS else " Giving up."))

    out_path = lesson_path(lesson_id, "extracted_concepts.json", create_dir=True)
    if extracted_concepts_json is None:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"error": "failed to parse after retries and redo", "raw": last_raw}, f, indent=2, ensure_ascii=False)
        raise ValueError(f"Curriculum extraction failed to produce valid JSON after retries and redo")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(extracted_concepts_json, f, indent=2, ensure_ascii=False)
    return extracted_concepts_json


if __name__ == "__main__": 
    extracted_file = input("normalized_output.json path: ").strip() or "normalized_output.json"
    lesson_id = input("Lesson id (blank = 'manual-test'): ").strip() or "manual-test"
    with open(extracted_file, "r", encoding="utf-8") as f:
        extracted_data = json.load(f)
    run_curriculum_extraction(extracted_data, lesson_id)
    print(f"Done. Output written to backend/lessons/{lesson_id}/extracted_concepts.json")