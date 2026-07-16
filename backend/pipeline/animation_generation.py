import os
import re
import shutil
import asyncio
import urllib.request
import json
import time
from openai import OpenAI, RateLimitError
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from lesson_paths import lesson_path

from dotenv import load_dotenv
load_dotenv()

api_key = os.environ["OPENAI_API_KEY"]

client = OpenAI(
    api_key=api_key,
    base_url=os.environ["LLM_BASE_URL"],
    default_headers={"x-goog-api-key": api_key}
)

MCP_SERVER_URL = os.environ.get("MANIM_MCP_URL", "http://localhost:8000/mcp/")
DOWNLOAD_BASE_URL = os.environ.get("MANIM_DOWNLOAD_URL", "http://localhost:8000/download")
RENDERED_FILENAME = "Main.mp4"  # matches `manim -qm main.py Main` output in server.py
MAX_ANIMATION_SLIDES = 8
MAX_RETRIES = 2  # compile failures are cheap enough to retry twice; drop to 1 if render cost grows
INTER_SLIDE_DELAY = 3  # seconds between slides — spreads out request rate rather than only reacting to 429s


# ── Manim code generation ───────────────────────────────────────────────────────
animation_system_prompt = """
You are an expert at writing short Manim Community Edition animations for
students from elementary to high school level.

Write a SINGLE Python file defining exactly one class named `Main` that
subclasses `Scene`. The animation must:
- Run for roughly 5-10 seconds total.
- Depict ONE clear transformation, motion, or spatial relationship — nothing else.
- Use simple, readable shapes/text. No external assets, no images, no audio.
- Be visually uncluttered — this is for students, not a technical audience.
- Use `Text(...)` for all on-screen text, including simple math expressions
  like "l = 0" or "base + offset". Do NOT use MathTex or Tex — no LaTeX
  toolchain is available in the render environment.
- Do NOT use TransformMatchingTex to animate between two Text objects — it
  requires Tex/MathTex mobjects internally and will crash on plain Text.
  For transforming one piece of text into another, use TransformMatchingShapes,
  ReplacementTransform, or FadeOut+FadeIn instead.
- If you use NumberLine, always pass `include_numbers=False`. NumberLine's
  built-in tick labels are rendered as MathTex internally even though you
  never write MathTex yourself, and no LaTeX toolchain is available — this
  will crash. Instead, add your own tick labels manually as separate
  `Text(...)` objects positioned with `.next_to(tick_position, DOWN, buff=0.15)`.

LAYOUT — overlapping text and shapes are a common failure. Avoid this by:
- Never place two mobjects at the same coordinates or rely on eyeballed offsets.
  Use `.next_to(other_mobject, DIRECTION, buff=0.3)` to position one mobject
  relative to another with automatic spacing.
- When several mobjects sit in a row or column, group them with
  `VGroup(...).arrange(RIGHT, buff=0.4)` (or `DOWN`) instead of positioning
  each one manually.
- Keep all mobjects within the visible frame: roughly x in [-6.5, 6.5],
  y in [-3.5, 3.5]. Scale down (`.scale(0.8)`) or use a smaller `font_size`
  if a group would otherwise run off-screen.
- Labels must never sit on top of the shape they describe — place them
  above, below, or beside the shape with `.next_to(shape, UP, buff=0.2)`,
  never at the shape's own center.
- Before the final `self.wait()`, everything on screen should occupy
  clearly separate regions with visible spacing between them.

SCALE — objects sized too large for the frame are a common failure, separate
from positioning. Avoid this by:
- Before setting any explicit size (`width=`, `height=`, `length=`, `radius=`,
  `font_size=`), consider how large it will actually render. A `NumberLine`
  or `Rectangle` with `length`/`width` greater than 10 will not fit within
  the visible frame (roughly 13 units wide, 7.5 units tall) alongside any
  other content.
- When a scene has multiple objects sharing the frame, budget space between
  them — e.g. two number lines side by side should each be sized to roughly
  half the frame width, not each sized as if it had the whole frame to itself.
- After building a group with `VGroup(...).arrange(...)`, check its overall
  footprint. If it would exceed roughly 12 units wide or 6.5 units tall,
  call `.scale_to_fit_width(12)` or `.scale_to_fit_height(6.5)` on the group
  before positioning it, rather than trusting the individual object sizes
  to already fit.
- Prefer `font_size` in the 28-40 range for body text and labels; only use
  larger

Return ONLY the Python code. No markdown fences. No commentary. No explanation.
""".strip()


def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()

def get_concept_context(slides: list, animation_slide: dict) -> str:
    """Pulls title + explanatory text from the concept slide(s) that precede
    this animation slide and share its concept_ids, so the generated
    animation stays consistent with how the concept was already taught."""
    concept_ids = set(animation_slide.get("concept_ids", []))
    if not concept_ids:
        return ""

    context_parts = []
    for slide in slides:
        if slide is animation_slide:
            break
        if slide.get("type") != "concept":
            continue
        if concept_ids & set(slide.get("concept_ids", [])):
            title = slide.get("title", "")
            texts = [el["content"] for el in slide.get("body", []) if el.get("type") == "text"]
            context_parts.append(f"{title}: {' '.join(texts)}")

    return "\n".join(context_parts)


