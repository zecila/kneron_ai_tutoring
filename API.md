# Kneron Tutoring Project API Reference

Base URL: `http://localhost:5000`

This document describes the active routes in `backend/server.py`. The browser application uses cookie-based sessions; there is no bearer-token API.

## Conventions

### Sessions And Identity

Flask issues a signed, 365-day session cookie on first contact. Include that cookie on every request (`credentials: "include"` in `fetch`, or a cookie jar in another client).

- **Anonymous visitors** receive a random `session_id`. They can create personal lessons and retain progress, quiz history, and saved items in that browser session.
- **Signed-in users** have a `user_id` attached to the same session. Signup and login claim data created by the current anonymous session.
- **Roles** are `student` and `teacher`. The UI presents different workflows for each role.

Class routes require a signed-in account. The UI reserves class ownership and assignment management for teachers and class joining for students. At the API layer, mutation routes enforce login, class ownership, or enrollment as documented; they do not all independently re-check the account role.

### Lesson Access

A lesson-scoped route grants access when the caller is either:

- the anonymous session or signed-in user that owns the lesson; or
- a signed-in student enrolled in the class for a currently `published` assignment.

Most inaccessible resources return `404` rather than `403` so the API does not reveal whether another user's resource exists. Owner-only operations, such as retrying or deleting a lesson, do not grant access to assigned students.

### Data Formats

- Request and response bodies are JSON unless the route specifies `multipart/form-data`, `audio/wav`, or an upstream WebRTC response.
- Timestamps are UTC ISO 8601 strings. Assignment `due_at` values are nullable ISO 8601 date-times supplied by the client.
- Boolean values stored in SQLite may appear as `0`/`1` in database-backed response objects.
- Successful routes return `200` unless another status is listed.

### Errors

API errors use this shape:

```json
{ "error": "Human-readable message" }
```

Some tutor errors add fields such as `superseded` or `upstream`. Unhandled `/api/*` exceptions return `500` with a generic message instead of a stack trace.

## Rate Limits

The default limit is **200 requests per hour**. It is keyed by signed-in user, then anonymous session, then IP address. Auth routes marked as IP-based use the client IP directly. Redis stores shared counters under Docker Compose; running without `REDIS_URL` falls back to process-local memory.

| Route | Limit |
|---|---:|
| `POST /api/auth/signup` | 5/hour, IP-based |
| `POST /api/auth/login` | 50/hour, IP-based |
| `POST /api/auth/forgot-password` | 100/hour, IP-based |
| `POST /api/lessons` | 10/hour |
| `POST /api/classes/<class_id>/assignments` | 10/hour |
| `POST /api/classes/<class_id>/assignments/<assignment_id>/regenerate` | 10/hour |
| `POST /api/lessons/<lesson_id>/info/definition` | 60/hour |
| `POST /api/lessons/<lesson_id>/info/equation` | 60/hour |
| `POST /api/tts` | 120/hour |
| `GET /api/health` | Exempt |
| `GET /api/config` | Exempt |
| `GET /api/avatar/health` | Exempt |
| `GET /api/lessons/<lesson_id>/status` | Exempt |
| `POST /api/lessons/<lesson_id>/avatar/webrtc/offer` | 60/hour |
| `POST /api/lessons/<lesson_id>/tutor/message` | 120/hour |
| `POST /api/lessons/<lesson_id>/avatar/speak` | 120/hour |
| `POST /api/lessons/<lesson_id>/avatar/interrupt` | 240/hour |
| `POST /api/lessons/<lesson_id>/avatar/speaking` | 1,200/hour |
| `POST /api/lessons/<lesson_id>/avatar/disconnect` | 240/hour |

A limit violation returns `429`:

```json
{ "error": "Too many requests. Please slow down and try again shortly." }
```

## Authentication And Accounts

### `POST /api/auth/signup`

Creates an account and claims data belonging to the current anonymous session.

