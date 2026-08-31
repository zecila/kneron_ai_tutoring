# Kneron Tutoring Project

Kneron turns a PDF, DOCX, or PPTX document into an interactive, narrated lesson. The application combines generated slides and study materials with quizzes, progress tracking, classroom assignments, and a context-aware voice tutor represented by a LiveTalking avatar.

## Features

- **Generated lessons:** extract text and equations, map the source into a curriculum graph, generate per-concept quizzes, build a slideshow, and optionally render Manim animations.
- **Interactive presentation:** navigate responsive slides, read speaker notes, play synthesized narration, change playback speed, and open expanded definition or equation cards.
- **Study workspace:** review key terms, formulas, flashcards, saved items, and generated quizzes organized by concept.
- **Progress tracking:** resume the last viewed slide, record completion, review quiz attempts, and compare latest and best scores.
- **Accounts and anonymous use:** create personal lessons without an account, then claim that session's lessons and activity when signing up or logging in. Accounts support student and teacher roles, editable names, password changes, and password resets.
- **Classroom workflow:** teachers manage classes, rosters, and assignments; students join with an invite code and complete published work.
- **Live tutor:** ask typed or spoken questions while viewing a lesson. The tutor grounds its answer in the current slide or study concept and other relevant curriculum content, then speaks through a lip-synchronized WebRTC avatar.

## Student And Teacher Workflows

### Students

Students can create up to 10 personal lessons, join or leave classes with a six-character invite code, open published assignments, and track personal and assigned work from the Progress page. Quiz history is grouped into attempts, and a teacher can set a per-concept attempt limit for an assignment.

### Teachers

Teachers can create up to 10 active classes, rename or archive them, generate 30-minute invite codes, inspect the roster, and remove students. An assignment starts as a generated draft that the teacher can preview in the student view, regenerate, discard, or publish. Published assignments support a custom title, due date and time, quiz-attempt limit, later edits, and archiving.

Teachers can also generate a temporary sample lesson outside a class. The interface prompts the teacher to discard this trial lesson when leaving it; assignments should be created from the relevant class when the work needs to be retained and distributed.

## Architecture

Four containers are orchestrated with Docker Compose. LiveTalking currently runs as a separate local process:

| Service | What it does | Port |
|---|---|---|
| `backend` | Flask app: auth, lesson pipeline orchestration, API, serves the built frontend | `5000` |
| `manim-agent` | Standalone MCP server that renders Manim animations on request | `8000` |
| `redis` | Shared rate-limit counters and tutor request coordination across gunicorn workers | `6379` (internal only) |
| `whisperlivekit` | CPU-backed streaming speech recognition | `8002` |
| LiveTalking (local) | WebRTC tutor avatar and lip synchronization | `8010` |

The browser and service traffic is split as follows:

```text
Browser ──HTTP/API──────────────> Flask backend ──LLM/TTS requests──> configured AI services
   │                                  │
   │                                  ├──lesson media/rendering────> Manim agent
   │                                  ├──session coordination──────> Redis
   │                                  └──WebRTC/avatar commands────> LiveTalking
   └──streaming microphone audio───────────────────────────────────> WhisperLiveKit
```

The frontend is a Vite-built static bundle (`frontend/`), served directly by Flask from `frontend/dist` — there's no separate frontend server or container. The browser connects directly to WhisperLiveKit for streaming speech recognition; Flask returns the configured browser-facing WebSocket URL but does not proxy microphone audio.

Lesson generation runs asynchronously through a priority queue with these resumable stages:

```text
document → normalized text/OCR → curriculum graph + quizzes → slideshow JSON → optional Manim media
```

The frontend polls generation status while the pipeline runs. A failed lesson records the stage and error so its owner can retry from the failed stage; assignment drafts can also be regenerated from the original upload.

See [`API.md`](./API.md) for the full API reference and [`HANDOFF.md`](./HANDOFF.md) for the complete local-machine setup.

## Prerequisites