def generate_manim_code(animation_description: str, concept_name: str,
                         concept_context: str = "", error_context: str = "") -> str:
    """One LLM call: turn a plain-language animation description into Manim code."""
    user_prompt = f"""
Concept: {concept_name}
{f"How this concept was already taught: {concept_context}" if concept_context else ""}

Animation to depict: {animation_description}

Write the Main Scene class now. Use the same terminology and variable names
as the prior teaching context above, if any is given.
""".strip()

    messages = [{"role": "system", "content": animation_system_prompt}]
    if error_context:
        # Retry path: show the LLM its own broken code and the compiler error.
        messages.append({"role": "user", "content": user_prompt})
        messages.append({"role": "assistant", "content": error_context["code"]})
        messages.append({"role": "user", "content": (
            f"That code failed to compile with this error:\n{error_context['error']}\n"
            "Fix it and return the corrected full Main Scene code only."
        )})
    else:
        messages.append({"role": "user", "content": user_prompt})

    # 429s are common once several animation slides queue LLM calls back-to-back;
    # back off and retry rather than letting the pipeline crash on rate limits.
    for backoff_attempt in range(4):
        try:
            response = client.chat.completions.create(
                model="gpt-5.4-mini",
                messages=messages,
                temperature=0.2,
                max_tokens=2000,
            )
            return strip_fences(response.choices[0].message.content.strip())
        except RateLimitError:
            wait = 2 ** backoff_attempt  # 1s, 2s, 4s, 8s
            print(f"[animation] rate limited, retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("Exceeded retries after repeated rate limiting")


# ── MCP rendering ───────────────────────────────────────────────────────────────
async def render_via_mcp(code: str, job_id: str) -> None:
    async with streamablehttp_client(MCP_SERVER_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("generate_video", arguments={"code": code, "job_id": job_id})
            if result.isError:
                error_text = result.content[0].text if result.content else "unknown error"
                raise RuntimeError(error_text)


def download_rendered_video(job_id: str, dest_path: str) -> None:
    url = f"{DOWNLOAD_BASE_URL}/{job_id}/{RENDERED_FILENAME}"
    urllib.request.urlretrieve(url, dest_path)


# ── Per-slide render loop with repair ───────────────────────────────────────────
def render_animation_slide(slide: dict, all_slides: list, lesson_id: str) -> str | None:
    description = slide.get("animation_description", "")
    concept_name = ", ".join(slide.get("concept_ids", [])) or slide.get("title", "concept")
    concept_context = get_concept_context(all_slides, slide)
    job_id = f"{lesson_id}_{slide['slide_id']}"

    error_context = ""
    last_code = ""
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            code = generate_manim_code(description, concept_name, concept_context, error_context)
            last_code = code
            asyncio.run(render_via_mcp(code, job_id))

            dest_dir = lesson_path(lesson_id, "animations", create_dir=True)
            os.makedirs(dest_dir, exist_ok=True)
            dest_filename = f"slide_{slide['slide_id']}.mp4"
            dest_path = os.path.join(dest_dir, dest_filename)
            download_rendered_video(job_id, dest_path)
            """
            # TESTING
            dest_dir = os.path.dirname(os.path.abspath(__file__))
            dest_filename = f"slide_{slide['slide_id']}.mp4"
            dest_path = os.path.join(dest_dir, dest_filename)
            download_rendered_video(job_id, dest_path)
            return dest_filename
            """
            return f"animations/{dest_filename}"
        except (RuntimeError, ExceptionGroup) as eg:
            e = eg
            while isinstance(e, ExceptionGroup):
                e = e.exceptions[0]
            error_context = {"code": last_code, "error": str(e)}
        except Exception as e:
            # LLM/API errors, download hiccups — treat as a failed attempt
            # too, not a pipeline-ending crash.
            error_context = {"code": last_code, "error": str(e)}

        print(f"[animation slide {slide['slide_id']}] attempt {attempt} failed")
        if attempt > MAX_RETRIES:
            print(f"[animation slide {slide['slide_id']}] giving up after {attempt} attempts.")
            return None

    return None


# ── Entry point ──────────────────────────────────────────────────────────────────
def run_animation_generation(slideshow: dict, lesson_id: str) -> dict:
    """
    Walks the slideshow for animation slides, renders up to MAX_ANIMATION_SLIDES,
    and attaches video_path to each successful slide. Failures are non-fatal —
    the slide simply keeps no video_path and the frontend can skip it.
    """
    if "slideshow" not in slideshow:
        return slideshow

    slides = slideshow["slideshow"]["slides"]
    animation_slides = [s for s in slides if s.get("type") == "animation"][:MAX_ANIMATION_SLIDES]

    failed_slide_ids = set()
    for i, slide in enumerate(animation_slides):
        if i > 0:
            time.sleep(INTER_SLIDE_DELAY)
        video_path = render_animation_slide(slide, slides, lesson_id)
        if video_path is None:
            failed_slide_ids.add(slide["slide_id"])
            print(f"[animation] dropping slide {slide['slide_id']} — render failed")
        else:
            slide["video_path"] = video_path

    if failed_slide_ids:
        slideshow["slideshow"]["slides"] = [
            s for s in slides if s.get("slide_id") not in failed_slide_ids
        ]
        slideshow["slideshow"]["total_slides"] = len(slideshow["slideshow"]["slides"])

    out_path = lesson_path(lesson_id, "slideshow.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(slideshow, f, indent=2, ensure_ascii=False)

    return slideshow