```json
{
  "email": "student@example.com",
  "password": "at least 6 characters",
  "role": "student",
  "first_name": "Ada",
  "last_name": "Lovelace"
}
```

`role` defaults to `student`; the accepted values are `student` and `teacher`. Both name fields are required.

- `201`: `{ "ok": true, "email": "student@example.com", "role": "student" }`
- `400`: missing/invalid fields, invalid role, or a password shorter than six characters
- `409`: account already exists

### `POST /api/auth/login`

```json
{ "email": "student@example.com", "password": "..." }
```

- `200`: `{ "ok": true, "email": "student@example.com", "role": "student" }`
- `401`: invalid credentials

Login also claims data associated with the current anonymous session.

### `POST /api/auth/logout`

Clears `user_id` while retaining the browser session.

```json
{ "ok": true }
```

### `GET /api/auth/me`

Safe to call anonymously.

Logged-in response:

```json
{
  "logged_in": true,
  "email": "student@example.com",
  "role": "student",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "member_since": "2026-08-28T18:30:00+00:00"
}
```

Anonymous response: `{ "logged_in": false }`.

### `POST /api/auth/update-name`

Requires login.

```json
{ "first_name": "Ada", "last_name": "Byron" }
```

- `200`: `{ "ok": true, "first_name": "Ada", "last_name": "Byron" }`
- `400`: either name is empty
- `401`: not logged in

### `POST /api/auth/change-password`

Requires login and the current password.

```json
{ "current_password": "...", "new_password": "at least 6 characters" }
```

- `200`: `{ "ok": true }`
- `400`: current password is wrong or the new password is too short
- `401`: not logged in

### `POST /api/auth/forgot-password`

```json
{ "email": "student@example.com" }
```

Always returns the same `200` response to avoid account enumeration:

```json
{
  "ok": true,
  "message": "If that email is registered, a reset link has been sent."
}
```

The current implementation is development-only: for an existing account it prints a localhost reset link to backend logs. It does not send email. The token expires after 30 minutes.

### `POST /api/auth/reset-password`

```json
{ "token": "...", "password": "at least 6 characters" }
```

- `200`: `{ "ok": true }`
- `400`: missing/expired/used token or password too short

### `POST /api/auth/delete-account`

Requires login. Deletes lesson folders owned by the account, removes user-keyed lesson progress and quiz attempts, deletes the user, and clears both signed-in and anonymous session identifiers.

- `200`: `{ "ok": true }`
- `401`: not logged in

## Classes

### `GET /api/classes`

Requires login.

- A teacher receives active classes they own.
- A student receives active classes they joined, including the teacher's first and last name.

```json
{
  "classes": [
    {
      "id": 12,
      "teacher_id": 4,
      "name": "Algebra I",
      "archived": 0,
      "created_at": "2026-08-28T18:30:00+00:00"
    }
  ]
}
```

### `POST /api/classes`

Requires login. The UI exposes this to teachers. An account may own at most 10 active classes.

```json
{ "name": "Algebra I" }
```

- `201`: `{ "ok": true, "class_id": 12 }`
- `400`: missing name or 10-class limit reached
- `401`: not logged in

### `PATCH /api/classes/<class_id>`

Requires ownership of the class.

```json
{ "name": "Advanced Algebra" }
```

- `200`: `{ "ok": true }`
- `400`: empty name
- `404`: class not owned by the caller

### `POST /api/classes/<class_id>/archive`

Requires ownership. Sets the class's `archived` flag; it is then omitted from normal teacher and student class lists.

- `200`: `{ "ok": true }`
- `404`: class not owned by the caller

### `POST /api/classes/<class_id>/invite-code`

Requires ownership. Generates a six-character uppercase alphanumeric code valid for 30 minutes. A new code invalidates every older code for that class.

```json
{
  "ok": true,
  "code": "A1B2C3",
  "expires_at": "2026-08-28T19:00:00+00:00"
}
```