- Docker with Compose v2 (`docker compose`, not the older standalone `docker-compose`)
- An NVIDIA GPU and current driver for LiveTalking
- Sibling checkouts of the pinned LiveTalking and WhisperLiveKit repositories
- An OpenAI-compatible API key (see [Environment variables](#environment-variables))
- Python 3.12 and the pinned LiveTalking environment

## Setup

1. **Clone the tutor, pinned LiveTalking fork, and pinned WhisperLiveKit repositories as siblings.** See [`HANDOFF.md`](./HANDOFF.md).

2. **Prepare the LiveTalking model and avatar**, then run `./scripts/setup-local.sh`. The helper creates `.env` from `.env.example` on its first run and checks the local prerequisites.

3. **Start LiveTalking** in its Python environment:
   ```bash
   python app.py --transport webrtc --model wav2lip --avatar_id tutor_avatar_v3
   ```

4. **Build and start the Compose services:**
   ```bash
   docker compose up -d --build
   ```
   The first build and Whisper model download can take several minutes. Subsequent starts without code changes can drop `--build`.

5. **Open the app:** [http://localhost:5000](http://localhost:5000)

6. **Stop the Compose services:**
   ```bash
   docker compose down
   ```
   This stops and removes the containers but **keeps your data**. Tutor data in `backend/data/` is a host bind mount and is not touched by `down`. Avoid `docker compose down -v` unless you also intend to remove the cached WhisperLiveKit model.

## Environment variables

Copy `.env.example` to `.env` and set these values:

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
| `TUTOR_CHAT_MODEL` | OpenAI-compatible model used for contextual tutor replies. Defaults to `gpt-5.4-mini` |
| `TTS_MAX_CHARS_PER_REQUEST` | Maximum text sent in one TTS request. Longer narration and tutor replies are merged from WAV chunks. Defaults to `500` |
| `REDIS_URL` | Already set in `docker-compose.yml` to `redis://redis:6379` — no action needed unless running outside Compose |
| `FLASK_ENV` | `development` or `production`. Compose defaults to `development` unless overridden — set `FLASK_ENV=production` in your shell or `.env` before `up` for a production-style run |

`MANIM_MCP_URL` and `MANIM_DOWNLOAD_URL` are already wired up in `docker-compose.yml` to point at the `manim-agent` service by its container name — no need to set these yourself under normal Compose usage.

`LIVETALKING_BASE_URL` is also wired to `http://host.docker.internal:8010`, where the separately started local LiveTalking process listens.

The included WhisperLiveKit service runs the Faster Whisper `base` model on CPU, publishes port `8002`, and uses this Deepgram-compatible streaming endpoint:

```env
STT_WEBSOCKET_URL=ws://127.0.0.1:8002/v1/listen?language=en&encoding=linear16&sample_rate=16000&channels=1&interim_results=true&endpointing=1000&vad_events=true
```

The microphone UI sends 16 kHz mono PCM audio, displays interim transcription, and submits a finalized utterance through the same tutor flow as typed text. A recording is limited to 60 seconds and the submitted transcript is limited to 2,000 characters. The browser must have microphone permission, and its origin must be included in `WHISPER_CORS_ORIGINS`.

The remaining WhisperLiveKit settings in `.env.example` select the sibling checkout, published port, backend, model, language, pause segmentation, CORS origins, and optional Hugging Face token. `LIVETALKING_SIGNALING_TIMEOUT` and `LIVETALKING_COMMAND_TIMEOUT` can also be set when the default 60-second signaling and 15-second command timeouts do not fit the local machine.

## Current Limits And Development Behavior

- Uploads are limited to 50 MB and must use `.pdf`, `.docx`, or `.pptx`.
- Students and anonymous sessions can own at most 10 personal lessons. Teacher trial lessons are exempt because they are intended to be temporary.
- A teacher can have at most 10 active classes. Generating a new invite code invalidates the previous code; each code expires after 30 minutes.
- Tutor messages, speech text, and finalized microphone transcripts are limited to 2,000 characters. The tutor uses up to 12 recent conversation messages and up to five relevant lesson concepts when building context.
- Password reset is development-only: the backend prints a localhost reset link to its logs instead of sending email.
- The API applies per-user or per-session rate limits backed by Redis. See [`API.md`](./API.md#rate-limits) for route-specific values.

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
├── .env.example
├── docker-compose.yml
├── HANDOFF.md
├── API.md
├── README.md
├── scripts/
│   └── setup-local.sh
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── server.py            # Flask app, routes, auth
│   ├── db.py                # SQLite connection helper
│   ├── lesson_paths.py       # Shared path/ID helpers for the pipeline
│   ├── pipeline_queue.py     # Priority/FIFO background lesson queue
│   ├── tests/                # Queue, STT, and TTS bridge tests
│   ├── data/                 (gitignored — bind-mounted, persists on host)
│   │   ├── app.db
│   │   ├── uploads/
│   │   └── lessons/<lesson_id>/
│   └── pipeline/              # Document extraction → curriculum → slideshow generation
├── frontend/
│   ├── vite.config.js
│   ├── public/                # Application logic, icons, and STT audio worklet
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

**The tutor avatar doesn't connect or speak.** Start the pinned LiveTalking checkout with `tutor_avatar_v3`, then verify it is listening on port `8010`. Run `./scripts/setup-local.sh` to check the fork integration, Wav2Lip model, and avatar assets. Also confirm that the configured TTS service is reachable from the backend container.

**The microphone does not transcribe.** Confirm `whisperlivekit` is healthy, open the application from an origin allowed by `WHISPER_CORS_ORIGINS`, and check that `STT_WEBSOCKET_URL` uses a browser-reachable `ws://` or `wss://` address rather than a Compose service name.

## Acknowledgements

- [LiveTalking](https://github.com/lipku/LiveTalking) provides the real-time, lip-synchronized avatar foundation. This project uses a pinned integration fork containing tutor-specific changes.
- [WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit) provides the real-time streaming speech-to-text service used for tutor microphone input.

Both projects remain subject to their respective licenses. Thank you to their authors and contributors for making this integration possible.
