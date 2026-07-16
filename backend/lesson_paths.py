"""
Shared lesson-identity helpers for the pipeline.

Every generated lesson gets its own folder under backend/lessons/<lesson_id>/.
All pipeline scripts (text extraction, curriculum extraction, slideshow
generation) and eventually server.py import from here so the ID format and
folder layout stay consistent in exactly one place.
"""

import os
import re
import json
import secrets
from datetime import datetime, timezone

# Anchor everything to the repo root
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)

LESSONS_DIR = os.path.join(BACKEND_DIR, "data", "lessons")
UPLOAD_DIR  = os.path.join(BACKEND_DIR, "data", "uploads")
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend", "dist")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Fresh clones / fresh Docker volume mounts won't have these yet since
# they're gitignored — create them on import so routes never 404 on a
# directory that simply hasn't been created yet.
os.makedirs(LESSONS_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

class Status:
    QUEUED              = "queued"
    EXTRACTING          = "extracting"
    BUILDING_CURRICULUM = "building_curriculum"
    GENERATING_SLIDES   = "generating_slides"
    READY               = "ready"
    FAILED              = "failed"

STATUS_INFO = {
    Status.QUEUED:              {"label": "Queued",                "progress": 0.0},
    Status.EXTRACTING:          {"label": "Reading your document",  "progress": 0.20},
    Status.BUILDING_CURRICULUM: {"label": "Mapping out concepts",   "progress": 0.50},
    Status.GENERATING_SLIDES:   {"label": "Building your slides",   "progress": 0.80},
    Status.READY:               {"label": "Ready",                  "progress": 1.0},
    Status.FAILED:              {"label": "Something went wrong",   "progress": None},
}


def new_lesson_id() -> str:
    """
    Sortable, collision-resistant lesson ID, e.g. '20260625-143022-a1b2c3'.
    The timestamp prefix means lessons sort newest-last by plain string sort
    (handy later for the library list); the hex suffix avoids collisions if
    two lessons are ever created in the same second.
    """
    stamp  = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(3)
    return f"{stamp}-{suffix}"


def _validate(lesson_id: str) -> str:
    """
    Reject anything that isn't a plain id. This matters once lesson_id starts
    arriving from a URL (server.py, later) rather than only from a script's
    own argv — an unvalidated id flowing into os.path.join is a path-traversal
    risk (e.g. lesson_id = '../../etc').
    """
    if not lesson_id or not _ID_RE.match(lesson_id):
        raise ValueError(f"Invalid lesson_id: {lesson_id!r}")
    return lesson_id


def lesson_dir(lesson_id: str, create: bool = False) -> str:
    path = os.path.join(LESSONS_DIR, _validate(lesson_id))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def lesson_path(lesson_id: str, filename: str, create_dir: bool = False) -> str:
    """Path to a specific file inside a lesson's folder."""
    return os.path.join(lesson_dir(lesson_id, create=create_dir), filename)


def write_meta(lesson_id: str, **fields) -> dict:
    """
    Merge `fields` into lessons/<id>/meta.json, creating it if needed.
    Each pipeline stage calls this with whatever it knows (source filename,
    course name, slide count, a status string) — later stages only add to it,
    never wipe out what an earlier stage already recorded.
    """
    path = lesson_path(lesson_id, "meta.json", create_dir=True)
    meta = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            meta = json.load(f)

    meta.update(fields)
    meta.setdefault("lesson_id", lesson_id)
    meta.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)  # atomic on POSIX and Windows — no reader ever sees a partial file
    return meta


def read_meta(lesson_id: str) -> dict:
    with open(lesson_path(lesson_id, "meta.json"), "r", encoding="utf-8") as f:
        return json.load(f)