### `GET /api/classes/<class_id>/invite-code`

Requires ownership. Returns the latest code only if it is still valid:

```json
{ "code": "A1B2C3", "expires_at": "2026-08-28T19:00:00+00:00" }
```

When no valid code exists, both values are `null`.

### `POST /api/classes/join`

Requires login. The UI exposes this to students.

```json
{ "code": "A1B2C3" }
```

- `200`: `{ "ok": true, "already_joined": false }`
- `200`: `{ "ok": true, "already_joined": true }` when enrollment already exists
- `400`: missing, invalid, superseded, or expired code
- `401`: not logged in

### `POST /api/classes/<class_id>/leave`

Requires login. Removes the caller's enrollment. The operation is idempotent.

```json
{ "ok": true }
```

### `GET /api/classes/<class_id>/roster`

Requires ownership.

```json
{
  "students": [
    {
      "id": 7,
      "email": "student@example.com",
      "first_name": "Ada",
      "last_name": "Lovelace",
      "joined_at": "2026-08-28T18:30:00+00:00"
    }
  ]
}
```

### `POST /api/classes/<class_id>/students/<student_id>/remove`

Requires ownership. Removes the specified enrollment and returns `{ "ok": true }`.

### `GET /api/classes/<class_id>/students/<student_id>/progress`

Requires ownership and current enrollment of the student. Returns the same array shape as `GET /api/progress`, restricted to assignments from classes owned by the requesting teacher. It never includes the student's personal lessons or another teacher's assignments.

- `200`: progress array
- `404`: class not owned or student not enrolled

## Assignments

Assignment status is `draft`, `published`, or `archived`. Teacher list routes omit archived assignments; student list routes return published assignments only.

### `POST /api/classes/<class_id>/assignments`

Requires ownership. Uploads a source document, creates a draft assignment, and queues lesson generation.

Body: `multipart/form-data` with a `file` field. Accepted extensions are `.pdf`, `.docx`, and `.pptx`; the application-wide upload limit is 50 MB.

- `202`: `{ "assignment_id": 25, "lesson_id": "20260828-183000-a1b2c3" }`
- `400`: missing file or unsupported extension
- `404`: class not owned
- `413`: upload exceeds 50 MB

### `GET /api/classes/<class_id>/assignments`

Requires ownership. Returns non-archived assignments newest first. Each assignment includes `lesson`, containing status metadata plus its display label/progress, or `null` when metadata is unavailable.

```json
[
  {
    "id": 25,
    "class_id": 12,
    "lesson_id": "20260828-183000-a1b2c3",
    "teacher_id": 4,
    "status": "draft",
    "title": null,
    "due_at": null,
    "max_attempts": null,
    "created_at": "2026-08-28T18:30:00+00:00",
    "published_at": null,
    "lesson": { "status": "ready", "label": "Ready", "progress": 1.0 }
  }
]
```

### `GET /api/classes/<class_id>/student-assignments`

Requires current enrollment. Returns published assignments sorted by due date, with assignments lacking a due date last. Each entry includes the same `lesson` metadata enrichment as the teacher list.

### `GET /api/lessons/<lesson_id>/assignment`

Owner-only lookup used while previewing a teacher's generated lesson.

- `200`: assignment object
- `200`: `{}` when the owned lesson is not an assignment
- `404`: lesson not owned

### `DELETE /api/classes/<class_id>/assignments/<assignment_id>`

Requires class ownership and a `draft` assignment. Deletes the draft assignment and its lesson records.

- `200`: `{ "ok": true }`
- `400`: assignment is not a draft
- `404`: class or assignment not found

### `POST /api/classes/<class_id>/assignments/<assignment_id>/regenerate`

Requires class ownership and a draft whose lesson is `ready` or `failed`. Restarts generation from the original upload with priority over normal queued jobs.

- `202`: `{ "status": "regenerating" }`
- `400`: lesson is still processing
- `404`: class, draft, lesson metadata, or original upload not found

