# Architecture

Every component of this service and how the parts interact. For what the API
*does*, see [README.md](README.md); this document is about how it is built.

The governing constraint is that the whole thing must run on a free 512 MB /
0.1 CPU instance. Every structural decision below follows from that.

---

## 1. Deployment topology

```
┌─ your upload backend ──┐
│  (holds the API key)   │──┐
└────────────────────────┘  │   HTTPS + x-api-key
                            │
┌─ /demo page in a browser ─┤
│  (key typed in by hand)   │
└───────────────────────────┘
                            ▼
              ┌──────────────────────────────────────┐
              │  ONE uvicorn process, ONE worker     │
              │  python:3.11-slim · ~80 MB resident  │
              │  Render free: 512 MB / 0.1 CPU       │
              │                                      │
              │  no model · no ffmpeg · no disk      │
              │  no database · no queue · no cache   │
              └───────┬──────────────────────┬───────┘
                      │                      │
         ┌────────────▼─────────┐  ┌─────────▼──────────────┐
         │ Cloudinary           │  │ Groq                   │
         │ res.cloudinary.com   │  │ api.groq.com           │
         │ video→mp3, video→jpg │  │ whisper-large-v3       │
         └──────────────────────┘  └────────────────────────┘
```

Five runtime dependencies — `fastapi`, `uvicorn`, `httpx`, `pydantic`,
`pydantic-settings` — about 2,500 lines of application code and 1,200 lines of
tests.

One worker is deliberate. There is no longer a model to duplicate across
workers, but 0.1 CPU cannot usefully serve two, and in-process concurrency is
already bounded by a semaphore sized to the provider's rate limit.

---

## 2. Startup, once per process

[`app/main.py`](app/main.py) builds the app in `create_app()` and the
process-wide objects in the `lifespan` handler.

| Step | What is built | Why it happens once |
|---|---|---|
| `get_settings()` | `Settings`, `lru_cache`'d | environment and `.env` are read a single time |
| `_configure_logging` | stdlib logging at `LOG_LEVEL` | — |
| `httpx.AsyncClient` | shared pool, `follow_redirects=False` | a client per request would discard connection pooling and TLS session reuse, a measurable share of latency at 0.1 CPU |
| `httpx.Limits` | `max_connections = MAX_CONCURRENT_ASR * 2 + 6` | each in-flight translation holds two connections at once, Cloudinary and the provider, so the pool is sized above that |
| `asyncio.Semaphore` | `MAX_CONCURRENT_ASR`, default 3 | sized by Groq's 20 requests/minute, not by our memory |
| `InlineRunner` ×2 | one per feature | the queue seam (§8) |
| startup warnings | logged if `API_KEY` or `GROQ_API_KEY` is unset | misconfiguration surfaces at boot, not at the first real request |

Everything is stored on `app.state`, and the pipeline handlers read the client
and settings **off `app.state`** rather than closing over them. That is what
allows a test to swap in an `httpx.MockTransport` client after startup without
rebuilding the runners. On shutdown the client is closed.

Nothing else is warmed up, so a cold start is essentially process spawn — which
matters because the free tier spins the instance down after 15 minutes idle and
whoever arrives next pays for the wake-up.

---

## 3. Every file and its responsibility

