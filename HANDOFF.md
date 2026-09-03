# Local Project Handoff

This guide installs and runs the Kneron tutor on one Linux workstation. Complete the sections in order for a first-time setup.

## What Starts Where

`docker compose up -d` starts these four services:

| Service | Purpose | Host port |
|---|---|---|
| `backend` | Tutor API and built web application | `5000` |
| `manim-agent` | Animation rendering | `8000` |
| `redis` | Shared application state | Internal only |
| `whisperlivekit` | CPU speech-to-text using Faster Whisper `base` | `8002` |

The following dependencies are not started by Compose:

| Dependency | How it runs |
|---|---|
| LiveTalking | A systemd user service supervises its Python process on port `8010` |
| LLM API | External service configured by `LLM_BASE_URL` |
| TTS API | External service configured by `TTS_BASE_URL` |

LiveTalking is installed as a user service once. Normal startup only requires Docker Compose; the user service starts at login and restarts LiveTalking after crashes or failed health checks.

## Files Supplied With The Handoff

The recipient needs:

- Access to the three Git repositories listed below
- `livetalking-assets-v1.tar.gz`
- `livetalking-assets-v1.sha256`
- The real environment values delivered through a private channel
- Access to the configured LLM and TTS services

Do not commit the asset archive, model, avatar, or real `.env` file to Git.

## System Requirements

- Linux; the current setup was tested under Ubuntu/WSL
- Git
- Docker with the `docker compose` command
- systemd user services and `loginctl` for automatic LiveTalking startup
- Python 3.12 for LiveTalking
- FFmpeg for LiveTalking
- An NVIDIA GPU and current NVIDIA driver for LiveTalking
- Enough CPU and system memory for the WhisperLiveKit `base` model

The development machine uses an NVIDIA GeForce RTX 5060 Laptop GPU with 8 GB VRAM and driver 592.82. This records the tested environment; it is not yet a formal minimum hardware specification or concurrency guarantee.

## 1. Clone The Tested Versions

Keep all three repositories in the same parent directory with these exact directory names:

```text
kneron-tutor/
├── kneron_project/
├── LiveTalking/
└── WhisperLiveKit/
```

Clone the tutor's `main` branch, the LiveTalking handoff tag, and the tested WhisperLiveKit commit:

```bash
mkdir kneron-tutor
cd kneron-tutor

git clone --branch main https://github.com/zecila/kneron_ai_tutoring.git kneron_project
git clone --branch kneron-handoff-v1 https://github.com/zecila/LiveTalking.git LiveTalking
git clone https://github.com/QuentinFuxa/WhisperLiveKit.git WhisperLiveKit

git -C WhisperLiveKit checkout ed571b69099d089a04f15b7690bbcae6aa2cc54b
git -C WhisperLiveKit submodule update --init --recursive
```

Keep the tutor repository on `main` so it can receive normal application updates. Keep LiveTalking on `kneron-handoff-v1` and WhisperLiveKit on the commit above unless those integrations are deliberately retested and upgraded.

## 2. Install The LiveTalking Assets

Create a dedicated staging directory and place both downloaded files there:

```bash
mkdir -p ~/kneron-handoff-assets
```

The directory must contain:

```text
~/kneron-handoff-assets/
├── livetalking-assets-v1.tar.gz
└── livetalking-assets-v1.sha256
```

In the commands below, use `~/kneron-handoff-assets` wherever `/path/to/downloaded-assets` appears.

Put both downloaded asset files in the same directory and verify the archive before extracting it:

```bash
cd /path/to/downloaded-assets
sha256sum -c livetalking-assets-v1.sha256
```

Continue only if the output is:

```text
livetalking-assets-v1.tar.gz: OK
```

Extract the archive into the cloned LiveTalking repository:

```bash
tar -xzf livetalking-assets-v1.tar.gz -C /path/to/kneron-tutor/LiveTalking
```

Verify both required assets landed in the expected locations:

```bash
test -f /path/to/kneron-tutor/LiveTalking/models/wav2lip.pth
test -f /path/to/kneron-tutor/LiveTalking/data/avatars/tutor_avatar_v3/coords.pkl
```

Both commands should finish without printing an error. The entire `tutor_avatar_v3` directory is required, not only `coords.pkl`.

## 3. Create The LiveTalking Environment

Create LiveTalking's local Python environment from its repository:

```bash
cd /path/to/kneron-tutor/LiveTalking
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install torch==2.9.1 torchvision==0.24.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements.txt
```

If `python3.12`, Python virtual environments, FFmpeg, or the NVIDIA driver are unavailable, install those system prerequisites before continuing. Do not commit `.venv/`.