### `POST /api/classes/<class_id>/assignments/<assignment_id>/publish`

Requires class ownership and a draft assignment.

```json
{
  "title": "Unit 3 Review",
  "due_at": "2026-09-04T23:59:00.000Z",
  "max_attempts": 3
}
```

All three fields are nullable. An empty title is stored as `null`, causing the generated lesson title to be used.

- `200`: `{ "ok": true }`
- `404`: class not owned or assignment is not a draft

### `PATCH /api/classes/<class_id>/assignments/<assignment_id>`

Requires class ownership and a `published` assignment. Uses the same body as publish and replaces the title, due date, and maximum attempts with the supplied values.

- `200`: `{ "ok": true }`
- `404`: class not owned or assignment is not published

### `POST /api/classes/<class_id>/assignments/<assignment_id>/archive`

Requires class ownership. Sets the assignment status to `archived` and returns `{ "ok": true }`.

## Lessons

### `POST /api/lessons`

Uploads a personal lesson and returns immediately while generation runs in the background.

Body: `multipart/form-data` with a `file` field. Accepted extensions are `.pdf`, `.docx`, and `.pptx`; the upload limit is 50 MB.

Students and anonymous sessions can own at most 10 personal lessons. Teacher uploads through this route are treated by the UI as temporary trial lessons and are not subject to that limit.

- `202`: `{ "lesson_id": "20260828-183000-a1b2c3" }`
- `400`: missing file, unsupported extension, or lesson limit reached
- `413`: upload exceeds 50 MB

### `GET /api/lessons`

Lists lessons owned by the current anonymous session or signed-in user, newest first. Assigned lessons are not included; retrieve those through class/progress routes.

```json
[
  {
    "lesson_id": "20260828-183000-a1b2c3",
    "status": "ready",
    "source_filename": "chapter-3.pdf",
    "course": "Linear Algebra",
    "slide_count": 14,
    "created_at": "2026-08-28T18:30:00+00:00",
    "updated_at": "2026-08-28T18:34:00+00:00"
  }
]
```

### `GET /api/lessons/<lesson_id>/status`

Returns lesson metadata enriched with a display `label` and numeric `progress`. This route is exempt from rate limiting and may be polled during generation.

Statuses:

| Status | Label | Progress |
|---|---|---:|
| `queued` | Queued | `0.0` |
| `extracting` | Reading your document | `0.2` |
| `building_curriculum` | Mapping out concepts | `0.5` |
| `generating_slides` | Building your slides | `0.8` |
| `ready` | Ready | `1.0` |
| `failed` | Something went wrong | `null` |

A failed lesson also records `failed_stage` and `error`.

### `POST /api/lessons/<lesson_id>/retry`

Owner-only. Resumes a failed lesson from its recorded stage and gives the retry priority over normal jobs.

- `202`: `{ "status": "retrying" }`
- `400`: lesson is not failed
- `404`: lesson not owned, metadata missing, or original upload missing

### `GET /api/lessons/<lesson_id>/slideshow`

Returns generated slideshow JSON. A custom assignment title overrides `slideshow.course` in the response.

- `200`: slideshow document
- `404`: no access, slideshow is not generated, or the generated file is unreadable

### `GET /api/lessons/<lesson_id>/curriculum`

Returns the generated curriculum graph containing concepts and study material. A custom assignment title overrides the top-level `course` field.

- `200`: curriculum document
- `404`: no access, curriculum is not generated, or the generated file is unreadable

### `GET /api/lessons/<lesson_id>/progress`

Returns the current identity's database-backed progress row, or `{}` if no progress has been saved.

```json
{
  "session_id": "...",
  "user_id": 7,
  "lesson_id": "20260828-183000-a1b2c3",
  "last_viewed_slide": 4,
  "completed": 0,
  "updated_at": "2026-08-28T18:45:00+00:00"
}
```

