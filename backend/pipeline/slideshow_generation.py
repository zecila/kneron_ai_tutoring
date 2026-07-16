import os
import re
import json
import time
from openai import OpenAI, RateLimitError
from lesson_paths import lesson_path

api_key = os.environ["OPENAI_API_KEY"]

client = OpenAI(
    api_key=api_key,
    base_url = os.environ["LLM_BASE_URL"],
    default_headers={"x-goog-api-key": api_key}
)

# ── Concept filtering ──────────────────────────────────────────────────────────
def filter_concepts(curriculum: dict) -> list:
    filtered = []
    supplementary_buffer = []

    for concept in curriculum["curriculum_graph"]["concepts"]:
        score      = concept.get("importance_score", 0.0)
        importance = concept.get("importance", "supplementary")

        if importance == "core" or score >= 0.7:
            filtered.append({**concept, "_slide_treatment": "full"})
        elif importance == "supporting" or score >= 0.4:
            filtered.append({**concept, "_slide_treatment": "condensed"})
        else:
            supplementary_buffer.append(concept["name"])

    if supplementary_buffer:
        filtered.append({
            "concept_id": "c_supplementary",
            "name": "Additional Topics",
            "description": "Supplementary concepts covered briefly: " + ", ".join(supplementary_buffer),
            "importance": "supplementary",
            "importance_score": 0.2,
            "relationships": [],
            "study": {"flashcards": [], "quiz_questions": []},
            "_slide_treatment": "summary"
        })

    return filtered


def slim_concepts(concepts: list) -> list:
    """Strip study data — the LLM only needs structure, not full quiz/flashcard content."""
    keep = {"concept_id", "name", "description", "importance",
            "importance_score", "relationships", "_slide_treatment"}
    return [{k: v for k, v in c.items() if k in keep} for c in concepts]


# ── JSON repair helpers ────────────────────────────────────────────────────────
def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def attempt_repair(raw: str) -> str:
    raw = re.sub(r",\s*([}\]])", r"\1", raw)                          # trailing commas
    raw = raw.replace("\u201c", '"').replace("\u201d", '"')            # curly quotes
    raw = raw.replace("\u2018", "'").replace("\u2019", "'")
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)      # control chars
    return raw


def close_truncated_json(raw: str) -> str:
    """
    Count unclosed brackets/braces and append the missing closers.
    Handles the most common truncation case where the LLM runs out of tokens
    mid-array or mid-object.
    """
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
        closer = "".join(reversed(stack))
        print(f"[truncation repair] Closing {len(stack)} open bracket(s): {closer!r}")
        return raw + closer
    return raw


def find_error_context(raw: str, char_pos: int, window: int = 120) -> str:
    start   = max(0, char_pos - window)
    end     = min(len(raw), char_pos + window)
    snippet = raw[start:end]
    marker  = " " * (char_pos - start) + "^"
    return f"...{snippet}...\n    {marker}"


def call_llm(messages: list, label: str = "", max_tokens: int = 50000) -> tuple[str, str]:
    tag = f"[{label}] " if label else ""
    print(f"{tag}Calling LLM (max_tokens={max_tokens})...")

    for backoff_attempt in range(4):
        try:
            response = client.chat.completions.create(
                model="gpt-5.4-mini",
                response_format={"type": "json_object"},
                messages=messages,
                temperature=0.15,
                max_tokens=max_tokens,
            )
            break
        except RateLimitError:
            wait = 2 ** backoff_attempt
            print(f"{tag}rate limited, retrying in {wait}s")
            time.sleep(wait)
    else:
        raise RuntimeError(f"{tag}exceeded retries after repeated rate limiting")

    finish = response.choices[0].finish_reason
    if finish == "length":
        print(f"WARNING [{label}]: output truncated (finish_reason=length).")
    return response.choices[0].message.content.strip(), finish


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
    
def has_variable(latex: str) -> bool:
    """Returns True if LaTeX contains a variable letter (not just numbers/operators)."""
    # Remove known number-only patterns and operators, check if letters remain
    cleaned = re.sub(r'\\\\[a-zA-Z]+', '', latex)  # remove \commands
    cleaned = re.sub(r'[0-9\s\+\-\=\*\/\.\,\%\^\{\}\(\)\[\]\\]', '', cleaned)
    return len(cleaned) > 0  # any remaining letters = variables


