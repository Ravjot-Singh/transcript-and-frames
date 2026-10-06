# Transcript and Frames API

Two independent capabilities over Cloudinary-hosted video, in one small FastAPI
service that runs on a free 512 MB / 0.1 CPU instance:

| Route | In | Out |
|---|---|---|
| `POST /v1/translate` | a Cloudinary video URL | the spoken content as English text, source language auto-detected |
| `POST /v1/frames` | a Cloudinary video URL + its duration | one Cloudinary image URL per sampled frame |

Neither route depends on the other. Deleting one `include_router` line in
[`app/main.py`](app/main.py) splits either into its own deployment.

Component-by-component internals are in [ARCHITECTURE.md](ARCHITECTURE.md);
Render setup and a brief for integrating this into another backend are in
[DEPLOY_AND_INTEGRATE.md](DEPLOY_AND_INTEGRATE.md).

## How it stays small

All the expensive work is delegated to services that already do it for free:

```
                      ┌─ Cloudinary ── video → mp3,  video → jpg
  caller ──► this API ─┤
                      └─ Groq ─────── whisper-large-v3 (translate)
```

This process holds **no model**, runs **no ffmpeg**, spawns **no subprocess**,
and writes **no media to disk**. Five dependencies total; ~80 MB resident.

What actually crosses the wire per request:

| | bytes through this service |
|---|---|
| the video | **0** — never downloaded |
| the audio | ~2.4 MB for a 10-minute video, streamed in 64 KB chunks, never fully in memory |
| the frames | **0** — callers fetch them from Cloudinary's CDN |

Three decisions earn most of that:

- **Cloudinary derives the audio.** Asking for `.mp3` with `af_16000,br_32k` makes
  Cloudinary transcode, and since Whisper downsamples to 16 kHz mono anyway, the
  lower bitrate costs nothing in accuracy while cutting transit bytes ~4×.
- **Cloudinary renders the frames.** `so_<seconds>` delivers a still at any
  timestamp, cached on its CDN afterwards. This replaces one ffmpeg subprocess
  per frame — up to 60 of them — and with it every per-frame timeout and failure
  path.
- **The inverse-normal quantile comes from the standard library.**
  `statistics.NormalDist().inv_cdf` matches `scipy.stats.norm.ppf` to ~1e-15, so
  dropping scipy removed scipy + numpy: about 100 MB of wheels and resident
  memory, for identical numbers.

## Running it, step by step

From a fresh clone to a working service. The container is what gets deployed, so
running it this way tests the real artifact rather than an approximation.

### Step 1 — prerequisites

- **Docker** — `docker --version` should print a version, and `docker info` should
  print a server version. If not, start Docker Desktop.
- **A Cloudinary account** (free) with at least one video uploaded.
- **A Groq API key** (free, no card) — obtained in step 2.

Nothing else. No Python installation is needed to run the service: the image
carries its own.

### Step 2 — get a Groq API key

1. Sign up at <https://console.groq.com>
2. Open <https://console.groq.com/keys>
3. **Create API Key**, and copy it immediately — it is shown only once
4. It looks like `gsk_...`

Free-tier ceilings for `whisper-large-v3`: 20 requests/minute, 2,000 per day,
7,200 audio-seconds per hour and 28,800 per day. That is roughly 48 ten-minute
videos a day.

### Step 3 — create `.env`

```bash
cp .env.example .env
```

```powershell
Copy-Item .env.example .env
```

Fill in two values. Nothing else is required — every other setting has a working
default, documented in `.env.example`.

```
API_KEY=<a strong random string you invent>
GROQ_API_KEY=gsk_...
ENABLE_DEMO=true
```

`API_KEY` is not issued by anyone; you choose it. It is the shared secret callers
must send as the `x-api-key` header. Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`ENABLE_DEMO=true` switches on the test console. It defaults to **false** so that
a deployment which forgets the variable has the console off rather than exposed.

Two rules for this file: **no quotes** around values (`--env-file` treats them as
part of the value), and one `KEY=value` per line. `.env` is git-ignored and never
enters the image.

Add `CLOUDINARY_API_SECRET` only if your video URLs are signed, use the
`authenticated`/`private` delivery types, or your account has **Strict
transformations** enabled. If you are unsure, skip it — step 7 will tell you.

### Step 4 — build the image

