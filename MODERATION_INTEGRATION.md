# Moderation pipeline — reference design

How the Express backend should wire this service together with the two moderation
services. **Nothing described here is implemented in this repository**, and this
service never calls a moderation route. This is the specification for the new Express
route, written precisely enough to implement from.

```
client ──► Express backend (the new route)
              │
              ├─► Cloudinary                upload, and destroy on rejection
              ├─► this service              /v1/frames, /v1/translate
              ├─► text moderation           GET  /moderate?text=...
              └─► image moderation          POST /moderate-images {"urls":[...]}
```

---

## 1. The fixed contracts

Neither moderation service can be changed. Everything below is shaped around them.

### Text — `GET /moderate?text=<string>`

```json
{"safe": true,
 "scores": {"toxicity": 0, "severe_toxicity": 0, "obscene": 0,
            "threat": 0, "insult": 0, "identity_attack": 0}}
```

The text travels **in the URL**, which is the central constraint: a transcript of any
length will not fit. See §3.

> Its OpenAPI declares the response as `string` while the observed body is an object.
> Confirm from a raw response whether it is real JSON or a JSON string containing JSON
> — if the latter, every caller must `JSON.parse` twice.

### Images — `POST /moderate-images` with `{"urls": ["..."]}`

```json
{"safe": false, "nsfw_score": 0.046, "violence_score": 0.567,
 "reason": "violent or disturbing image detected"}
```

One aggregate verdict, **no per-URL breakdown**. It is unknown whether it reads past
the first URL, so **send exactly one URL per call**. That makes frame moderation
N sequential calls, which dominates the latency budget.

### This service

`POST /v1/frames` returns `frame_urls` — a flat array of strings that drops straight
into `{"urls": [...]}` — and accepts `max_frames` to limit how many there are.
`POST /v1/translate` returns `english_text`. Both need `x-api-key`.

Its failures are attributed to whichever side actually failed, which is what makes a
retry policy possible:

| Status | `error` | Meaning | Retry? |
|---|---|---|---|
| 400 | `invalid_cloudinary_url`, `unsupported_cloudinary_url`, `redirect_rejected` | the URL was rejected before anything was fetched | no |
| 413 | `audio_too_large` | derived audio over the cap | no |
| 502 | `upstream_media_error` | **Cloudinary** refused the derived URL or could not be reached — including a connection dropped part-way through the audio | **yes**, once |
| 503 | `asr_rate_limited` | speech-provider quota exhausted | honour `Retry-After` |
| 502 | `asr_unavailable` | the **speech provider** failed or was unreachable | yes, with backoff, max 2 |
| 503 | `asr_not_configured` / `auth_not_configured` | misconfiguration | no — alert |
| 500 | `internal_error` | genuinely unexpected | once |

A transport failure never arrives as `500`, so a `500` from this service means a real
defect rather than a network blip — treat it as alert-worthy rather than routine.
Every error body carries a `correlation_id`; log it, as it matches exactly one line in
that service's logs.

---

## 2. Call sequence

```
1. receive the video from the client
2. upload to Cloudinary                 -> secure_url, duration, public_id
3. in parallel (Promise.allSettled):
     a. POST /v1/frames    {cloudinary_url, video_id, duration_sec, max_frames}
     b. POST /v1/translate {cloudinary_url}
4. in parallel (Promise.allSettled):
     a. images: one call per URL, STOP at the first safe:false
     b. text:   every chunk is scored, no early exit
                skipped entirely when english_text is empty
5. decide (§4)
6. reject -> destroy the Cloudinary asset, respond with the category only
   accept -> respond with the transcript and frame data
```

Step 3 must use `allSettled`, not `all`: a transcript failure must not discard frames
that already succeeded, because the decision in §4 still needs to know the frames were
clean before it can attribute the rejection correctly.

```js
const [framesResult, textResult] = await Promise.allSettled([
  callFrames(cloudinaryUrl, videoId, durationSec, MAX_FRAMES),
  callTranslate(cloudinaryUrl),
]);
```

---

## 3. Chunking the transcript

**Chunk on percent-encoded length, not character count.** `encodeURIComponent` turns
each space into `%20` and most punctuation into three bytes, so English prose inflates
by roughly 1.3×. Chunking on raw characters produces URLs that are intermittently too
long — a failure that looks like a server fault rather than a client bug.

