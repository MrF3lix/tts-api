# Using the TTS API from another application

How a client, such as a web UI, a desktop app or a script, drives the gateway: finding models, submitting jobs, following progress and fetching audio.
This page describes the API as it is now. Login, job ownership and keeping samples permanently are planned in [auth-and-frontend.md](auth-and-frontend.md).

The full schema is also served by the gateway at `/docs` (Swagger UI) and `/openapi.json`, which can be used to generate a typed client.

## 1. The flow in short

```
 client                                   gateway
   │  GET  /models                          │   which models, languages, ref_text policy
   │ ─────────────────────────────────────► │
   │  POST /jobs  (multipart)               │   202 + job { id, samples[], ... }
   │ ─────────────────────────────────────► │
   │  GET  /jobs/{id}/events  (SSE)         │   event: job  (on every change)
   │ ◄───────────────────────────────────── │   event: end  (when all samples are finished)
   │  GET  /jobs/{id}/samples/{i}.wav       │   audio/wav, per completed sample
   │ ─────────────────────────────────────► │
```

A **job** is one request for N samples (variants) of the same text, voice and model. Every sample has its own seed and its own status, and samples are generated in parallel on whatever workers are free. Samples become available one by one, so a UI can play the first while the others are still running.

Everything about a job (state and audio) is kept for **24 hours** (`jobs.result_ttl_seconds`), then it's gone and the endpoints return `404`. A client that wants to keep audio must download it.