# ── Prompts ────────────────────────────────────────────────────────────────────
system_prompt = """
You are an expert instructional designer building a structured slideshow lesson plan.
You receive a curriculum graph of concepts and must generate a complete, content-rich
slideshow JSON. Every concept must be taught thoroughly.

═══════════════════════════════════════
SLIDE TYPES
═══════════════════════════════════════
- title      : one opening slide for the whole course
- concept    : teaches one concept (may span 2 slides for dense content)
- summary    : groups supplementary concepts or wraps a topic cluster
- animation  : a bonus slide for a core concept that fundamentally involves
               motion, transformation, or change over time. Optional —
               only used when it would genuinely aid understanding.
- quiz_prompt: poses a question — no answer shown, just for engagement
- transition : signals a shift between topic clusters (keep very brief)

═══════════════════════════════════════
SLIDE COUNT — DO NOT SKIP CONCEPTS
═══════════════════════════════════════
- Core concepts (_slide_treatment: full): 2 slides each minimum.
  Use Slide 1 for definitions and context, Slide 2 for equations and examples.
  If a concept has BOTH many definitions AND many equations, always use 2 slides.
- Supporting concepts (_slide_treatment: condensed): 1-2 slides each.
- Supplementary (_slide_treatment: summary): 1 grouped slide maximum.
- Add 1 quiz_prompt per 3-4 core concepts.
- Add transition slides only between unrelated topic clusters.

DO NOT reduce slide count to save tokens. A 30-concept curriculum should produce
40-60+ slides. Every concept deserves full treatment.

═══════════════════════════════════════
SLIDE DENSITY RULES
═══════════════════════════════════════
1. MAX 4 elements of type definition/equation/example per slide combined.
   If more are needed, split across slides.
2. TEXT IS MANDATORY: every slide must have at least one "text" element.
   Text elements must genuinely explain — not just label — the content.
   Every definition/equation/example must be preceded or followed by a
   text element that explains why it matters or how to read it.
3. NEVER leave a slide with only definitions or only equations and no prose.
4. NEVER crowd — split rather than overflow.

═══════════════════════════════════════
ANIMATION SLIDES — MINIMUM 1, MAXIMUM 8
═══════════════════════════════════════
- Include AT LEAST 1 animation slide in the ENTIRE lesson, and up to 8.
  Every lesson has some concept that benefits from motion — a shape
  transforming, a value changing, a process unfolding step by step.
  Scan the full curriculum for the strongest 1-8 candidates before
  concluding none exist.
- Subjects like math, physics, and geometry usually have several strong
  candidates: number lines shifting, shapes rotating/scaling, graphs
  changing as a variable moves, step-by-step equation manipulation,
  fractions splitting or combining, etc. Treat these as likely candidates,
  not exceptions.
- An animation slide is a BONUS slide added immediately after its related
  concept slide(s). It never replaces a concept slide — the concept must
  already be fully taught before an animation slide appears.
- Only mark a concept for animation if a student would gain understanding
  from watching something move or transform that they could not get from
  a still image or text. If the concept is definitional, historical, or
  purely verbal, do NOT create an animation slide for it.
- Choose only the strongest candidates across the whole lesson — do not
  spread animations evenly or force one per topic cluster.

═══════════════════════════════════════
BODY ELEMENT TYPES
═══════════════════════════════════════
- text      : a full prose sentence or paragraph. Required on every slide.
              Must explain, connect, or contextualize — not just restate.
- bullet    : a bullet point. Use level 0-2 for nesting.
- equation  : a GENERAL, SYMBOLIC LaTeX formula using variables/symbols only.
              Escape ALL backslashes: \\\\frac not \\frac.
              AN EQUATION MUST contain at least one variable (a, b, x, n, r, P...).
              WRONG — these are worked examples, use "example" instead:
                "4.7 \\\\times 10 = 47"
                "0.37 = 37\\\\%"
                "12\\\\% = 0.12"
              RIGHT — these are true equations with variables:
                "a \\\\times 10^n"
                "P \\\\times r \\\\times t"
                "\\\\frac{a}{b} \\\\times 100"
- definition: a term being formally defined. content = "Term: definition text"
- example   : a concrete worked example with SPECIFIC NUMBERS only.
              Use this whenever the math contains only numbers, no variables.
              Wrap math in $ delimiters: "$4.7 \\\\times 10 = 47$"
              Do NOT prefix content with "Example:" — label is added 

═══════════════════════════════════════
LAYOUT — VISUAL VARIETY
═══════════════════════════════════════
Every slide must include a "layout" field chosen from these 5 options:

"default"
  Single column, elements stacked top to bottom.
  Use for: title slides, transition slides, simple concept introductions.

"two_col"
  Body items are split into two columns by a visual divider.
  Items up to and including the first "col_break" marker go in the left column.
  Items after "col_break" go in the right column.
  Add a special marker element: { "type": "col_break" } where the split should occur.
  Use for: comparing two ideas, definition on left + example on right,
           two related equations side by side.
  BREVITY RULE: each column gets ONE text sentence maximum.
  With definitions or examples already present, the text is just
  a label. Put explanation in speaker notes.

"highlight_box"
  The FIRST text element is rendered as a large, color-blocked callout box.
  Remaining body elements appear below it in the normal column layout.
  Use for: key insight slides, quiz_prompt slides, summary openers,
           slides where one central idea must stand out.

"equation_hero"
  The FIRST equation element is rendered large and centered with emphasis.
  All other body elements appear below it.
  Use for: slides where one formula is the star of the show.

"cards"
  Each definition or example element is rendered as a distinct card tile.
  Text elements above the first definition/example serve as intro prose.
  Use for: slides introducing 2-4 definitions or worked examples at once.

Vary layouts across the slideshow. Do not use "default" for every slide.
A typical 40-slide deck might use: 15 default, 10 two_col, 6 highlight_box,
5 equation_hero, 4 cards.

═══════════════════════════════════════
SPEAKER NOTES — CRITICAL
═══════════════════════════════════════
- Write as a complete spoken teacher narration, 60-120 words per slide.
- Narrate every body element in order — definitions, equations, examples.
- Expand beyond the slide text: explain WHY, connect to prior concepts,
  give intuition, walk through examples step by step.
- For equations: read aloud in plain English, explain each variable.
- Plain prose only — no bullets, headers, or markdown.

═══════════════════════════════════════
ANIMATION HINTS — KEEP EMPTY
═══════════════════════════════════════
Always use: "animation_hints": []
Do not generate animation hints — they will be handled by the frontend.

═══════════════════════════════════════
OUTPUT FORMAT — STRICT JSON RULES
═══════════════════════════════════════
- Return ONLY a valid JSON object. No markdown fences. No commentary.
- No trailing commas before } or ]
- Escape backslashes: write \\\\ not \\  (LaTeX: "\\\\frac{a}{b}")
- No literal newlines inside strings — use \\n if needed
- "total_slides" must exactly equal the number of slides in the array

Schema — every slide must match this exactly:
{
  "slide_id": 1,
  "type": "title|concept|summary|quiz_prompt|transition",
  "layout": "default|two_col|highlight_box|equation_hero|cards",
  "concept_ids": ["c001"],
  "title": "<slide title>",
  "body": [
    { "type": "text|bullet|equation|definition|example|col_break", "content": "...", "level": 0 }
  ],
  "speaker_notes": "<spoken narration, 60-120 words>",
  "animation_hints": [],
  "source_slides": []
}

Animation slide schema (only used when type is "animation"):
{
  "slide_id": 12,
  "type": "animation",
  "concept_ids": ["c001"],
  "title": "<slide title>",
  "animation_description": "<plain-language description of what should
    move/transform and why it illustrates the concept, 2-4 sentences>",
  "speaker_notes": "<spoken narration, 60-120 words>",
  "source_slides": []
}

Full output schema:
{
  "slideshow": {
    "course": "<course name>",
    "total_slides": <integer>,
    "slides": [ ...slide objects... ]
  }
}
""".strip()