```js
const BUDGET = 1500; // encoded bytes of the text value

function chunkForQuery(text, budget = BUDGET) {
  const sentences = text.split(/(?<=[.!?])\s+/).filter(Boolean);
  const chunks = [];
  let current = "";

  const encodedLength = (s) => encodeURIComponent(s).length;

  for (const sentence of sentences) {
    // A single sentence longer than the budget cannot be packed; split it hard.
    if (encodedLength(sentence) > budget) {
      if (current) { chunks.push(current); current = ""; }
      chunks.push(...hardSplit(sentence, budget));
      continue;
    }
    const candidate = current ? `${current} ${sentence}` : sentence;
    if (encodedLength(candidate) > budget) {
      chunks.push(current);
      current = sentence;
    } else {
      current = candidate;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

function hardSplit(sentence, budget) {
  // Walk forward adding words until the encoded form would exceed the budget.
  const out = [];
  let current = "";
  for (const word of sentence.split(/\s+/)) {
    const candidate = current ? `${current} ${word}` : word;
    if (encodeURIComponent(candidate).length > budget) {
      if (current) out.push(current);
      current = word;
    } else {
      current = candidate;
    }
  }
  if (current) out.push(current);
  return out;
}
```

Splitting on sentence boundaries matters for accuracy: a chunk cut mid-sentence can
change the apparent meaning of what it contains.

**Why 1500.** The real limit is unknown and is imposed by whatever proxies that
service, not by the service itself — so it cannot be inferred from its code. 1500
encoded bytes survives essentially any proxy, CDN or server default. To measure the
actual ceiling and raise it later:

```bash
for n in 1000 2000 4000 8000 16000; do
  body=$(head -c $n < /dev/zero | tr '\0' 'a')
  printf '%6d -> ' "$n"
  curl -s -o /dev/null -w '%{http_code}\n' --get --data-urlencode "text=$body" \
    https://<text-service>/moderate
done
```

The first non-200 marks the cutoff; set `BUDGET` comfortably below it. Expect `414`
(URI too long) or `400` from the proxy rather than anything from the application.

---

## 4. Merging and deciding

```
text verdict    safe   = every chunk returned safe:true
                scores = per-category MAXIMUM across chunks
                (skipped entirely when english_text is empty -> not applicable)

image verdict   safe   = false if any frame returned safe:false; the loop stops there
                scores = the flagged frame's, or the maximum seen if all were clean

overall         accept = text.safe AND image.safe AND no call failed
```

**Reject whenever either service says `safe: false`.** Scores are recorded for the
audit trail but never override a verdict — if either service retunes its own
thresholds later, this logic stays correct without changes here.

**Any failure counts as not safe.** A timeout, a connection error, a non-2xx, or a
response whose shape does not parse all mean the content was not moderated, and
unmoderated content is never accepted.

**An empty transcript is not a failure.** A video with no audio track, or with no
speech, legitimately returns `english_text: ""`. Make no text call at all, record the
text verdict as not-applicable, and decide on the frames alone.

```js
const overall = {
  accept: textVerdict.safe && imageVerdict.safe && !anyCallFailed,
  text: textVerdict,    // {safe, scores, chunks, skipped}
  images: imageVerdict, // {safe, nsfw_score, violence_score, reason, flaggedIndex}
};
```

Because the image loop early-exits, keep the index it stopped at: combined with the
`frames[]` array from this service, it gives the **timestamp** of the offending
moment, which the aggregate verdict alone cannot provide.

---

## 5. Rejection and cleanup

On rejection — including rejection caused by a moderation failure — delete the video
from Cloudinary:

```js
await cloudinary.uploader.destroy(publicId, { resource_type: "video", invalidate: true });
```

`resource_type: "video"` is required; the default is `image` and the call silently
succeeds without deleting anything if it is omitted.

Three things to get right:

- **Deletion is its own failure point.** It must be retried and logged independently of
  the moderation outcome.
- **Never report the upload as accepted because deletion failed.** The client still
  gets a rejection; the orphaned asset becomes an operational problem, not a product
  one. Log it loudly and reconcile separately.
- **It is idempotent.** Destroying an already-destroyed public id is safe, so retrying
  cannot cause damage.

---

## 6. What the client is told