```
app/
  main.py                 app factory · lifespan · CORS · 2 exception handlers · router wiring
  config.py               every env var, with free-tier defaults, plus derived properties
  errors.py               typed errors: HTTP status + stable code + safe public message
  security.py             the API-key and CIDR-allowlist dependency
  deps.py                 request-scoped accessors onto app.state

  cloudinary_delivery/    deliberately not named `cloudinary`, so it can never shadow
                          the PyPI package of that name
    parse.py   343 ln     THE SSRF BOUNDARY — validates and decomposes delivery URLs
    derive.py  151 ln     rebuilds them as audio / frame URLs, with optional signing

  routers/
    meta.py               GET /            GET /health          open
    demo.py               GET /demo                             open, gated by ENABLE_DEMO
    translate.py          POST /v1/translate                    keyed
    frames.py             POST /v1/frames                       keyed

  services/
    frame_math.py         the two formulas — pure functions, no I/O, no dependencies
    frame_pipeline.py     metadata → frame count → timestamps → URLs
    frame_sink.py         FrameSink protocol: where the frames are handed over
    audio_proxy.py        constant-memory streaming and the multipart envelope
    asr.py                AsrProvider protocol + GroqProvider (the only provider code)
    translate_pipeline.py URL → audio → transcript, including the spool fallback
    runner.py             Runner protocol + InlineRunner

  static/demo.html        740 ln test console: one file, no build step, no CDN
```

---

## 4. Cross-cutting layers

### Configuration

`Settings` is read once and then passed **down as an argument**. No module
reaches for a global; pipelines accept `settings` explicitly. That is why tests
can vary configuration per case rather than mutating shared state.

### Access control

[`security.py:require_caller`](app/security.py) is attached at the **router**
level via `APIRouter(dependencies=[...])`, not per endpoint, so a new route added
under `/v1` cannot be left unguarded by forgetting a decorator. Three checks, in
order:

1. `API_KEY` empty → `503 auth_not_configured`. The service **fails closed**; it
   cannot accidentally run open.
2. `x-api-key` compared with `secrets.compare_digest`, so the check leaks neither
   the secret's length nor its content through timing.
3. If `ALLOWED_IPS` is non-empty, the caller must fall inside one of those
   networks, resolved from the left-most `X-Forwarded-For` entry when
   `TRUST_PROXY_HEADER` is on. **Off by default**, because free hosting
   providers rotate egress IPs without notice and this would silently 403 the
   integration.

`/`, `/health` and `/demo` are intentionally open: the keep-alive cron cannot
hold a key, and none of the three reveals a secret or spends any quota.

### Error handling

Every failure path raises an `AppError` subclass carrying a status, a stable
machine-readable `code` and a safe `public_message`. One handler in `main.py`
assigns a `correlation_id`, logs the code and the real detail against it, and
returns only:

```json
{"success": false, "error": "...", "message": "...", "correlation_id": "..."}
```

The detail is withheld unless `DEBUG_ERRORS=true`, because upstream messages can
contain signed URLs and provider internals. A second handler catches anything
unexpected as a logged `500`.

### Concurrency

A single semaphore, acquired inside `InlineRunner.submit`, wrapping only the ASR
work. URL parsing, frame mathematics and JSON serialisation stay free on the
event loop, so a request waiting on Groq never blocks a frames request.

---

## 5. Request flow: `POST /v1/translate`