For the simplest case, one sample and no progress display, `POST /generate` blocks and returns the WAV directly (see [section 7](#7-the-blocking-shortcut-post-generate)).

## 2. Discover models and languages

```http
GET /models
```

```json
[
  {
    "id": "xtts",
    "name": "GPT XTTS v2.0",
    "backend": "xtts",
    "ref_text": "ignored",
    "languages": [{"code": "ch_be", "name": "Bern"}, {"code": "ch_zh", "name": "Zürich"}, {"code": "de", "name": "Deutsch"}],
    "workers": 2
  },
  {
    "id": "qwen3-tts",
    "name": "Qwen3-TTS 12Hz 1.7B Base",
    "backend": "qwen3",
    "ref_text": "optional",
    "languages": [{"code": "de", "name": "Deutsch"}, {"code": "en", "name": "English"}],
    "workers": 1
  }
]
```

Build the form from this response instead of hard-coding models. Models appear here once their workers have started.

| Field | Use in a UI |
|---|---|
| `id` | Send as `model` when submitting |
| `name` | Label in the model picker |
| `languages` | Options of the language picker; send the `code` (the `name` is accepted too) |
| `ref_text` | `ignored`: hide the transcript field. `optional`: show it, and say that results are better with it. `required`: show it as mandatory |
| `workers` | `0` means no worker is running; disable the model or warn that jobs will wait |

`GET /languages?model=<id>` returns only the language list of one model. `GET /health` returns `{"status": "ok", "workers": {"xtts": 2, ...}}` and is a cheap check that the gateway and Redis are up.

## 3. Submit a job

```http
POST /jobs
Content-Type: multipart/form-data
```

| Field | Required | |
|---|---|---|
| `text` | yes | Text to speak, at most 1000 characters |
| `language` | yes | A language code (or name) of the chosen model |
| `speaker_wav` | yes | Reference recording of the voice to clone, any format `soundfile` can read (WAV, FLAC, OGG), at most 60 s. Stereo is mixed down |
| `ref_text` | depends on the model | Transcript of `speaker_wav`. For `qwen3-tts` it must match the recording, otherwise the output can derail into loops |
| `model` | no | Model id, defaults to `xtts` |
| `num_samples` | no | 1 to 8, default 3 |
| `seed` | no | Base seed; sample `i` uses `seed + i`. Random if omitted. Sending the same inputs and seed reproduces a result |

The response is `202 Accepted`, a `Location: /jobs/{id}` header and the job itself:

```json
{
  "id": "3f2c9a...",
  "status": "queued",
  "model": "xtts",
  "language": "ch_be",
  "text": "Grüessech mitenand",
  "ref_text": null,
  "created_at": 1767000000.0,
  "samples_total": 3,
  "samples_completed": 0,
  "samples": [
    {"index": 0, "status": "queued", "seed": 184467},
    {"index": 1, "status": "queued", "seed": 991201},
    {"index": 2, "status": "queued", "seed": 55021}
  ],
  "queue_position": 4,
  "eta_seconds": 36
}
```

Store the `id`. It is the only handle to the job, and with it a client can resume after a page reload.

## 4. Follow progress

The job object above is what every progress update looks like.

| Field | Meaning |
|---|---|
| `status` | Job: `queued` → `running` → `completed` / `failed` / `cancelled`. `completed` means at least one sample succeeded |
| `samples[i].status` | Sample: `queued` → `running` → `completed` / `failed` / `cancelled` |
| `samples[i].url` | Present once the sample is `completed`; a path relative to the gateway |
| `samples[i].duration_s` | Length of the generated audio |
| `samples[i].error` | Present on `failed` samples |
| `queue_position` | Tasks of other jobs ahead of this one; only while the job is `queued`, otherwise `null` |
| `eta_seconds` | Rough estimate until all samples are done, from the average generation time and the number of workers; `null` when unknown or finished |

Drive the UI from the per-sample statuses. In particular, a cancelled job reports `cancelled` right away, while samples that were already running still finish and become playable.

### Option A: Server-Sent Events (recommended)

```http
GET /jobs/{id}/events
Accept: text/event-stream
```

The stream sends:

- `event: job` with the full job object as `data`, first immediately and then on every change (sample started, finished, queue position or ETA moved).
- `: keepalive` comment lines while nothing changes. Ignore them; `EventSource` does so automatically.
- `event: end` with `{"status": "<job status>"}` once all samples are finished. The server then closes the stream.

**Close the `EventSource` on `end`.** Browsers automatically reconnect when a server closes a stream, so a client that doesn't close it reopens the stream and gets the finished job again in a loop.

The stream also ends after 30 minutes (`jobs.events_timeout_seconds`) even if the job isn't finished. If the `end` status is still `queued` or `running`, open the stream again.

If the gateway is behind a reverse proxy, the proxy must not buffer the response (the gateway sends `X-Accel-Buffering: no` for nginx).

### Option B: Polling

```http
GET /jobs/{id}
```

Returns the same object. While the job is unfinished the response carries `Retry-After: 2`; poll at that interval. Use this where SSE is awkward (some HTTP clients, serverless functions with short time limits).

## 5. Fetch audio

```http
GET /jobs/{id}/samples/{i}.wav
```

Returns `audio/wav` (16-bit). This is the `url` field of a completed sample, so prefix it with the gateway's base URL. It can be used directly as `<audio src>` or downloaded.

| Status | Meaning |
|---|---|
| `409` | Sample is not `completed` yet (or failed / was cancelled); `detail` says which |
| `404` | Wrong index, or the job / audio has expired |

## 6. Cancel

```http
DELETE /jobs/{id}
```

Cancels every sample that hasn't started yet and returns the updated job. Running samples still finish. Call this when the user abandons a job, so queued work doesn't take GPU time from others.

## 7. The blocking shortcut: `POST /generate`

Same fields as `POST /jobs` without `num_samples`. Waits for one sample and returns the WAV as the response body. It goes through the same queue, so it can wait behind other jobs.

| Status | Meaning |
|---|---|
| `200` | Body is the audio |
| `500` | Generation failed; `detail` has the error |
| `504` | Not done within 5 minutes; the job continues, follow it at the `Location` header |

Fine for scripts and quick integrations. A UI should use `/jobs`, which shows progress and doesn't hold a request open.

## 8. Errors

Errors are JSON in FastAPI's format: `{"detail": "<message>"}`, or for missing or mistyped form fields `{"detail": [{"loc": [...], "msg": "..."}]}`. The string messages are meant for people and can be shown as they are.

| Status | When | What the client should do |
|---|---|---|
| `422` | Invalid input: empty or too long text, unknown model or language, `ref_text` missing where required, unreadable or too long `speaker_wav`, `num_samples` out of range | Show `detail` next to the form |
| `429` | More than 200 samples are already waiting for this model | Wait the `Retry-After` seconds (30), then retry, or tell the user the service is busy |
| `404` | Unknown or expired job | Drop the job from local state |
| `409` | Sample audio requested before it is completed | Wait for the sample's status |
| `5xx` | Gateway or Redis problem | Retry later |

## 9. Examples

### TypeScript (browser or Node 18+)

```ts
const API = "https://api.example.org"; // gateway base URL

type Sample = { index: number; status: string; seed: number; url?: string; duration_s?: number; error?: string };
type Job = {
  id: string; status: string; model: string; samples_total: number; samples_completed: number;
  samples: Sample[]; queue_position: number | null; eta_seconds: number | null;
};

async function submitJob(form: {
  text: string; language: string; speaker: Blob; model?: string; refText?: string; numSamples?: number; seed?: number;
}): Promise<Job> {
  const body = new FormData();
  body.append("text", form.text);
  body.append("language", form.language);
  body.append("speaker_wav", form.speaker, "speaker.wav");
  if (form.model) body.append("model", form.model);
  if (form.refText) body.append("ref_text", form.refText);
  if (form.numSamples) body.append("num_samples", String(form.numSamples));
  if (form.seed !== undefined) body.append("seed", String(form.seed));

  const res = await fetch(`${API}/jobs`, { method: "POST", body });
  if (!res.ok) {
    const { detail } = await res.json();
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

function followJob(id: string, onUpdate: (job: Job) => void): () => void {
  const es = new EventSource(`${API}/jobs/${id}/events`);
  es.addEventListener("job", (e) => onUpdate(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("end", (e) => {
    es.close(); // otherwise the browser reconnects
    const { status } = JSON.parse((e as MessageEvent).data);
    if (status === "queued" || status === "running") followJob(id, onUpdate); // stream timed out
  });
  return () => es.close();
}

// usage
const job = await submitJob({ text: "Grüessech mitenand", language: "ch_be", speaker: file, numSamples: 3 });
followJob(job.id, (job) => {
  for (const s of job.samples) {
    if (s.status === "completed") showPlayer(s.index, `${API}${s.url}`);
  }
});
```

### Python

```python
import json
import httpx

API = "http://localhost:8000"

with open("ref.wav", "rb") as f:
    job = httpx.post(f"{API}/jobs", data={"text": "Grüessech mitenand", "language": "ch_be", "num_samples": 3},
                     files={"speaker_wav": f}).raise_for_status().json()

# follow the event stream until the job is finished
with httpx.stream("GET", f"{API}/jobs/{job['id']}/events", timeout=None) as r:
    event = None
    for line in r.iter_lines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event == "job":
            job = json.loads(line.split(":", 1)[1])
            print(job["status"], f"{job['samples_completed']}/{job['samples_total']}", job["eta_seconds"])

for s in job["samples"]:
    if s["status"] == "completed":
        audio = httpx.get(f"{API}{s['url']}").raise_for_status().content
        open(f"sample_{s['index']}.wav", "wb").write(audio)
```

### curl

```bash
curl -F text="Grüessech mitenand" -F language=ch_be -F num_samples=3 -F speaker_wav=@ref.wav http://localhost:8000/jobs
curl -N http://localhost:8000/jobs/<id>/events
curl -o out.wav http://localhost:8000/jobs/<id>/samples/0.wav
```

## 10. Notes for a web UI

**Calling the gateway from a browser.** The gateway has no CORS configuration and no authentication yet. A page served from another origin can't call it directly, and it should not be exposed publicly as it is. Put a small backend in between (for example Next.js route handlers) that forwards requests to the gateway, as planned in [auth-and-frontend.md](auth-and-frontend.md). Forward the SSE response as a stream, without buffering, and the multipart body as is.

**Suggested screen.**

1. Model picker filled from `/models`; switching the model refills the language picker and shows or hides the transcript field according to `ref_text`.
2. Reference voice: file upload or microphone recording (`MediaRecorder` output has to be converted to WAV or another format `soundfile` reads; WebM/Opus is not accepted). Keep it under 60 s; a clean 5–15 s clip is usually enough.
3. Text, number of variants and an optional seed field (show each sample's seed so a good result can be reproduced).
4. After submit: one row per sample with its status, a player as soon as it completes, the queue position or ETA from the job, and a cancel button.

**Resuming.** Keep the job id in the URL or local storage. On reload, `GET /jobs/{id}` restores the full state, then reopen the event stream if it's unfinished.

**Retention.** Tell users that results disappear after 24 hours, and offer a download button per sample.