From the project root:

```bash
docker build -t transcript-frames:local .
```

About a minute the first time, a few seconds afterwards. The result is ~241 MB,
almost all of it `python:3.11-slim` plus five wheels — no model weights, no
ffmpeg.

### Step 5 — run the container

```bash
docker run -d --name tf -p 8000:8000 --env-file .env transcript-frames:local
```

`--env-file` is resolved relative to your current directory, so run this from the
project root.

To reproduce Render's free tier exactly — the measured idle footprint is ~48 MB
of 512 MB:

```bash
docker run -d --name tf --memory 512m --cpus 0.1 -p 8000:8000 \
  --env-file .env transcript-frames:local
```

### Step 6 — confirm it started

```bash
curl http://127.0.0.1:8000/health
```

```powershell
curl.exe http://127.0.0.1:8000/health
```

Both flags must be `true` before anything else will work:

```json
{"status":"healthy","auth_configured":true,"asr_configured":true, ...}
```

If `auth_configured` is false, no `API_KEY` reached the container — check for
quotes in `.env`, and that you ran `docker run` from the project root. If the
command returns nothing at all, you were just faster than startup; wait a moment
and repeat.

### Step 7 — open the test console and try both routes

Go to **<http://127.0.0.1:8000/demo>**, paste your `API_KEY` into the field, then
either click **Use a public sample** to check the plumbing against Cloudinary's
`demo` cloud, or paste one of your own video URLs with its duration. Press **Send
request** on each panel.

A `502 upstream_media_error` here means Cloudinary refused the derived URL; if
the underlying status is 401, your account has strict transformations enabled and
`CLOUDINARY_API_SECRET` is required (step 3).

### Where to go for what

| Address | What it is | Needs a key? |
|---|---|---|
| <http://127.0.0.1:8000/> | Service banner: version, features, which consoles are enabled | no |
| <http://127.0.0.1:8000/health> | Liveness and configuration flags. Makes no upstream call, so it is safe to poll | no |
| <http://127.0.0.1:8000/demo> | **Test console** — send real requests, see the transcript, the timestamp timeline and the frame thumbnails. Requires `ENABLE_DEMO=true` | typed into the page |
| <http://127.0.0.1:8000/docs> | Generated OpenAPI schema. Click **Authorize**, paste the key, and call either route from the browser | via **Authorize** |
| <http://127.0.0.1:8000/openapi.json> | The raw schema, for generating a client | no |
| `POST http://127.0.0.1:8000/v1/translate` | Speech in any language → English text | `x-api-key` |
| `POST http://127.0.0.1:8000/v1/frames` | Video metadata → Cloudinary frame URLs | `x-api-key` |

On a deployed instance, replace `http://127.0.0.1:8000` with
`https://<your-service>.onrender.com`.

### Step 8 — day-to-day commands

```bash
docker logs -f tf                     # follow the logs
docker stats tf --no-stream           # memory and CPU right now
docker stop tf                        # stop
docker start tf                       # start again
docker rm -f tf                       # remove
docker build -t transcript-frames:local .   # rebuild after a code change
```

**Environment variables are read once, at container creation.** After editing
`.env`, replace the container rather than restarting it:

```bash
docker rm -f tf && docker run -d --name tf -p 8000:8000 --env-file .env transcript-frames:local
```

```powershell
docker rm -f tf; docker run -d --name tf -p 8000:8000 --env-file .env transcript-frames:local
```

`docker restart tf` would silently reuse the old environment. If the name is
already taken, `docker rm -f tf` first — a stopped container still owns its name.

### On Windows PowerShell

`curl` is an alias for `Invoke-WebRequest`, so `curl -s <url>` prompts for `Uri`
instead of working. Use `curl.exe` — the real curl, shipped with Windows 10+ —
or `Invoke-RestMethod`, which parses the JSON for you.

PowerShell 5.1 also mangles embedded double quotes when passing arguments to a
native executable, so for any request with a JSON body use `Invoke-RestMethod`:

```powershell
$KEY = "<your API_KEY>"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/frames" `
  -Headers @{ "x-api-key" = $KEY } -ContentType "application/json" `
  -Body '{"cloudinary_url":"https://res.cloudinary.com/demo/video/upload/dog.mp4","video_id":"d","duration_sec":90}'
```