### `POST /api/lessons/<lesson_id>/progress`

Upserts progress. Omitted fields retain their previous values.

```json
{ "last_viewed_slide": 4, "completed": false }
```

Returns `{ "ok": true }`.

### `GET /api/progress`

Returns every ready personal lesson owned by the caller plus published or archived class assignments available through the caller's enrollments. Each entry includes lesson metadata, progress, quiz history with resolved concept names, and its source.

```json
[
  {
    "lesson": { "lesson_id": "...", "status": "ready", "course": "Linear Algebra" },
    "progress": { "last_viewed_slide": 4, "completed": 0 },
    "quiz_history": [
      { "concept_id": "c003", "concept_name": "Eigenvalues", "is_correct": 1 }
    ],
    "source": {
      "type": "class",
      "class_name": "Algebra I",
      "due_at": "2026-09-04T23:59:00.000Z",
      "title": "Unit 3 Review",
      "archived": false
    }
  }
]
```

Personal lessons use `{ "type": "personal", "archived": false }` as their source.

### `DELETE /api/lessons/<lesson_id>`

Owner-only. Deletes the generated folder and the owner's lesson record. For assignment-backed lessons, draft assignment data is deleted; a non-draft assignment is archived and submission history is preserved.

- `200`: `{ "ok": true }`
- `404`: lesson not owned

### `GET /api/lessons/<lesson_id>/media/<filename>`

Serves a generated lesson file such as a Manim `.mp4`.

- `200`: file response
- `400`: path would escape the lesson directory
- `404`: no access or file does not exist

## Quizzes

Questions are generated per concept and identified by `question_id`. The obsolete single-question `question_index` submission route is no longer available.

### `GET /api/lessons/<lesson_id>/concepts/<concept_id>/quiz`

Returns the active generated question set and attempt information.

```json
{
  "questions": [
    {
      "question_id": "..._c003_b0_q000",
      "concept_id": "c003",
      "question_text": "Which statement is correct?",
      "type": "multiple_choice",
      "choices": ["A", "B", "C", "D"],
      "answer": "B",
      "explanation": "...",
      "generation_batch": 0,
      "active": 1
    }
  ],
  "max_attempts": 3,
  "attempts_used": 1
}
```

`max_attempts` and `attempts_used` apply only when an enrolled student opens an assignment. Owners receive `null` and `0` respectively.

### `POST /api/lessons/<lesson_id>/quiz-attempt-batch`

Grades a batch of question IDs. The server derives each concept and correct answer from the stored question.

```json
{
  "attempts": [
    { "question_id": "..._c003_b0_q000", "answer_given": "B" },
    { "question_id": "..._c003_b0_q001", "answer_given": "True" }
  ],
  "review": false
}
```

For an assignment student, the batch counts as one attempt per touched concept and is recorded under one timestamp. Questions from exhausted concepts are skipped. Owner previews and requests with `review: true` are graded by the client but not added to history.

```json
{
  "ok": true,
  "touched_concept_ids": ["c003"],
  "exhausted_concept_ids": [],
  "regenerating_concept_ids": ["c003"]
}
```

Unknown question IDs and IDs belonging to another lesson are ignored.
For each accepted non-review attempt, a replacement question batch is generated
in the background and atomically becomes active for that student when it is
complete. Another student's active questions and attempt count are unchanged.
Owner previews regenerate the canonical batch used by students who have not yet
created a personal retry batch.

### `GET /api/lessons/<lesson_id>/quiz-history`

Returns attempts oldest first. Add `?concept_id=c003` to restrict the result to one concept.

Each row includes the question snapshot, given/correct answers, explanation, `is_correct`, `attempt_number`, and `submitted_at`.

## Saved Study Items

### `GET /api/lessons/<lesson_id>/saved-items`

Returns saved key terms, formulas, flashcards, or quiz snapshots for the current identity.