```
router (translate.py)
  │  pydantic validates the body → TranslateRequest
  │  require_caller has already passed
  ▼
InlineRunner.submit(TranslateJob)          ← acquires the ASR semaphore (max 3)
  ▼
run_translation()  [translate_pipeline.py]
  │
  ├─1 include_native && !ENABLE_NATIVE_TRANSCRIPT → 400 feature_disabled
  │     the flag is rejected rather than silently ignored
  │
  ├─2 parse_delivery_url()                [parse.py]   ◄── SSRF boundary
  │     https only · host ∈ {res.cloudinary.com, res-N.cloudinary.com, your CNAMEs}
  │     no userinfo · no explicit port · no IP literal · video resource type only
  │     remote (fetch) delivery refused unless ALLOW_FETCH_DELIVERY
  │     → CloudinaryAsset(cloud, delivery, signature, transformations,
  │                        version, public_id, extension, query)
  │
  ├─3 build_audio_url()                   [derive.py]
  │     appends af_16000,br_32k · extension → .mp3 (or f_mp3 for fetch sources)
  │     re-signs when CLOUDINARY_API_SECRET is set
  │     forwards allowlisted query params byte-for-byte
  │
  ├─4 open_audio()                        [audio_proxy.py]
  │     GET with follow_redirects=False
  │     every 3xx hop → assert_host_allowed() → re-validated, max 3 hops
  │     Content-Length over the cap → 413 before a single body byte is read
  │     status ≥ 400 → 502 upstream_media_error
  │
  ├─5 iter_capped(response.aiter_bytes(64 KB))
  │     counts bytes and aborts mid-stream past the cap
  │     Content-Length is a claim by the server; this is the fact
  │
  ├─6 multipart_stream()
  │     wraps those chunks in a form-data envelope
  │     peak memory = ONE CHUNK, whatever the file size
  │
  ├─7 GroqProvider.translate_streaming()  [asr.py]
  │     POST api.groq.com/openai/v1/audio/translations
  │     Authorization: Bearer · model=whisper-large-v3
  │     response_format=verbose_json · temperature=0
  │     body = the async generator → chunked transfer encoding
  │
  ├─8 _handle() maps the reply
  │     200       → _parse_verbose_json → text, language, duration,
  │                  mean(avg_logprob)
  │     411 / 400-length → ChunkedUploadRejected ───┐
  │     429       → 503 asr_rate_limited + Retry-After
  │     413       → 413 audio_too_large             │
  │     401/403   → 502 asr_unavailable             │
  │     5xx       → 502 asr_unavailable             │
  │                                                 │
  └─9 fallback, attempted once ◄────────────────────┘
        _translate_spooled()
          re-fetch — Cloudinary has the derived audio CDN-cached by now
          → SpooledTemporaryFile: RAM below SPOOL_THRESHOLD_BYTES, disk above
          → translate_file() with files=, so httpx sets Content-Length
          → the response reports upload_path "spooled" instead of "streamed"
```

When `ENABLE_NATIVE_TRANSCRIPT` is on, `include_native` adds a **second** fetch
and a `transcribe_streaming` call against turbo. That doubling of audio-quota
consumption is exactly why it is off by default.

---

## 6. Request flow: `POST /v1/frames`

No network calls at all. Roughly 0.6 ms end to end.

```
router (frames.py)
  │  pydantic enforces duration_sec > 0 at the schema level (422)
  ▼
InlineRunner.submit(FrameJob)              ← NO semaphore; this is arithmetic
  ▼
run_frames()  [frame_pipeline.py]
  ├─ duration_sec ≤ 0 → 400 invalid_duration
  │  over the maximum && REJECT_OVER_MAX_DURATION → 400 duration_too_long
  │
  ├─ parse_delivery_url()                  the same boundary as translate
  │
  ├─ compute_frame_count(duration/60)      [frame_math.py]
  │    mu    = MAX_ALLOWED_DURATION_MIN / 2            = 5 min
  │    sigma = SIGMA_L 2.0 if d ≤ mu else SIGMA_R 4.0
  │    clamp(round(60 · exp(-(d-mu)² / 2σ²)), 8, 60)
  │
  ├─ compute_frame_timestamps(T, N)
  │    t_i = T/2 + (T/6) · Φ⁻¹((i + 0.5)/N)
  │    → clamp [0, T] → round to 3 dp → order-preserving dedupe
  │    Φ⁻¹ = statistics.NormalDist().inv_cdf           ← stdlib, not scipy
  │
  ├─ build_frame_url() per timestamp       [derive.py]
  │    so_<t>,q_80[,w_<n>,c_limit] → .jpg  (or f_jpg for fetch sources)
  │
  └─ FrameSink.deliver()                   [frame_sink.py]
       UrlListSink (default) → {"delivery": "url"}, moves no image bytes
```

`requested_frame_count` and `frame_count` are reported separately on purpose:
millisecond de-duplication can shorten the list when two quantiles collide,
which happens on very short videos.

---

## 7. The external contracts, exactly