Also: `$VAR = "value"` rather than `export`, `${PWD}` rather than `$(pwd)`, and
`;` rather than `&&`.

### Running without Docker

If you would rather run it directly:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Linux/macOS: .venv/bin/python
.venv/Scripts/python -m uvicorn app.main:app --reload
```

Run it from the project root — `.env` is resolved relative to the working
directory, so started elsewhere the service comes up with no keys.

## Test console — `/demo`

A single static page, served by the service itself so it shares the API's origin
and needs no CORS configuration. It sends real requests to both routes and shows
exactly what went out and came back:

- the outgoing request with the API key masked, plus **copy as curl**
- HTTP status, browser round-trip time and the server's own `elapsed_sec`
- the transcript rendered readably, with detected language, duration and confidence
- for frames: a **timeline of the sampled timestamps**, which makes the
  inverse-normal clustering around the midpoint visible at a glance, and the
  **actual frame thumbnails** — loaded straight from Cloudinary's CDN, which
  demonstrates that no image bytes pass through the service
- errors shown with their code and `correlation_id`, so a 503 or 401 is as
  legible as a success
- optionally, **uploading a video to Cloudinary from the page**, using an
  unsigned upload preset. This mirrors the production flow — browser uploads to
  Cloudinary, the backend only ever receives a URL — and auto-fills
  `duration_sec`, `video_id` and the delivery URL from Cloudinary's response,
  which is precisely the metadata the upload backend is expected to forward.

The page holds no secret: whoever is testing types the key in and it stays in
their browser's local storage. Because a browser page cannot keep a key secret,
this is a testing tool rather than a production frontend — set `ENABLE_DEMO=false`
on any deployment strangers can reach.

## API

Both `/v1` routes require an `x-api-key` header. `/` and `/health` do not.

### `POST /v1/translate`

```bash
curl -X POST http://127.0.0.1:8000/v1/translate \
  -H "x-api-key: $API_KEY" \
  -H "content-type: application/json" \
  -d '{"cloudinary_url":"https://res.cloudinary.com/demo/video/upload/v1/clip.mp4"}'
```

```json
{
  "success": true,
  "detected_language": "hi",
  "english_text": "At the Taj Mahal in Agra, the 300th Urs of Mughal emperor Shah Jahan was inaugurated. On this occasion, the gate to the graves of Shah Jahan and Mumtaz was opened.",
  "audio_duration_sec": 41.2,
  "transcription_confidence": -0.21,
  "confidence_scale": "mean_avg_logprob",
  "provider": "groq:whisper-large-v3",
  "upload_path": "streamed",
  "elapsed_sec": 4.118
}
```

Works for any language Whisper supports — the source language is detected, never
configured. `transcription_confidence` is the mean per-segment `avg_logprob`;
closer to 0 is better.

> **There is no `language_probability`.** Hosted Whisper APIs return the detected
> language but no confidence for that detection, so the field is absent rather
> than filled with a different number wearing that name. Only a locally hosted
> `faster-whisper` exposes it, and that does not fit a free instance.

This route returns the transcript and nothing else. It performs no content
inspection, classification or filtering of what was said — that belongs to the
separate existing route that handles it.

### `POST /v1/frames`

```bash
curl -X POST http://127.0.0.1:8000/v1/frames \
  -H "x-api-key: $API_KEY" \
  -H "content-type: application/json" \
  -d '{"cloudinary_url":"https://res.cloudinary.com/demo/video/upload/v1/clip.mp4",
       "video_id":"abc123","duration_sec":90.0,"width":1920,"height":1080}'
