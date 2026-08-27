# Kneron Tutoring Project

Turn a document (PDF, DOCX, or PPTX) into an interactive, narrated lesson: an AI pipeline extracts key concepts, builds a slideshow (with optional Manim-generated animations), and serves it as a web app with quizzes and progress tracking.

## Architecture

Three containers, orchestrated with Docker Compose:

| Service | What it does | Port |
|---|---|---|
| `backend` | Flask app: auth, lesson pipeline orchestration, API, serves the built frontend | `5000` |
| `manim-agent` | Standalone MCP server that renders Manim animations on request | `8000` |
| `redis` | Backing store for rate limiting (shared counters across gunicorn workers) | `6379` (internal only) |

The frontend is a Vite-built static bundle (`frontend/`), served directly by Flask from `frontend/dist` — there's no separate frontend server or container.

Lesson generation is a two-pass LLM pipeline: document → extracted text → curriculum graph (concepts, flashcards, quiz questions) → slideshow JSON, with an optional additional pass that generates Manim code for `animation`-type slides and dispatches it to `manim-agent` for rendering.

See [`API.md`](./API.md) for the full API reference.

## Prerequisites

- Docker with Compose v2 (`docker compose`, not the older standalone `docker-compose`)
- An OpenAI-compatible API key (see [Environment variables](#environment-variables))
- No local Python, Node, or system libraries needed — everything runs inside the containers

## Setup

1. **Clone the repo.**

2. **Create `backend/.env`** with the required variables (see below). There's no `.env.example` checked in yet — this section is the reference until one exists.

3. **Build and start everything:**
   ```bash
   docker compose up --build
   ```
   First build will take a few minutes (installs system packages, Python deps, and runs the frontend build). Subsequent starts without code changes can drop `--build`.

4. **Open the app:** [http://localhost:5000](http://localhost:5000)

5. **Stop everything:**
   ```bash
   docker compose down
   ```
   This stops and removes the containers but **keeps your data** (`backend/data/` is a bind mount to your host filesystem, not a Docker volume — it isn't touched by `down`). Only `docker compose down -v` would remove volumes, and this project doesn't use named volumes for anything that matters, so `-v` has no effect on your lesson/account data either way.

## Environment variables

Set these in `backend/.env`:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | API key for the LLM provider used by the curriculum/slideshow/animation pipeline |
| `FLASK_SECRET_KEY` | Signs session cookies — keep this stable across restarts, or existing users get logged out |
| `LLM_BASE_URL` | Base URL for the LLM API (supports OpenAI-compatible endpoints) |
| `TTS_BASE_URL` | Base URL for the text-to-speech service backing `/api/tts` |
| `STT_WEBSOCKET_URL` | Full `ws://` or `wss://` URL for the browser-facing streaming speech-to-text service |
| `TTS_MODEL` | Narration TTS model. Defaults to `indextts1.5` |
| `TTS_VERSION` | Narration TTS model version/voice. Defaults to `kneo350` |
| `TUTOR_TTS_MODEL` | Tutor avatar TTS model. Defaults to `TTS_MODEL` |
| `TUTOR_TTS_VERSION` | Tutor avatar TTS model version/voice. Defaults to `TTS_VERSION` |
| `TTS_MAX_CHARS_PER_REQUEST` | Maximum text sent in one TTS request. Longer narration and tutor replies are merged from WAV chunks. Defaults to `500` |
| `REDIS_URL` | Already set in `docker-compose.yml` to `redis://redis:6379` — no action needed unless running outside Compose |
| `FLASK_ENV` | `development` or `production`. Compose defaults to `development` unless overridden — set `FLASK_ENV=production` in your shell or `.env` before `up` for a production-style run |

`MANIM_MCP_URL` and `MANIM_DOWNLOAD_URL` are already wired up in `docker-compose.yml` to point at the `manim-agent` service by its container name — no need to set these yourself under normal Compose usage.

For a local WhisperLiveKit instance published on port `8002`, use its Deepgram-compatible streaming endpoint:

```env
STT_WEBSOCKET_URL=ws://127.0.0.1:8002/v1/listen?language=en&encoding=linear16&sample_rate=16000&channels=1&interim_results=true&endpointing=1000&vad_events=true
```

## Data persistence

`backend/data/` (SQLite database, uploaded source files, and generated lesson folders) is bind-mounted from the host, not stored inside the container image. This means:

- Your data survives rebuilds, restarts, and `docker compose down`.
- It's easy to back up: `cp backend/data/app.db backend/data/app.db.bak`.
- It does **not** currently survive a fresh clone onto a different machine — if you need to move data to another host, copy the `backend/data/` folder over directly.

If you ever see a lesson generate successfully but re-fetching it 404s, or an account you just created disappears after a restart, check that the bind mount path in `docker-compose.yml` (`./backend/data:/app/backend/data`) still matches where `lesson_paths.py` resolves `LESSONS_DIR`/`DB_PATH` — a mismatch there means writes silently land in the container's ephemeral filesystem instead of your host disk.

## System dependencies (for reference — already baked into the images)

You shouldn't need to install any of this yourself; it's here for context if something in the pipeline errors in a way that looks environment-related.

**backend:** `tesseract-ocr` (OCR via pytesseract), `poppler-utils` (PDF rasterization via pdf2image), `libgl1`/`libglib2.0-0` (opencv-python-headless runtime deps), Node.js 20 (frontend build only, not needed at runtime).

**manim-agent:** `ffmpeg` (video encoding), `libcairo2-dev`/`libpango1.0-dev` (text/vector rendering for pycairo and ManimPango), `pkg-config`/`build-essential` (compiling those C extensions), `libgl1` (moderngl headless rendering).

## Project structure

```
kneron_project/
├── docker-compose.yml
├── API.md
├── README.md
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── .env                (gitignored — see Environment variables above)
│   ├── server.py            # Flask app, routes, auth
│   ├── db.py                # SQLite connection helper
│   ├── lesson_paths.py       # Shared path/ID helpers for the pipeline
│   ├── data/                 (gitignored — bind-mounted, persists on host)
│   │   ├── app.db
│   │   ├── uploads/
│   │   └── lessons/<lesson_id>/
│   └── pipeline/              # Document extraction → curriculum → slideshow generation
├── frontend/
│   ├── vite.config.js
│   ├── public/                # Static assets copied as-is (e.g. app.js)
│   ├── dist/                  (gitignored — build output, created at Docker build time)
│   └── src/
└── mcp_Manim_Agent/
    └── src/
        ├── Dockerfile
        ├── server.py
        └── requirements.txt
```

## Troubleshooting

**Can't log in / account seems to have vanished after a restart.** Check the bind mount path matches the actual path the code writes to (see [Data persistence](#data-persistence)) — a path mismatch is the most common cause of data silently not persisting.

**`/app.js` 404s in the browser.** Confirm `frontend/dist/` actually contains `app.js` after the build (`docker compose exec backend ls /app/frontend/dist`). If it's missing, check `vite.config.js`'s `publicDir` setting resolves relative to the project root, not the Vite `root`.

**Animations aren't rendering.** Confirm the `manim-agent` container is up (`docker compose ps`) and reachable from `backend` — `MANIM_MCP_URL`/`MANIM_DOWNLOAD_URL` should resolve via the Compose network (they use the service name `manim-agent`, not `localhost`).