```json
[
  {
    "lesson_id": "...",
    "item_id": "quiz-c003-0",
    "item_type": "quiz",
    "content": { "question_id": "...", "question_text": "..." },
    "created_at": "2026-08-28T18:45:00+00:00"
  }
]
```

### `POST /api/lessons/<lesson_id>/saved-items/<item_id>`

Idempotently saves an item for the current identity.

```json
{ "item_type": "quiz", "content": { "question_id": "..." } }
```

`content` is optional and is stored as a JSON snapshot. Returns `{ "ok": true }`.

### `DELETE /api/lessons/<lesson_id>/saved-items/<item_id>`

Idempotently removes an item and returns `{ "ok": true }`.

## Definition And Equation Cards

Results are held in an in-process LRU cache keyed by lesson, content, and context. The cache is not persistent and is not shared across gunicorn workers.

### `POST /api/lessons/<lesson_id>/info/definition`

```json
{
  "term": "eigenvalue",
  "definition_on_slide": "...",
  "context": "Linear Algebra - Unit 3",
  "slide_text": "..."
}
```

Only `term` is required.

```json
{
  "term": "eigenvalue",
  "part_of_speech": "noun",
  "definition": "...",
  "examples": ["...", "...", "..."]
}
```

- `400`: missing term
- `404`: no lesson access
- `503`: upstream AI service temporarily unavailable

### `POST /api/lessons/<lesson_id>/info/equation`

```json
{
  "latex": "\\frac{d}{dx}x^2",
  "context": "Calculus - Unit 1",
  "slide_text": "..."
}
```

Only `latex` is required. The structured response contains `latex`, `description`, `variables`, `constraints`, and three worked `examples` with step arrays.

- `400`: missing LaTeX
- `404`: no lesson access
- `503`: upstream AI service temporarily unavailable

## Narration And Speech Recognition

### `POST /api/tts`

Synthesizes narration using the configured TTS model.

```json
{ "text": "Narrate this slide." }
```

- `200`: raw `audio/wav` response
- `400`: empty text
- `502`: upstream TTS failure; the JSON error may include `upstream`

Long text is split at sentence/word boundaries according to `TTS_MAX_CHARS_PER_REQUEST`, synthesized in chunks, and merged into one compatible WAV response. The backend initializes the configured models before accepting traffic and retries model initialization after transient runtime failures. A Redis-backed generation lock queues concurrent requests because the configured provider exposes one usable generation slot; lock saturation returns `503` so the browser can retry safely.

### `GET /api/health`

Returns `{ "status": "ok" }` when the backend is accepting requests. Docker Compose uses this route for its backend health check.

### `GET /api/config`

Returns public browser feature configuration and sets `Cache-Control: no-store`.

```json
{ "avatar_enabled": false }
```

### `GET /api/stt/config`

Returns the browser-facing WebSocket URL configured by `STT_WEBSOCKET_URL` and sets `Cache-Control: no-store`.

```json
{
  "websocket_url": "ws://127.0.0.1:8002/v1/listen?language=en&encoding=linear16&sample_rate=16000&channels=1"
}
```

- `200`: valid `ws://` or `wss://` URL
- `503`: missing or invalid URL

The browser connects directly to this endpoint. The Flask route does not initialize a model or proxy audio. The tutor UI expects a Deepgram-compatible stream with interim results and VAD events. It sends 16 kHz mono `linear16` audio, limits recording to 60 seconds and transcripts to 2,000 characters, and submits finalized speech through the tutor message flow.

## Tutor And LiveTalking Avatar

Except for the public health route, routes in this section require lesson access. `sessionid` values are LiveTalking avatar session IDs and must contain 1-128 letters, digits, underscores, or hyphens. A positive integer `attempt_id` orders tutor requests so newer messages can invalidate stale speech work across backend workers.

### `GET /api/avatar/health`