```

```json
{
  "video_id": "abc123",
  "requested_frame_count": 13,
  "frame_count": 13,
  "timestamps_sec": [18.468, 27.024, 31.959, 35.773, 39.064, 42.09, 45.0, 47.91, 50.936, 54.227, 58.041, 62.976, 71.532],
  "frames": [
    {"index": 0, "timestamp_sec": 18.468,
     "url": "https://res.cloudinary.com/demo/video/upload/so_18.468,q_80/v1/clip.jpg"}
  ],
  "delivery": "url",
  "elapsed_sec": 0.0006
}
```

`duration_sec` comes from the Cloudinary upload response your backend already
receives — no `ffprobe`, no Admin API call.

**How many frames.** An asymmetric Gaussian over duration in minutes:

```
mu    = MAX_ALLOWED_DURATION_MIN / 2            = 5 min
sigma = SIGMA_L (2.0) if d <= mu else SIGMA_R (4.0)
F(d)  = clamp(round(60 * exp(-(d - mu)^2 / (2 sigma^2))), 8, 60)
```

Peaks at 60 frames at 5 minutes. Because `sigma_l < sigma_r` the count falls
away faster below the midpoint than above it: a 2-minute video gets
`60·exp(-9/8) ≈ 19.48` → **19**, a 7-minute video gets 53. Never below 8, never
above 60.

**Where they sit.** Inverse-normal quantiles, not uniform spacing:

```
t_i = T/2 + (T/6) * PPF((i + 0.5)/N),  clamped to [0,T], rounded to ms
```

Frames cluster around the temporal midpoint, where the representative content
usually is, and thin out toward the ends. A single frame lands exactly at `T/2`.
`frame_count` can fall below `requested_frame_count` on very short videos, when
several quantiles round to the same millisecond.

### Cloudinary URL handling

Derived URLs are rebuilt from parsed components, never by patching the original
string, so all of these work:

| Variant | Handling |
|---|---|
| chained transformations, conditionals, named transforms, layers | preserved, with the new transformation appended after them |
| folders, nested folders, percent-encoded ids, dots in the public id | preserved; only the real extension is swapped |
| no extension at all | the target extension is simply added |
| `vNNN` versions | preserved in place |
| `s--sig--` signatures, before *or* after the transformations | detected and **re-signed**, since the old signature cannot cover a new transformation. Needs `CLOUDINARY_API_SECRET`, else the request is refused rather than sent doomed |
| `authenticated` / `private` delivery | accepted |
| `fetch` and other remote types | refused unless `ALLOW_FETCH_DELIVERY=true`; when enabled the remote URL is kept verbatim (its `https://` is not mangled) and the format is set with `f_mp3` / `f_jpg` instead of touching the origin's extension |
| query strings | filtered by `CLOUDINARY_FORWARD_QUERY_PARAMS` |
| fragments | discarded — they are never sent to a server |
| uppercase hosts, trailing or doubled slashes | normalised |

**Query parameters.** Only allowlisted parameters are carried onto the derived
URL; the default is `__cld_token__`. Cloudinary's `_a=` analytics parameter and
other noise are dropped, because they describe the request that built the
original URL rather than ours. Forwarded parameters are copied **byte-for-byte**
— never decoded and re-encoded — because a delivery token is an HMAC over its
exact characters and normalising `*` to `%2A` is enough to invalidate it.

> If you use Cloudinary token authentication, generate **acl-scoped** tokens
> (e.g. `acl=/video/*`). A token minted for one exact video URL will not
> authorize its `.mp3` or `.jpg` variants, and Cloudinary's 401 surfaces here as
> `upstream_media_error`.

**Frame delivery** is a seam, not a decision:
[`app/services/frame_sink.py`](app/services/frame_sink.py) ships `UrlListSink`
(the default — returns URLs, moves zero bytes) plus documented `BytesForwardSink`
and `LocalFileSink` stubs, selected by `FRAME_SINK`:

| `FRAME_SINK` | Status | What it does |
|---|---|---|
| `url` | **working default** | Returns one Cloudinary URL per timestamp. Zero image bytes pass through this service; Cloudinary renders each still on first request and CDN-caches it afterwards. |
| `bytes` | stub | Would fetch each frame and forward the bytes to the remote analyzer. Blocked on that service's contract. Also costs 60 downloads + 60 uploads for a 60-frame video on 0.1 CPU. |
| `local` | stub | Would write `storage/frames/<video_id>/frame_<i>_<t>s.jpg`, the original naming. Unsuitable here: no persistent disk, and local copies of user media are deliberately avoided. |

A typo in `FRAME_SINK` fails at startup; the two stubs raise
`NotImplementedError` when a request reaches them.

The existing frame-analysis service lives on another machine and its contract is
not known here, so **no endpoint URL, schema or auth for it has been invented**.
Wiring it up means implementing one `deliver` method.

### Calling it from your backend

