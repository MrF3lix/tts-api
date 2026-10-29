## TTS UI


Run the legacy UI on a NVIDIA DGX Spark:

```bash
uv run --extra legacy --extra cu132 tts-ui-legacy device=cuda
```


### API

The API is split into a **gateway** (FastAPI, no model) and **workers** (one model each), connected through Redis:

```
client ──► gateway ──► Redis stream tts:tasks:xtts      ──► xtts-worker ×N
                   └─► Redis stream tts:tasks:qwen3-tts ──► qwen-worker ×M
```

Each worker serves one model, chosen by its model config in `configs/model/`:

| Model id | Config | Languages | Reference transcript (`ref_text`) |
|---|---|---|---|
| `xtts` | `gpt_xtts_v2` | Swiss German dialects + `de` | ignored |
| `qwen3-tts` | `qwen3_tts_base` (`Qwen/Qwen3-TTS-12Hz-1.7B-Base`) | `de en fr it es pt ru zh ja ko` | optional: with it the model clones in ICL mode, without it only the speaker embedding is used (lower quality). It must match the reference audio, or the output can derail into loops |

Workers register their model on startup, so `/models` lists what's available. A request picks one model with `model` (default `xtts`); all samples of a job use that model.

A job asks for N samples of the same text; each sample is one task, so samples spread over all free workers.
Job state, progress events and the generated audio live in Redis (kept for 24h by default).

Interactive docs are at `/docs`.

| Endpoint | |
|---|---|
| `POST /jobs` | multipart: `text`, `language` (code or name, per model), `speaker_wav`, optional `ref_text`, `num_samples` (default 3), `model`, `seed`. Returns `202` with the job |
| `GET /jobs/{id}` | status, per-sample progress, queue position, ETA (poll; honours `Retry-After`) |
| `GET /jobs/{id}/events` | the same as a live Server-Sent Events stream; ends when the job is finished |
| `GET /jobs/{id}/samples/{i}.wav` | a finished sample |
| `DELETE /jobs/{id}` | cancel samples that haven't started |
| `POST /generate` | blocking single sample, returns `audio/wav` directly; same fields as `/jobs` except `num_samples` |
| `GET /models` | registered models with their languages, `ref_text` policy and live worker count |
| `GET /languages?model=` | languages of one model |
| `GET /health` | live workers per model |

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

Qwen3-TTS needs a newer transformers than coqui, so its extra conflicts with `legacy` and needs its own environment:

```bash
UV_PROJECT_ENVIRONMENT=.venv-qwen uv run --extra qwen --extra api --extra cu132 tts-ui-worker model=qwen3_tts_base device=cuda
```

### Run in Docker (DGX Spark)

Requires the NVIDIA Container Toolkit (preinstalled on DGX OS). The checkpoint in `model/` is mounted, not copied into the image.

```bash
docker compose up -d --build
```

This starts Redis, the gateway (published on `PORT`), `XTTS_WORKERS` XTTS workers and `QWEN_WORKERS` Qwen3-TTS workers, all sharing the GPU (0 disables a model).
Qwen weights are downloaded from Hugging Face on first start and kept in the `hf-cache` volume.
All of them restart on crash and come back after a reboot (`restart: unless-stopped`, Docker itself starts on boot).
Workers that die mid-generation have their task taken over by another worker.

Settings via the environment or a `.env` file (see `.env.example`): `PORT` (8000), `XTTS_WORKERS` (2), `QWEN_WORKERS` (1), `DEVICE` (cuda), `TORCH_EXTRA` (cu132), `REDIS_MAXMEMORY` (4gb).
Change the number of workers without a rebuild: `docker compose up -d --scale xtts-worker=3 --scale qwen-worker=2`.

Logs: `docker compose logs -f gateway xtts-worker qwen-worker`. Update after `git pull`: `docker compose up -d --build`. Stop for good: `docker compose down`.