```
Cloudinary — GET, none of our credentials are sent
  audio   /<cloud>/video/upload/[s--sig--/][existing transforms/]af_16000,br_32k/[vN/]<public_id>.mp3
  frame   /<cloud>/video/upload/[s--sig--/][existing transforms/]so_42.090,q_80/[vN/]<public_id>.jpg

Groq — POST with a Bearer key
  /openai/v1/audio/translations     model=whisper-large-v3        ← translation
  /openai/v1/audio/transcriptions   model=whisper-large-v3-turbo  ← native, gated off
```

Byte accounting:

| | through this service |
|---|---|
| the video | **0** — never downloaded |
| the audio (translate) | ~2.4 MB per 10-minute video, in 64 KB chunks, never fully resident |
| the frames | **0** — consumers fetch from Cloudinary's CDN |
| the response | 2–20 KB of JSON |

Cloudinary is asked for 16 kHz at 32 kbps because Whisper downsamples to 16 kHz
mono regardless, so the reduction costs nothing in accuracy while cutting
transit bytes roughly fourfold.

---

## 8. The seams

Extension points that exist as working code rather than as plans:

| Seam | Where | What plugs in |
|---|---|---|
| `Runner` protocol | [`runner.py`](app/services/runner.py) | a `QueuedRunner` returning a job id, plus `/v1/jobs/{id}`. Pipelines, validation and response models stay untouched because they only ever talk to a `Runner`. |
| `AsrProvider` protocol | [`asr.py`](app/services/asr.py) | another hosted API, or a local `faster-whisper` if the service ever gets a bigger box. Three methods. |
| `FrameSink` protocol | [`frame_sink.py`](app/services/frame_sink.py) | `BytesForwardSink` or `LocalFileSink`, once the remote analyzer's contract is known. Nothing about that contract has been invented. |
| router isolation | [`main.py`](app/main.py) | deleting one `include_router` line splits either feature into its own deployment; they share only config, the HTTP client and the URL parser. |

---

## 9. Supporting, non-runtime

- **119 tests**, fully offline — both upstreams are served by
  `httpx.MockTransport`, so no network and no quota is used.
- [`scripts/smoke.py`](scripts/smoke.py) — the live counterpart, and the only
  thing that can reveal whether the Cloudinary account needs
  `CLOUDINARY_API_SECRET`.
- `Dockerfile` — `python:3.11-slim` matching local Python, non-root uid 10001,
  one worker, `$PORT` honoured.
- `.github/workflows/keepalive.yml` — pings `/health` every 10 minutes inside a
  daily window, dodging the 15-minute spin-down without consuming all 750
  monthly instance-hours.
- `.env.example`, `docker-compose.yml`, `pytest.ini`.

---

## 11. Constraints to preserve

Properties that are deliberate rather than incidental, and that a later change
should not quietly undo:

- **No content inspection of the transcript.** Classification, filtering,
  flagging and redaction belong to a separate existing service.
- **Nothing about the remote frame-analysis contract is invented.** Its endpoint,
  schema, auth and transport are unknown here;
  [`frame_sink.py`](app/services/frame_sink.py) is where they plug in.
- **No resident model, ffmpeg, scipy/numpy, local media storage, or multiple
  workers.** Before adding a dependency, check whether Cloudinary or the ASR
  provider can do the work instead.
- **Free tiers only.** Render free (512 MB / 0.1 CPU), Groq's free tier,
  Cloudinary's free plan. Quotas, spin-down behaviour and instance-hour caps are
  the real limits.
- **`.env` is never committed, printed or read by tooling.** Required keys are
  documented in `.env.example`.

---

## 10. Deliberately absent

No database, no Redis, no job queue, no background tasks, no caching layer
(Cloudinary's CDN is the cache), no ffmpeg or ffprobe, no model weights, no
scipy or numpy, no persistent storage, no multiple workers, and no
retry-with-backoff beyond the single spool fallback.

Also absent by design: **any inspection of the transcript's content**. No
classification, filtering, flagging or redaction, and no behaviour that depends
on what was said. A separate existing service owns that question;
`/v1/translate` returns the text and nothing more.
