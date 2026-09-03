# Internal server deployment

This deployment serves the application at `https://10.200.210.28:8444`. Caddy
is the only service published on the host; Flask, Manim, Redis, and
WhisperLiveKit remain on the private Compose network.

## 1. Create the checkouts

The two repositories should be adjacent so the default relative build path
resolves correctly:

```text
parent-directory/
|-- kneron_ai_tutoring/
`-- WhisperLiveKit/
```

From the directory that will contain both repositories:

```bash
git clone https://github.com/zecila/kneron_ai_tutoring.git
git clone https://github.com/QuentinFuxa/WhisperLiveKit.git
git -C WhisperLiveKit checkout --detach ed571b69099d089a04f15b7690bbcae6aa2cc54b
```

If a checkout already exists, verify it instead of cloning over it:

```bash
git -C WhisperLiveKit rev-parse HEAD
```

## 2. Create the private environment file

From the application project root:

```bash
cp deploy/server.env.example .env
chmod 600 .env
openssl rand -hex 32
```

Edit `.env` and replace `FLASK_SECRET_KEY`, `OPENAI_API_KEY`, and
`TTS_BASE_URL` with the real values. Use the random output from the final
command as `FLASK_SECRET_KEY`. The `.env` file is ignored by Git and must not
be committed or shared in chat or logs.

`HF_TOKEN` can remain empty when the configured Whisper model is publicly
downloadable. The server must be able to reach GitHub and Hugging Face during
the initial checkout/build, plus the configured LLM and TTS services while the
application is running.

## 3. Validate without starting containers

```bash
docker compose -f docker-compose.yml -f docker-compose.server.yml config --quiet
```

The server uses Docker Compose 2.29.7, which supports the `!reset` directives
that remove the development host-port mappings.

## 4. Build and start

```bash
docker compose -f docker-compose.yml -f docker-compose.server.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.server.yml ps
```

Watch startup without printing the environment file:

```bash
docker compose -f docker-compose.yml -f docker-compose.server.yml logs -f caddy backend whisperlivekit
```

Whisper's first startup can take several minutes while it downloads and caches
the model. Caddy creates a certificate from its internal CA, so browsers will
show a certificate warning until that CA is installed as trusted.

## 5. Smoke test

On the server:

```bash
curl -k https://10.200.210.28:8444/api/health
curl -k https://10.200.210.28:8444/api/config
```

Expected responses include `{"status":"ok"}` and
`{"avatar_enabled":false}`. Then open `https://10.200.210.28:8444` from a
different machine on the internal network and accept the certificate warning.