def run_slideshow_generation(curriculum_data: dict, lesson_id: str) -> dict: 
    filtered_concepts = filter_concepts(curriculum_data)
    slimmed_concepts  = slim_concepts(filtered_concepts)
    course_name       = curriculum_data["curriculum_graph"]["course"]
    concept_text      = json.dumps(slimmed_concepts, indent=2, ensure_ascii=False)

    user_prompt = f"""
Course: {course_name}

Concepts to teach (treat every concept fully — do not skip any):
{concept_text}

Generate the complete slideshow JSON.

Requirements:
- Core concepts get 2 slides each minimum. Do not compress them to save space.
- Every slide needs at least one "text" element that genuinely explains the content.
- Every definition/equation/example must have accompanying explanatory text.
- Vary the layout field across slides — use all 5 layouts across the deck.
- Speaker notes must be 60-120 words of genuine spoken narration per slide.
- animation_hints must always be an empty array [].
- Return only valid JSON. No markdown. No trailing commas. No commentary.
- Escape all LaTeX backslashes: write \\\\\\\\ not \\\\.
- total_slides must equal the actual number of slides in the array.
""".strip()


# ── Generation + parse loop ────────────────────────────────────────────────────
    MAX_RETRIES = 1
    MAX_REDOS = 1  # full fresh-conversation restarts if all repair attempts fail
    
    slideshow = None
    for redo in range(MAX_REDOS + 1):

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]

        last_raw    = ""
        last_finish = ""

        for attempt in range(1, MAX_RETRIES + 2):
            label = f"redo {redo} attempt {attempt}"

            if attempt == 1:
                raw, finish = call_llm(messages, label, max_tokens=50000)
            elif last_finish == "length":
                # Truncation: try structural close before giving up
                print(f"[{label}] Truncation on previous attempt — trying structural close...")
                raw    = last_raw
                finish = "truncation_repair"
            else:
                # Parse error: ask the LLM to regenerate more concisely
                print(f"[{label}] Asking LLM to regenerate after parse error...")
                retry_messages = messages + [
                    {"role": "assistant", "content": last_raw},
                    {"role": "user", "content": (
                        "Your previous JSON had a syntax error and could not be parsed. "
                        "Please regenerate the complete slideshow JSON. "
                        "Keep speaker_notes under 80 words per slide. "
                        "Use empty animation_hints arrays. "
                        "Return only valid JSON — no markdown, no trailing commas."
                    )},
                ]
                raw, finish = call_llm(retry_messages, label, max_tokens=50000)

            last_raw    = raw
            last_finish = finish

            was_truncated = finish in ("length", "truncation_repair")
            result = parse_with_repair(raw, label, was_truncated=was_truncated)

            if result is not None:
                slideshow = result
                print(f"Parsed successfully on {label}.")
                break

            if attempt <= MAX_RETRIES:
                print(f"Will retry ({attempt}/{MAX_RETRIES})...")
        if slideshow is not None:
            break
        print(f"[redo {redo}] All {MAX_RETRIES + 1} attempts exhausted." + 
              (" Restarting with a fresh conversation..." if redo < MAX_REDOS else " Giving up."))

    if slideshow is None:
        slideshow = {"error": "failed to parse after retries and redo", "raw": last_raw}


    # ── Post-processing ────────────────────────────────────────────────────────────

    VALID_LAYOUTS = {"default", "two_col", "highlight_box", "equation_hero", "cards"}

    if "slideshow" in slideshow:
        slides = slideshow["slideshow"]["slides"]
        actual_count = len(slides)
        slideshow["slideshow"]["total_slides"] = actual_count

        animation_slides = [s for s in slides if s.get("type") == "animation"]
        if len(animation_slides) > 8:
            print(f"  [fix] {len(animation_slides)} animation slides found, trimming to 8")
            excess = animation_slides[8:]
            slides[:] = [s for s in slides if s not in excess]

        violations = []
        for slide in slides:
            body = slide.get("body", [])

            for el in body:
                if el.get("type") == "equation" and "content" in el:
                    if not has_variable(el["content"]):
                        print(f"  [fix] Slide {slide['slide_id']}: demoting numeric equation "
                            f"to example: {el['content'][:50]!r}")
                        el["type"] = "example"

            # Coerce missing/invalid layout to "default"
            if slide.get("layout") not in VALID_LAYOUTS:
                print(f"  [fix] Slide {slide['slide_id']}: invalid layout "
                    f"{slide.get('layout')!r} → 'default'")
                slide["layout"] = "default"

            # Ensure animation_hints is always a clean empty list
            slide["animation_hints"] = []

            # Density check
            real_items  = [el for el in body if el.get("type") != "col_break"]
            dense_count = sum(1 for el in real_items if el.get("type") in ("definition", "equation", "example"))
            has_text    = any(el.get("type") == "text" for el in real_items)

            if dense_count > 4:
                violations.append(
                    f"  Slide {slide['slide_id']} '{slide['title']}': "
                    f"{dense_count} definition/equation/example elements (max 4)"
                )
            if not has_text and real_items:
                violations.append(
                    f"  Slide {slide['slide_id']} '{slide['title']}': no text element"
                )

        if violations:
            print("DENSITY VIOLATIONS DETECTED:")
            for v in violations:
                print(v)
        else:
            print("All slides passed density checks.")

        print(f"Slideshow generated: {actual_count} slides.")

    out_path = lesson_path(lesson_id, "slideshow.json", create_dir=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(slideshow, f, indent=2, ensure_ascii=False)

    return slideshow


if __name__ == "__main__": 
    curriculum_file = input("extracted_concepts.json path: ").strip() or "extracted_concepts.json"
    lesson_id = input("Lesson id (blank = 'manual-test'): ").strip() or "manual-test"
    with open(curriculum_file, "r", encoding="utf-8") as f:
        curriculum_data = json.load(f)
    run_slideshow_generation(curriculum_data, lesson_id)
    print(f"Done. Output written to backend/lessons/{lesson_id}/slideshow.json")