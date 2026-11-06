# Deploying transcript-and-frames

A FastAPI service with two jobs:

- `POST /v1/translate` — pull a video's audio from Cloudinary, transcribe it via Groq
  Whisper, return English text.
- `POST /v1/frames` — compute sampling timestamps and Cloudinary frame URLs for a video.
  **Pure arithmetic, no network calls**, so it works offline and costs nothing.

The Yteens backend calls both during a moderated upload. `MODERATION_INTEGRATION.md`
describes that contract; `ARCHITECTURE.md` covers the internals.

---

## 1. What it needs

| Dependency | For | Free tier? |
|---|---|---|
| Groq API key | Whisper ASR on `/v1/translate` | yes |
| Cloudinary API secret | signing delivery URLs for private assets | yes |

`/v1/frames` needs neither. If you only want frame sampling, the service runs with no
credentials at all.

**Resource profile:** no ffmpeg, no model weights, no build toolchain. The image is
~120 MB and the container's resident set ~80 MB, which is what lets it run on a 512 MB
free instance.

---

## 2. Configuration

`.env.example` lists every setting with its default. Copy it to `.env`. **`.env` is
git-ignored and must stay that way** — it holds the API key, the Groq key and the
Cloudinary secret.

**Every setting has a working default and the process boots with none of them set.** That
is deliberate, and it is also the trap: booting successfully does not mean it is
configured. `GET /health` is the check that tells you the truth.

### The ones that matter

| Variable | Default | Set it to |
|---|---|---|
| `API_KEY` | `""` | a long random value. **While empty, every `/v1` route answers 503** |
| `GROQ_API_KEY` | `""` | your Groq key. Empty means `/v1/translate` cannot transcribe |
| `CLOUDINARY_API_SECRET` | `""` | needed only for signed/private delivery URLs |
| `TRUST_PROXY_HEADER` | `true` | **must** be true behind Render/Koyeb/any proxy, or every caller looks like the proxy |
| `ALLOWED_IPS` | `""` | optional allowlist, e.g. `203.0.113.7,198.51.100.0/24` |
| `ALLOWED_ORIGINS` | `""` | leave empty — both routes are called server-to-server, not from a browser |
| `ENABLE_DOCS` | `true` | consider `false` in production |
| `ENABLE_DEMO` | **`false` in code**, `true` in `.env.example` | see below |
| `DEBUG_ERRORS` | `false` | keep false; true leaks internals in responses |

`API_KEY` here must match `TRANSCRIPT_SERVICE_API_KEY` in the backend's `.env`. A mismatch
shows up as every upload failing moderation, because the backend cannot get a transcript.

### Tuning you can usually leave alone

Frame sampling (`FRAME_A`, `FRAME_FMIN`, `FRAME_FMAX`, `MAX_ALLOWED_DURATION_MIN`,
`SIGMA_L`, `SIGMA_R`), audio handling (`AUDIO_TRANSFORMATION`, `MAX_AUDIO_BYTES`),
concurrency (`MAX_CONCURRENT_ASR`) and HTTP timeouts are all documented in
`.env.example` with the formula behind them.

One worth knowing: **`FRAME_FMIN` is 8.** The backend's frame-group moderation assumes a
video yields at least 8 frames — it requires a *fraction* of frames to be flagged before
rejecting, and the fraction is what stops one false positive sinking a clean video.
Lowering `FRAME_FMIN` weakens that. The backend has its own floor as a backstop, but the
two are coupled; see `services/moderationPipeline.js` there.

---

## 3. Two things that fail quietly

**`CONFIG_ENV_FILE` resolves relative to the current working directory.** Start the
service **from the project root**. Launched from elsewhere it silently finds no `.env`,
and `/health` then reports `auth_configured: false` while every `/v1` route answers 503 —
which reads as a broken deployment rather than a wrong working directory.

**`ENABLE_DEMO` disagrees with itself.** `app/config.py` defaults it to `False`;
`.env.example` ships `true`. Deleting the line therefore turns `/demo` off even though the
example file implied it was on. Set it explicitly either way.

---

## 4. Deploying

### Docker (any platform)

```bash
docker build -t transcript-frames .
docker run -p 8000:8000 --env-file .env transcript-frames
```

Or with compose, which mirrors Render's free-tier limits (512 MB, 0.1 CPU) so local
behaviour matches production:

```bash
docker compose up -d
docker compose logs -f
```

The image runs unprivileged as uid 10001 and reads `$PORT` from the environment, so it
drops onto Render/Koyeb/Fly without changes.

### One worker, deliberately

The `CMD` pins `--workers 1`. 0.1 CPU cannot usefully serve two, and concurrency is
already bounded in-process by a semaphore sized to the Groq rate limit
(`MAX_CONCURRENT_ASR`). Raising the worker count multiplies that bound and will get you
rate-limited rather than faster.

### Keep-alive

`.github/workflows/keepalive.yml` pings the service so a free instance does not cold-start
on the first real request. A cold start was measured at 60s+, and the backend's moderation
**fails closed** — a sleeping service rejects every upload. Keep this workflow enabled if
you deploy somewhere that sleeps.

---

## 5. Verifying a deploy

```bash
curl https://<your-host>/health
```

```json
{"status":"healthy","auth_configured":true,"asr_configured":true,
 "cloudinary_signing":true,"ip_allowlist_active":false,
 "native_transcript_enabled":false}
```

`/health` is unauthenticated by design, so monitoring needs no key. Check
`auth_configured` and `asr_configured` are **true** — if they are false the service is up
but will refuse or fail every real request.

Then the cheap end-to-end check, which uses no ASR quota:

```bash
curl -X POST https://<your-host>/v1/frames \
  -H "x-api-key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"cloudinary_url":"https://res.cloudinary.com/demo/video/upload/sample.mp4","duration_sec":60}'
```

That exercises auth and the sampling maths without touching Groq.

---

## 6. Note on repository contents

`.gitignore` excludes `tests/`, `scripts/` and `pytest.ini` from version control. They
exist on disk and still work locally, but **a fresh clone will not contain them** — so
nobody else can run the test suite, and CI cannot either.

That was a deliberate choice to keep the repository to what builds and deploys, and
`.dockerignore` already keeps them out of the image regardless. It is worth revisiting:
excluding tests from the *image* is right, excluding them from *version control* costs you
the ability to verify a change anywhere but this machine.
