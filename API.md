# Kneron Tutoring Project API Reference

Base URL: `http://localhost:5000` 

## Table of contents

- [Authentication & sessions](#authentication--sessions)
- [Rate limits](#rate-limits)
- [Auth](#auth)
  - [POST /api/auth/signup](#post-apiauthsignup)
  - [POST /api/auth/login](#post-apiauthlogin)
  - [POST /api/auth/logout](#post-apiauthlogout)
  - [POST /api/auth/change-password](#post-apiauthchange-password)
  - [GET /api/auth/me](#get-apiauthme)
  - [POST /api/auth/delete-account](#post-apiauthdelete-account)
- [Lessons](#lessons)
  - [POST /api/lessons](#post-apilessons)
  - [GET /api/lessons/\<lesson_id\>/status](#get-apilessonslesson_idstatus)
  - [POST /api/lessons/\<lesson_id\>/retry](#post-apilessonslesson_idretry)
  - [GET /api/lessons](#get-apilessons)
  - [DELETE /api/lessons/\<lesson_id\>](#delete-apilessonslesson_id)
  - [GET /api/lessons/\<lesson_id\>/curriculum](#get-apilessonslesson_idcurriculum)
  - [GET /api/lessons/\<lesson_id\>/media/\<filename\>](#get-apilessonslesson_idmediafilename)
- [Progress](#progress)
  - [GET /api/lessons/\<lesson_id\>/progress](#get-apilessonslesson_idprogress)
  - [POST /api/lessons/\<lesson_id\>/progress](#post-apilessonslesson_idprogress)
  - [GET /api/progress](#get-apiprogress)
- [Quizzes](#quizzes)
  - [POST /api/lessons/\<lesson_id\>/quiz-attempt](#post-apilessonslesson_idquiz-attempt)
  - [POST /api/lessons/\<lesson_id\>/quiz-attempt-batch](#post-apilessonslesson_idquiz-attempt-batch)
  - [GET /api/lessons/\<lesson_id\>/quiz-history](#get-apilessonslesson_idquiz-history)
- [Info cards (definitions & equations)](#info-cards-definitions--equations)
  - [POST /api/lessons/\<lesson_id\>/info/definition](#post-apilessonslesson_idinfodefinition)
  - [POST /api/lessons/\<lesson_id\>/info/equation](#post-apilessonslesson_idinfoequation)
- [Text-to-speech](#text-to-speech)
  - [POST /api/tts](#post-apitts)
- [Speech-to-text configuration](#speech-to-text-configuration)
  - [GET /api/stt/config](#get-apisttconfig)
- [Error shape](#error-shape)

---

## Authentication & sessions

Every request carries a signed session cookie (`Flask` session), issued automatically on first contact — you don't need to request one explicitly. This cookie identifies you as either:

- **Anonymous** — a random `session_id` assigned on first visit. Anonymous usage is fully functional; lessons and quiz history are tied to this session.
- **Signed in** — a `user_id` attached to the session after `/api/auth/login` or `/api/auth/signup`. Signing up or logging in automatically **claims** all data created under the current anonymous session, carrying it over to the account.

All endpoints below other than `/api/auth/signup` and `/api/auth/login` rely on this cookie for identity — there is no separate API key or bearer token.

**Include cookies on every request** (`credentials: "include"` in `fetch`, or a cookie jar in your HTTP client), or session-based auth will silently fail.

### Ownership and 404s

Any endpoint scoped to a specific `lesson_id` checks that the calling session/account owns that lesson. If it doesn't — including if the lesson simply doesn't exist — the response is **404 "Lesson not found"**, not 403. This is deliberate: it avoids confirming to a caller that a given lesson ID exists at all.

---

## Rate limits

Limits are keyed to your identity (user, then session, then IP), not shared across users. A default of **200 requests/hour** applies to any route without a more specific limit listed below.

| Route | Limit |
|---|---|
| `POST /api/auth/signup` | 5/hour |
| `POST /api/auth/login` | 10/hour |
| `POST /api/lessons` | 10/hour |
| `POST /api/lessons/<id>/info/definition` | 60/hour |
| `POST /api/lessons/<id>/info/equation` | 60/hour |
| `POST /api/tts` | 120/hour |
| `POST /api/lessons/<id>/avatar/speak` | 120/hour |
| `GET /api/lessons/<id>/status` | none (exempt — safe to poll frequently) |

A `429` response returns `{"error": "Too many requests. Please slow down and try again shortly."}`.

---

## Auth

### `POST /api/auth/signup`
Create an account. Automatically claims the current anonymous session's data.

**Body**
```json
{ "email": "you@example.com", "password": "at least 6 chars" }
```

**Responses**
- `201` — `{ "ok": true, "email": "you@example.com" }`
- `400` — missing fields, invalid email, or password under 6 characters
- `409` — an account with that email already exists

---

### `POST /api/auth/login`
**Body:** `{ "email": "...", "password": "..." }`

**Responses**
- `200` — `{ "ok": true, "email": "..." }`
- `401` — `{ "error": "Invalid credentials" }`

---

### `POST /api/auth/logout`
No body. Clears `user_id` from the session (the session itself, and any anonymous data under it, persists).

**Response:** `200` — `{ "ok": true }`

---

### `POST /api/auth/change-password`
Requires an active login session.

**Body**
```json
{ "current_password": "...", "new_password": "at least 6 chars" }
```

**Responses**
- `200` — `{ "ok": true }`
- `400` — new password too short, or current password incorrect
- `401` — not logged in

---

### `GET /api/auth/me`
Returns the current identity. Safe to call anonymously.

**Responses**
- `200` (logged in) — `{ "logged_in": true, "email": "...", "member_since": "2026-01-01T00:00:00Z" }`
- `200` (anonymous) — `{ "logged_in": false }`

---

### `POST /api/auth/delete-account`
Permanently deletes the account: all owned lesson folders on disk, all DB rows (quiz history, progress, lesson ownership), and the user row itself. Also clears the session, so the browser gets a fresh anonymous identity afterward.

**Responses**
- `200` — `{ "ok": true }`
- `401` — not logged in

---

## Lessons

### `POST /api/lessons`
Upload a document and kick off lesson generation. Returns immediately — generation happens in the background; poll `/status` to track progress.

**Body:** `multipart/form-data` with a `file` field.
Accepted types: `.pdf`, `.docx`, `.pptx`. Max size: 50MB. Max 10 lessons per owner.

**Responses**
- `202` — `{ "lesson_id": "20260713-142200-a1b2c3" }`
- `400` — no file provided, unsupported extension, or lesson limit (10) reached
- `413` — file over 50MB

---

### `GET /api/lessons/<lesson_id>/status`
Poll while a lesson is generating. Not rate-limited — safe to call every couple seconds.

**Response `200`**
```json
{
  "lesson_id": "20260713-142200-a1b2c3",
  "status": "generating_slides",
  "label": "Building your slides",
  "progress": 0.8,
  "source_filename": "chapter3.pdf",
  "course": "Intro to Linear Algebra"
}
```
`status` is one of: `queued`, `extracting`, `building_curriculum`, `generating_slides`, `ready`, `failed`. On `failed`, the payload also includes `failed_stage` and `error`.

**Error:** `404` if the lesson doesn't exist or isn't owned by the caller.

---

### `POST /api/lessons/<lesson_id>/retry`
Resumes a **failed** lesson from the stage it failed at, rather than restarting from scratch.

**Responses**
- `202` — `{ "status": "retrying" }`
- `400` — lesson isn't currently in a failed state
- `404` — lesson not found, or the original uploaded file is missing on disk

---

### `GET /api/lessons`
List lessons owned by the caller (anonymous session or logged-in account), newest first. Each entry is that lesson's current `meta.json` (same shape as the `/status` response).

**Response:** `200` — `[ { "lesson_id": "...", "status": "ready", ... }, ... ]`

---

### `DELETE /api/lessons/<lesson_id>`
Deletes the lesson's DB rows (ownership, quiz history, progress) and removes its folder from disk.

**Responses**
- `200` — `{ "ok": true }`
- `404` — not found / not owned

---

### `GET /api/lessons/<lesson_id>/curriculum`
Returns the full curriculum graph (concepts, relationships, flashcards, quiz questions) generated for the lesson.

**Responses**
- `200` — curriculum graph JSON
- `404` — lesson not found, or curriculum stage hasn't finished yet

---

### `GET /api/lessons/<lesson_id>/media/<filename>`
Serves a generated media file (e.g. an animation `.mp4`) belonging to the lesson.

**Responses**
- `200` — the file, streamed
- `400` — malformed path
- `404` — lesson not found / not owned, or file doesn't exist

*(Note: the finished slideshow itself — slides, narration text, embedded media references — is served by the equivalent `/api/lessons/<lesson_id>/slideshow` route once a lesson reaches `ready`; same auth/404 pattern as above.)*

---

## Progress

### `GET /api/lessons/<lesson_id>/progress`
**Response `200`:** `{ "last_viewed_slide": 4, "completed": false, "updated_at": "..." }` (or `{}` if nothing recorded yet)

### `POST /api/lessons/<lesson_id>/progress`
Upserts progress. Only send the fields you're updating — omitted fields are left unchanged.

**Body:** `{ "last_viewed_slide": 4, "completed": false }`
**Response:** `200` — `{ "ok": true }`

### `GET /api/progress`
Summary for the whole Progress page: every `ready` lesson owned by the caller, each with its progress and full quiz history (concept names resolved from the curriculum file).

**Response `200`**
```json
[
  {
    "lesson": { "lesson_id": "...", "status": "ready", "course": "...", ... },
    "progress": { "last_viewed_slide": 4, "completed": false },
    "quiz_history": [ { "concept_id": "c003", "concept_name": "Eigenvalues", "is_correct": true, ... } ]
  }
]
```

---

## Quizzes

### `POST /api/lessons/<lesson_id>/quiz-attempt`
Submit and grade a single quiz answer.

**Body**
```json
{ "concept_id": "c003", "question_index": 0, "answer_given": "B" }
```

**Responses**
- `200` — `{ "correct": true, "correct_answer": "B" }`
- `400` — invalid `question_index`, or the question is a `short_answer` type (not auto-graded)
- `404` — lesson or `concept_id` not found

---

### `POST /api/lessons/<lesson_id>/quiz-attempt-batch`
Submit several answers for one concept at once (e.g. finishing a quiz page), recorded under a shared timestamp so the frontend can group them into one "run."

**Body**
```json
{
  "concept_id": "c003",
  "attempts": [
    { "question_index": 0, "answer_given": "B" },
    { "question_index": 1, "answer_given": "True" }
  ]
}
```

**Responses**
- `200` — `{ "ok": true }`
- `404` — lesson or `concept_id` not found

---

### `GET /api/lessons/<lesson_id>/quiz-history?concept_id=<optional>`
Full attempt history for the lesson, optionally filtered to one concept, oldest first.

**Response `200`:** `[ { "concept_id": "...", "question_index": 0, "is_correct": true, "submitted_at": "..." }, ... ]`

---

## Info cards (definitions & equations)

Both routes are **on-demand and server-side cached** — the same term/equation asked for the same lesson and context won't trigger a second LLM call.

### `POST /api/lessons/<lesson_id>/info/definition`
**Body**
```json
{ "term": "eigenvalue", "definition_on_slide": "...", "context": "Linear Algebra unit 3", "slide_text": "..." }
```
`term` is required; the rest are optional context to improve relevance.

**Responses**
- `200` — `{ "term": "...", "part_of_speech": "noun", "definition": "...", "examples": ["...", "...", "..."] }`
- `400` — missing `term`
- `404` — lesson not found / not owned
- `503` — upstream AI service temporarily unavailable (safe to retry)

---

### `POST /api/lessons/<lesson_id>/info/equation`
**Body**
```json
{ "latex": "\\frac{d}{dx}x^2", "context": "Calculus unit 1", "slide_text": "..." }
```
`latex` is required.

**Responses**
- `200` — structured equation explanation (steps, worked example, LaTeX-formatted)
- `400` — missing `latex`
- `404` — lesson not found / not owned
- `503` — upstream AI service temporarily unavailable

---

## Text-to-speech

### `POST /api/tts`
Converts narration text to audio. Not lesson-scoped or cached — each call synthesizes fresh audio.

**Body:** `{ "text": "..." }`
**Response:** `200` — raw `audio/wav` bytes (not JSON — read as a blob)
**Error:** `400` — empty text; `502` — upstream TTS service error

Tutor avatar speech uses the same backend TTS service server-side, then forwards the generated WAV to LiveTalking over `/humanaudio`; the browser should listen to the WebRTC stream rather than playing a separate WAV.

Tutor `/avatar/interrupt` and `/avatar/speak` requests include the current positive integer `attempt_id`. Starting a newer attempt invalidates older speech work across backend workers; superseded `/avatar/speak` requests return `409` and never upload stale audio.

---

## Speech-to-text configuration

### `GET /api/stt/config`
Returns the browser-facing streaming STT WebSocket URL configured through `STT_WEBSOCKET_URL`.

**Response:** `200` — `{ "websocket_url": "ws://stt.example/audio/stt" }`

**Error:** `503` — STT is missing or is not a valid `ws://` or `wss://` URL.

The browser connects directly to this URL. This endpoint does not initialize an STT model or proxy audio.

The tutor microphone expects a Deepgram-compatible streaming endpoint, such as WhisperLiveKit's `/v1/listen`,
configured for 16 kHz mono `linear16` audio with interim results and VAD events enabled. During recording, the
browser displays interim and finalized transcript text in the chat input. A non-empty `speech_final` result is
shown for 600 ms and then submitted through the same tutor message flow as typed text. New speech or any normal
recording cancellation clears the pending submission. Recordings are limited to 60 seconds and transcripts to
2,000 characters; reaching either limit submits the current non-empty transcript. Hiding the page or detecting
a long timer gap after device sleep cancels and discards the active recording.

---

## Error shape

Outside of file/audio responses, errors are always JSON:
```json
{ "error": "human-readable message" }
```
Unhandled server exceptions on any `/api/*` route return a generic `500 { "error": "Internal server error" }` rather than leaking a stack trace.