**Category only, never scores.** Publishing exact scores and thresholds hands a
determined uploader a map of what to avoid.

```json
{"accepted": false, "reason": "violent or disturbing content"}
```

```json
{"accepted": true, "video_url": "https://res.cloudinary.com/...",
 "transcript": "...", "frame_count": 12}
```

**Full detail goes to the logs**, not the response: every score, the flagged frame's
index and timestamp, the chunk count, per-hop timings, and the `correlation_id` from
any failure returned by this service.

---

## 7. Latency budget

A worked example for a clean 5-minute video with `max_frames` unset:

| Hop | Time |
|---|---|
| Cloudinary upload | depends on the client's connection |
| `/v1/frames` | ~10 ms |
| `/v1/translate` | ~3 s (runs in parallel with frames) |
| 60 image calls, one URL each, sequential | ~60 s at 1 s per call |
| ~6 text chunks (parallel with images) | a few seconds |
| **total** | **~60 s, dominated entirely by the image loop** |

Add up to ~60 s per service for a free-tier cold start. This can exceed a platform's
request timeout, so the route needs either generous timeouts or, eventually, a job id
the client polls.

Three levers that actually move the number:

1. **`max_frames`.** Twelve frames instead of sixty cuts the dominant cost by 80%.
   This is the single biggest win and costs nothing but sampling density.
2. **Early exit on images.** Rejections become fast; only clean videos pay in full.
3. **Pre-warm the frames.** Each frame URL is rendered by Cloudinary on its *first*
   request, so the moderation service pays that render cost. Firing parallel `HEAD`
   requests at all the frame URLs before the loop starts means every subsequent fetch
   is a CDN hit:
   ```js
   await Promise.allSettled(frameUrls.map((u) => fetch(u, { method: "HEAD" })));
   ```

Suggested per-hop timeouts: 30 s for `/v1/frames`, 180 s for `/v1/translate`, 30 s per
image call, 15 s per text chunk — each generous enough to survive a cold start.

---

## 8. Data handling

- Send only the Cloudinary URL and the metadata. Neither moderation service needs, nor
  should receive, user identifiers or anything else from the originating request.
- The API key for this service goes in the `x-api-key` header — never a query string,
  where proxies log it.
- `english_text` is machine-generated from user-supplied audio. Escape it before
  display, and never interpolate it into SQL, a shell command, or a prompt that grants
  it authority. The same applies to `reason` from the image service.
- **Frame URLs are public.** Unless this service runs with `CLOUDINARY_API_SECRET` set,
  derived frame URLs are unsigned: anyone holding one can view that frame. They are
  being handed to a third-party service by design, so decide explicitly whether that is
  acceptable for the content, and whether they may be persisted or logged.
- This service stores nothing. If the transcript is discarded it can only be recovered
  by calling again, which re-spends ASR quota.

---

## 9. Known unknowns

Recorded rather than guessed. Each should be resolved before this goes to production.

| Unknown | Current assumption | How to resolve |
|---|---|---|
| Does `/moderate-images` read past the first URL? | **No** — one URL per call | Send two URLs where only the second is unsafe; if the verdict is `safe: true`, only the first was read |
| The real query-string limit on `/moderate` | 1500 encoded bytes is safe | The probe in §3 |
| Is the `/moderate` body JSON or a JSON string? | A JSON object | Inspect a raw response rather than the Swagger render |
| Per-call latency of the image service | ~1 s | Time ten real calls; it sets the whole budget |

---

## 10. Implementation checklist

- [ ] Persist Cloudinary's `duration` and `public_id` at upload time.
- [ ] `/v1/frames` and `/v1/translate` called in parallel with `Promise.allSettled`.
- [ ] `max_frames` set deliberately — it is the main latency lever.
- [ ] Optional: pre-warm frame URLs with parallel `HEAD` requests.
- [ ] One URL per `/moderate-images` call; stop at the first `safe: false`.
- [ ] Chunk the transcript on **encoded** length; score every chunk.
- [ ] Skip text moderation entirely when the transcript is empty.
- [ ] Any failure ⇒ not safe.
- [ ] On rejection, `destroy` with `resource_type: "video"`, retried and logged.
- [ ] Client sees the category only; full detail is logged.
- [ ] Record the flagged frame's index so its timestamp can be reported internally.