Returns `{ "status": "ready" }` when LiveTalking answers its readiness probe. It returns `503`, `{ "status": "unavailable" }`, and `Retry-After: 2` while LiveTalking is starting or unresponsive. When `AVATAR_ENABLED=false`, it returns `200` with `{ "status": "disabled" }` without contacting LiveTalking. This route is public, rate-limit exempt, and does not expose upstream service details.

All avatar command routes return `503` with `{ "error": "Avatar feature is disabled" }` when `AVATAR_ENABLED=false`.

### `POST /api/lessons/<lesson_id>/avatar/webrtc/offer`

Proxies a browser WebRTC offer to LiveTalking.

```json
{ "type": "offer", "sdp": "..." }
```

The response body, status, and content type come from LiveTalking. Its successful response normally contains the SDP answer and avatar `sessionid`.

- `400`: invalid body, type, or SDP
- `404`: no lesson access
- `503`: LiveTalking is starting or failed its readiness probe; retry shortly
- `502`: LiveTalking signaling unavailable

### `POST /api/lessons/<lesson_id>/tutor/message`

Generates a concise plain-text tutor reply grounded in the lesson.

```json
{
  "message": "Why is this eigenvalue negative?",
  "history": [
    { "role": "user", "content": "What does this matrix represent?" },
    { "role": "assistant", "content": "It represents the linear transformation..." }
  ],
  "scene": "slideshow",
  "current_slide_index": 4,
  "active_concept_id": null
}
```

- `message` is required and limited to 2,000 characters.
- `history` may contain at most 50 entries; only the last 12 are used. Each role must be `user` or `assistant`, each message is limited to 2,000 characters, and the used history is limited to 12,000 characters total.
- `scene`, when present, must be `slideshow` or `study`.
- `current_slide_index` is zero-based and must identify an existing slide when provided.
- `active_concept_id` is optional and limited to 128 characters.

The context includes the course and concept map, the current slide and speaker notes, the active study concept, and up to five question-relevant concepts.

- `200`: `{ "reply": "..." }`
- `400`: invalid message, history, or location fields
- `404`: no lesson access or generated lesson context unavailable
- `500`: stored lesson context is invalid
- `502`: tutor LLM failure or empty response

### `POST /api/lessons/<lesson_id>/avatar/speak`

Synthesizes the reply with the tutor TTS configuration and sends the WAV to LiveTalking.

```json
{
  "sessionid": "avatar-session-id",
  "attempt_id": 8,
  "text": "An eigenvalue can be negative when...",
  "interrupt": true
}
```

`text` is limited to 2,000 characters. `interrupt` defaults to `true`. Mathematical symbols and common units are normalized for speech before synthesis.

- `200`: `{ "ok": true }`
- `400`: invalid body, IDs, text, or `interrupt`
- `409`: `{ "error": "Tutor response was superseded", "superseded": true }`
- `502`: TTS or LiveTalking failure
- `503`: Redis-backed tutor coordination unavailable

The browser hears this audio through the WebRTC avatar stream; it should not play a separate WAV response.

### `POST /api/lessons/<lesson_id>/avatar/interrupt`

Marks the supplied attempt as current and asks LiveTalking to stop queued/current speech.

```json
{ "sessionid": "avatar-session-id", "attempt_id": 9 }
```

- `200`: `{ "ok": true }`
- `400`: invalid body or IDs
- `502`: LiveTalking failure
- `503`: tutor coordination unavailable

### `POST /api/lessons/<lesson_id>/avatar/speaking`

Polls LiveTalking's speaking state.

```json
{ "sessionid": "avatar-session-id" }
```

- `200`: `{ "speaking": true }`
- `400`: invalid body or session ID
- `502`: LiveTalking failure or malformed upstream state

### `POST /api/lessons/<lesson_id>/avatar/disconnect`

Closes and releases the LiveTalking session.

```json
{ "sessionid": "avatar-session-id" }
```

- `200`: `{ "ok": true, "closed": true }`
- `400`: invalid body or session ID
- `502`: LiveTalking failure or malformed upstream response