## 4. Configure The Tutor

Run the setup helper from the tutor repository:

```bash
cd /path/to/kneron-tutor/kneron_project
./scripts/setup-local.sh
```

On the first run, the helper creates a private root file named `.env` and exits intentionally. Edit that new file and replace the placeholder values supplied during handoff:

| Variable | Required value |
|---|---|
| `FLASK_SECRET_KEY` | A long, stable random value |
| `OPENAI_API_KEY` | Credential for the configured LLM provider |
| `LLM_BASE_URL` | OpenAI-compatible LLM API base URL |
| `TTS_BASE_URL` | Existing external TTS service base URL |
| `TTS_MODEL` / `TTS_VERSION` | Narration voice configuration |
| `TUTOR_TTS_MODEL` / `TUTOR_TTS_VERSION` | Tutor avatar voice configuration |

Leave the tested WhisperLiveKit values unchanged unless deliberately upgrading that service. `HF_TOKEN` may remain empty because the selected model is public.

Run the helper again after editing `.env`:

```bash
./scripts/setup-local.sh
```

Setup is ready only when every reported check begins with `[ok]` and the script ends with `Local prerequisites are ready`. The helper validates Docker, the GPU, environment values, repository paths and versions, LiveTalking assets, and the Compose configuration. It does not start any service.

The root `.env` is ignored by Git. Do not add it to a commit or send it through a public channel. A legacy `backend/.env` is not used by the handoff configuration and should not be created.

## 5. Install The LiveTalking Service

Make sure no manually started LiveTalking process is using port `8010`, then run:

```bash
cd /path/to/kneron-tutor/kneron_project
./scripts/install-livetalking-service.sh
```

The installer creates and enables a systemd user service. Its supervisor waits through model warm-up, checks `http://127.0.0.1:8010/health`, and restarts LiveTalking if it exits or stops responding. Follow its first startup with:

```bash
journalctl --user -u kneron-livetalking.service -f
```

Continue after this succeeds:

```bash
curl -fsS http://127.0.0.1:8010/health
```

## 6. Start The Compose Services

Run:

```bash
cd /path/to/kneron-tutor/kneron_project
docker compose up -d --build
docker compose ps
```

The first run builds the tutor, Manim, and WhisperLiveKit images and may download the Whisper `base` model. Wait until `whisperlivekit` reports `healthy`. The expected Compose services are:

```text
backend
manim-agent
redis
whisperlivekit
```

Confirm WhisperLiveKit is ready:

```bash
curl -fsS http://127.0.0.1:8002/health
```

Expected response:

```json
{"status":"ok","backend":"faster-whisper","ready":true}
```

## 7. Open And Verify The Tutor

Open <http://localhost:5000> in a browser. Opening the page proves only that the tutor frontend and backend are available; complete all of these checks:

1. Sign in and open or generate a lesson.
2. Play lesson narration to verify the external TTS service.
3. Open the tutor and confirm `tutor_avatar_v3` video connects.
4. Ask a typed question and confirm the avatar speaks the response.
5. Interrupt the avatar and confirm stale speech stops.
6. Use the microphone and confirm live transcription appears.
7. Close and reopen the tutor to confirm the avatar session disconnects cleanly.

Useful logs:

```bash
docker compose logs -f backend whisperlivekit
journalctl --user -u kneron-livetalking.service -f
```

Press `Ctrl+C` to stop following logs; the containers continue running.

Run the post-restart TTS and avatar smoke test from the tutor repository:

```bash
../LiveTalking/.venv/bin/python scripts/smoke-services.py
```

It waits for backend and avatar readiness, validates five real WAV responses, establishes a WebRTC connection with audio and video tracks, and cleans up the avatar session. A nonzero exit means the restart is not ready for users.

## Everyday Startup

The managed LiveTalking service starts at login. Start the Compose services with:

```bash
cd /path/to/kneron-tutor/kneron_project
docker compose up -d
```

Open <http://localhost:5000>.

Verify or restart the avatar service when needed:

```bash
systemctl --user status kneron-livetalking.service
systemctl --user restart kneron-livetalking.service
```

## Updating The Tutor

The tutor application is delivered through its `main` branch. Pull and rebuild it with:

```bash
cd /path/to/kneron-tutor/kneron_project
git pull --ff-only origin main
docker compose up -d --build
```

This updates the tutor and rebuilds its Compose-managed services. It does not update the separately pinned LiveTalking tag or WhisperLiveKit commit.

## Shutdown

Stop the Compose services from the tutor repository:

```bash
docker compose down
```

Stop LiveTalking by pressing `Ctrl+C` in its terminal.

Tutor accounts, uploads, and lessons persist under `kneron_project/backend/data/`. The Whisper model persists in the Docker volume named `whisperlivekit-hf-cache`. Avoid `docker compose down -v` unless the Whisper model cache should also be deleted.

## Troubleshooting

### Port 8002 Is Already In Use

An older standalone WhisperLiveKit container may still own the port. Find it with:

```bash
docker ps --filter publish=8002
```

If the container is specifically the obsolete standalone container named `whisperlivekit`, replace it with the Compose-managed service:

```bash
docker stop whisperlivekit
docker rm whisperlivekit
docker compose up -d --build whisperlivekit
```

Removing that container does not remove its named model-cache volume.

### WhisperLiveKit Does Not Become Healthy

```bash
docker compose logs --tail=200 whisperlivekit
curl -v http://127.0.0.1:8002/health
```

The first model download and initialization can take several minutes.

### Avatar Does Not Connect

Confirm LiveTalking is still running in its terminal and listening on port `8010`. Also confirm the startup command uses `--avatar_id tutor_avatar_v3`.

### Narration Or Avatar Speech Fails

TTS is external and is not a Compose service. Confirm `TTS_BASE_URL` and the TTS model/version values in the root `.env`, then inspect backend logs:

```bash
docker compose logs --tail=200 backend
```

### Compose Cannot Find WhisperLiveKit

Confirm the repositories are siblings and retain the expected directory names. From `kneron_project`, this file must exist:

```text
../WhisperLiveKit/Dockerfile.cpu
```

### Setup Reports The Wrong WhisperLiveKit Commit

Restore the tested revision:

```bash
git -C ../WhisperLiveKit checkout ed571b69099d089a04f15b7690bbcae6aa2cc54b
git -C ../WhisperLiveKit submodule update --init --recursive
```

## Local Port Reference

| Port | Service | Managed by |
|---|---|---|
| `5000` | Tutor application | Docker Compose |
| `8000` | Manim agent | Docker Compose |
| `8002` | WhisperLiveKit | Docker Compose |
| `8010` | LiveTalking | Local Python process |

## Condensed First-Time Command Sequence

The following is the command-only version of the first-time setup. It assumes the two LiveTalking asset files have been placed in `~/kneron-handoff-assets` and the real environment values are available through the private handoff channel.

```bash
cd ~
mkdir kneron-tutor
cd kneron-tutor

git clone --branch main https://github.com/zecila/kneron_ai_tutoring.git kneron_project
git clone --branch kneron-handoff-v1 https://github.com/zecila/LiveTalking.git LiveTalking
git clone https://github.com/QuentinFuxa/WhisperLiveKit.git WhisperLiveKit
git -C WhisperLiveKit checkout ed571b69099d089a04f15b7690bbcae6aa2cc54b
git -C WhisperLiveKit submodule update --init --recursive

cd ~/kneron-handoff-assets
sha256sum -c livetalking-assets-v1.sha256
tar -xzf livetalking-assets-v1.tar.gz -C ~/kneron-tutor/LiveTalking
test -f ~/kneron-tutor/LiveTalking/models/wav2lip.pth
test -f ~/kneron-tutor/LiveTalking/data/avatars/tutor_avatar_v3/coords.pkl

cd ~/kneron-tutor/LiveTalking
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install torch==2.9.1 torchvision==0.24.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements.txt
deactivate

cd ~/kneron-tutor/kneron_project
./scripts/setup-local.sh
nano .env
./scripts/setup-local.sh
```

After setup reports that all prerequisites are ready, start LiveTalking in terminal 1:

```bash
cd ~/kneron-tutor/LiveTalking
source .venv/bin/activate
python app.py --transport webrtc --model wav2lip --avatar_id tutor_avatar_v3
```

Start the Compose services in terminal 2:

```bash
cd ~/kneron-tutor/kneron_project
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8002/health
```

Open <http://localhost:5000> and complete the verification workflow in [Open And Verify The Tutor](#7-open-and-verify-the-tutor).

## Final Acceptance Checklist

- `./scripts/setup-local.sh` reports all checks as `[ok]`
- `docker compose ps` lists all four Compose services
- WhisperLiveKit reports `healthy`
- The tutor loads at <http://localhost:5000>
- Lesson generation completes
- Narration works through the external TTS service
- Microphone transcription works through WhisperLiveKit
- `tutor_avatar_v3` connects, speaks, interrupts, and disconnects
- Tutor data remains available after stopping and restarting both systems