```python
import httpx

async def get_transcript(client: httpx.AsyncClient, video_url: str) -> str:
    response = await client.post(
        f"{SERVICE_URL}/v1/translate",
        headers={"x-api-key": SERVICE_API_KEY},
        json={"cloudinary_url": video_url},
        timeout=180.0,
    )
    response.raise_for_status()
    return response.json()["english_text"]
```

Both routes are independent, so fire them concurrently:

```python
frames, transcript = await asyncio.gather(
    get_frames(client, video_url, duration_sec),
    get_transcript(client, video_url),
    return_exceptions=True,   # one failing must not lose the other
)
```

```javascript
// Browser, only if you later point a frontend directly at the service and add
// its origin to ALLOWED_ORIGINS. Note a browser cannot hold the API key secretly.
const response = await fetch(`${SERVICE_URL}/v1/translate`, {
  method: "POST",
  headers: { "content-type": "application/json", "x-api-key": SERVICE_API_KEY },
  body: JSON.stringify({ cloudinary_url: cloudinaryVideoUrl }),
});
const data = await response.json();
console.log(data.english_text);
```

### Errors

Every failure returns `{"success": false, "error": "<code>", "message": ..., "correlation_id": ...}`.
The real cause is logged against that id and never put in the body unless
`DEBUG_ERRORS=true`, because upstream messages can contain signed URLs.

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | missing or wrong `x-api-key` |
| `forbidden_origin` | 403 | caller IP outside `ALLOWED_IPS` |
| `auth_not_configured` | 503 | no `API_KEY` set — the service fails closed |
| `invalid_cloudinary_url` | 400 | not an https Cloudinary video delivery URL |
| `unsupported_cloudinary_url` | 400 | Cloudinary's, but a shape this service won't handle |
| `redirect_rejected` | 400 | a redirect pointed off the Cloudinary allowlist |
| `invalid_duration` / `duration_too_long` | 400 | bad `duration_sec` |
| `feature_disabled` | 400 | `include_native` while `ENABLE_NATIVE_TRANSCRIPT=false` |
| `audio_too_large` | 413 | derived audio over `MAX_AUDIO_BYTES` |
| `upstream_media_error` | 502 | Cloudinary refused the derived URL |
| `asr_rate_limited` | 503 + `Retry-After` | provider quota exhausted |
| `asr_unavailable` | 502 | provider outage or bad credentials |

## Security

- **API key on both `/v1` routes**, compared with `secrets.compare_digest`. The
  service answers 503 rather than serving anything when `API_KEY` is unset, so it
  cannot accidentally run open.
- **Optional `ALLOWED_IPS`** CIDR allowlist, off by default — free hosting
  providers rotate egress IPs without notice, which would silently 403 your
  integration.
- **SSRF boundary.** The only URLs this service will fetch are ones that survived
  [`parse_delivery_url`](app/cloudinary_delivery/parse.py): https only, host
  exactly `res.cloudinary.com` / `res-N.cloudinary.com` / a configured CNAME, no
  credentials in the authority, no explicit port, no IP literals. Redirects are
  followed manually and every hop is re-validated against the same allowlist.
  Cloudinary's `fetch` delivery type, which would make Cloudinary pull an
  arbitrary third-party URL, is refused unless `ALLOW_FETCH_DELIVERY=true`.
- **Size limits are enforced twice** — `Content-Length` up front, then a running
  byte count that aborts mid-stream, because `Content-Length` is a claim.
- **Signed URLs.** If your Cloudinary account enables *strict transformations*,
  derived URLs need signing: set `CLOUDINARY_API_SECRET` and it happens
  automatically. The tell is a 401 from Cloudinary surfacing as
  `upstream_media_error`.

## Free-tier budget

