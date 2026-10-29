## TTS UI


Run the legacy UI on a NVIDIA DGX Spark:

```bash
uv run --extra legacy --extra cu132 tts-ui-legacy device=cuda
```


### API

The API is split into a **gateway** (FastAPI, no model) and **workers** (one model each), connected through Redis:

```
client ──► gateway ──► Redis stream tts:tasks:xtts ──► xtts-worker ×N
```

A job asks for N samples of the same text; each sample is one task, so samples spread over all free workers.
Job state, progress events and the generated audio live in Redis (kept for 24h by default).

Interactive docs are at `/docs`.

| Endpoint | |
|---|---|
| `POST /jobs` | multipart: `text`, `language` (code or name), `speaker_wav`, optional `num_samples` (default 3), `model`, `seed`. Returns `202` with the job |
| `GET /jobs/{id}` | status, per-sample progress, queue position, ETA (poll; honours `Retry-After`) |
| `GET /jobs/{id}/events` | the same as a live Server-Sent Events stream; ends when the job is finished |
| `GET /jobs/{id}/samples/{i}.wav` | a finished sample |
| `DELETE /jobs/{id}` | cancel samples that haven't started |
| `POST /generate` | blocking single sample, returns `audio/wav` directly |
| `GET /health`, `/models`, `/languages` | live workers per model, available models and languages |

When more than `jobs.max_queued_samples` (200) samples are waiting for a model, new jobs get `429`.

```bash
curl -F text="Grüessech mitenand" -F language=ch_be -F num_samples=3 -F speaker_wav=@data/ref.wav \
  http://localhost:8000/jobs
curl -N http://localhost:8000/jobs/<id>/events
curl -o out.wav http://localhost:8000/jobs/<id>/samples/0.wav
```

Run locally without Docker (needs a Redis at `REDIS_URL`, default `redis://localhost:6379/0`). Use the same extras for both, since `uv run` syncs the shared `.venv` to exactly the extras given:

```bash
uv run --extra legacy --extra api --extra cu132 tts-ui-api
```

```bash
uv run --extra legacy --extra api --extra cu132 tts-ui-worker device=cuda
```

### Run in Docker (DGX Spark)

Requires the NVIDIA Container Toolkit (preinstalled on DGX OS). The checkpoint in `model/` is mounted, not copied into the image.

```bash
docker compose up -d --build
```

This starts Redis, the gateway (published on `PORT`) and `XTTS_WORKERS` worker replicas sharing the GPU.
All of them restart on crash and come back after a reboot (`restart: unless-stopped`, Docker itself starts on boot).
Workers that die mid-generation have their task taken over by another worker.

Settings via the environment or a `.env` file: `PORT` (8000), `XTTS_WORKERS` (2), `DEVICE` (cuda), `TORCH_EXTRA` (cu132), `REDIS_MAXMEMORY` (4gb).
Change the number of workers without a rebuild: `docker compose up -d --scale xtts-worker=3`.

Logs: `docker compose logs -f gateway xtts-worker`. Update after `git pull`: `docker compose up -d --build`. Stop for good: `docker compose down`.