Groq free tier, `whisper-large-v3` (verify current values at
<https://console.groq.com/settings/limits>):

| Limit | Value | In practice |
|---|---|---|
| requests/minute | 20 | `MAX_CONCURRENT_ASR=3` keeps you well clear |
| requests/day | 2,000 | not the binding constraint |
| audio-seconds/hour | 7,200 | ~12 ten-minute videos per hour |
| audio-seconds/day | 28,800 | ~48 ten-minute videos per day |
| file size | 25 MB | `MAX_AUDIO_BYTES` is 20 MB, so you get a clear 413 first |

Past the quota the route returns `503 asr_rate_limited` with `Retry-After`.
Raising `MAX_CONCURRENT_ASR` does not buy throughput — it only makes 429s likelier.

## Deployment (Render free)

1. Push this repo to GitHub.
2. Render → **New → Web Service** → connect the repo → runtime **Docker** →
   instance type **Free**.
3. Health check path: `/health`.
4. Add environment variables — at minimum `API_KEY` and `GROQ_API_KEY`, plus
   `CLOUDINARY_API_SECRET` if your account uses strict transformations or signed
   URLs. Do **not** upload `.env`; set them in the dashboard.
5. Deploy, then confirm: `curl https://<service>.onrender.com/health`.
6. Add a `SERVICE_URL` repository secret in GitHub so
   [`.github/workflows/keepalive.yml`](.github/workflows/keepalive.yml) can ping
   `/health`.

**Cold starts.** Free services spin down after 15 minutes idle and take ~1 minute
to wake. The keep-alive workflow pings every 10 minutes during a daily window
rather than around the clock: 24/7 pinging costs ~730 of your 750 monthly
instance-hours, leaving no room for a second free service. The window in the
workflow costs ~560 h/month.

**Why not Hugging Face Spaces:** CPU Basic hardware is free, but creating a
Gradio or Docker Space requires a paid plan — only Static Spaces are free. A
plain FastAPI server cannot be a Static Space.

**Alternative:** Koyeb's free instance is always-on with no spin-down, which
removes the cold-start problem and the keep-alive workflow entirely.

## Testing locally with real keys

> **The development tooling is not tracked in this repository.** `tests/`,
> `scripts/`, `pytest.ini` and `requirements-dev.txt` are git-ignored, so this
> repo holds only what is needed to build, configure and deploy the service. The
> two sections below describe that tooling for whoever has the working copy; a
> fresh clone will not contain it, and `/demo` plus `/docs` are the testing
> surface that ships.

The offline suite never touches the network. Once `.env` has your real
`API_KEY` and `GROQ_API_KEY`, here is how to exercise the live path.

**Minimum `.env` for a live run:**

```
API_KEY=<any-strong-random-string-you-choose>
GROQ_API_KEY=gsk_...
```

Everything else has a working default. Add `CLOUDINARY_API_SECRET` only if your
account uses strict transformations or signed/authenticated URLs.

**1. Start the server from the project root.**

```bash
.venv/Scripts/python -m uvicorn app.main:app --reload
```

> `.env` is resolved **relative to the current working directory**, so launch
> from the repo root. Started elsewhere, the service comes up with no keys and
> answers `503 auth_not_configured` — `/health` will show
> `"auth_configured": false`, which is the quickest way to spot it.

**2. Confirm the keys were picked up.**

```bash
curl http://127.0.0.1:8000/health
```

Both `"auth_configured"` and `"asr_configured"` must be `true`.

**3. Use the test console: <http://127.0.0.1:8000/demo>**

Paste your `API_KEY` into the field (it stays in your browser), then either
click **Use a public sample** to check the plumbing against Cloudinary's `demo`
cloud, or paste one of your own video URLs. Press **Send request** on each
panel. You get the status, both timings, the transcript, and for frames a
timestamp timeline plus the real thumbnails. This is the fastest way to see a
request and its response together.

**4. Or run the two scripted checks.**

Both read `SMOKE_VIDEO_URL`, `SMOKE_VIDEO_DURATION_SEC` and `API_KEY` from your
`.env`, so neither needs anything on the command line. Secrets are reported as
present or absent and never printed.

[`scripts/cloudinary_checks.py`](scripts/cloudinary_checks.py) — **zero ASR
quota**, so run this one freely. Ten groups of checks: the derived audio URL and
its real bitrate, that every frame renders and each timestamp yields a distinct
image, CDN behaviour, transformation chaining and ordering, query-parameter
filtering, a missing public id, the frame-count curve across durations, all ten
SSRF rejections against the running container, response stability under repeated
requests, and that `/demo`, `/docs` and `/openapi.json` are reachable.

```bash
.venv/Scripts/python scripts/cloudinary_checks.py
.venv/Scripts/python scripts/cloudinary_checks.py --all-frames   # fetch all of them
```

[`scripts/smoke.py`](scripts/smoke.py) — makes **exactly one** ASR request, since
the free tier is metered in audio-seconds. `--skip-translate` makes it zero.

```bash
.venv/Scripts/python scripts/smoke.py                    # URL and duration from .env
.venv/Scripts/python scripts/smoke.py <url> --duration 15 # or override them
```

It checks `/health`, calls both routes, fetches one generated frame to prove
Cloudinary will render it, and prints the latency of each stage plus a count of
the ASR requests it made. Add `--base-url https://...` to point it at a deployed
instance.

**5. Or just use curl.**

```bash
curl -X POST http://127.0.0.1:8000/v1/translate \
  -H "x-api-key: $API_KEY" -H "content-type: application/json" \
  -d '{"cloudinary_url":"https://res.cloudinary.com/demo/video/upload/dog.mp4"}'
```

### Reading the failures

| What you see | What it means |
|---|---|
| `503 auth_not_configured` | `API_KEY` is empty — wrong working directory, or the key is not in `.env` |
| `401 unauthorized` | the header does not match `API_KEY` |
| `503 asr_not_configured` | `GROQ_API_KEY` is empty |
| `502 asr_unavailable` | the Groq key was rejected, or Groq is down — check it at <https://console.groq.com/keys> |
| `503 asr_rate_limited` | free-tier quota exhausted; `Retry-After` says how long |
| `502 upstream_media_error` | Cloudinary refused the derived URL. A 401 underneath means **strict transformations** — set `CLOUDINARY_API_SECRET` |
| `413 audio_too_large` | the derived audio exceeded `MAX_AUDIO_BYTES` |

Set `DEBUG_ERRORS=true` in `.env` while developing to have the real upstream
detail included in the response body alongside the `correlation_id`. Leave it
false in production.

## Testing offline

```bash
.venv/Scripts/python -m pytest            # 119 tests, no network, no quota
```

Both upstreams are served by `httpx.MockTransport`, so no network and no quota
is used. Coverage includes the hand-calculated frame-math reference
values (19 frames at 2 minutes; the 90 s / 13-frame timestamp vector), the URL
parser's acceptance and rejection cases, the streaming byte cap aborting
mid-stream, the multipart envelope splicing audio intact, provider rate-limit and
outage mapping, the chunked→spooled upload fallback, redirect re-validation, and
every access-control path.

The live path is covered in [Testing locally with real keys](#testing-locally-with-real-keys)
above. Whether your Cloudinary account needs `CLOUDINARY_API_SECRET` is
deliberately the one thing the offline suite cannot tell you.

## Growing it later

Both extension points exist as code, not plans:

- **A job queue.** Routes talk to a `Runner`
  ([`app/services/runner.py`](app/services/runner.py)), and `InlineRunner` awaits
  the pipeline today. A `QueuedRunner` returning a job id, plus a
  `/v1/jobs/{id}` route, changes only the wiring in `app/main.py`. Worth doing if
  a platform's HTTP timeout ever becomes the binding constraint.
- **Native-language transcripts.** `include_native` is accepted and plumbed
  through; `GroqProvider.transcribe_streaming` is implemented. Set
  `ENABLE_NATIVE_TRANSCRIPT=true` to turn it on, accepting that it is a second
  provider call and therefore halves your daily video capacity.

## Layout

```
app/
  main.py                  app factory, lifespan, CORS, error handlers
  config.py                every env var, with free-tier defaults
  errors.py                typed errors; details logged, not returned
  security.py              API key + optional CIDR allowlist
  deps.py                  request-scoped accessors for startup objects
  cloudinary_delivery/
    parse.py               the SSRF boundary: validate and decompose URLs
    derive.py              build audio / frame URLs, with optional signing
  static/demo.html         the test console: one file, no build step, no CDN
  routers/
    meta.py                GET /, GET /health          (open)
    demo.py                GET /demo                   (open, ENABLE_DEMO)
    translate.py           POST /v1/translate          (keyed)
    frames.py              POST /v1/frames             (keyed)
  services/
    frame_math.py          the two formulas, pure and dependency-free
    frame_pipeline.py      metadata → timestamps → URLs
    frame_sink.py          where frames go; the remote-analyzer seam
    audio_proxy.py         constant-memory streaming + multipart envelope
    asr.py                 provider protocol + Groq implementation
    translate_pipeline.py  URL → audio → transcript, with spool fallback
    runner.py              the queue seam
```
