## TTS UI


Run the legacy UI on a NVIDIA DGX Spark:

```bash
uv run --extra legacy --extra cu132 tts-ui-legacy device=cuda
```


Run the FastAPI server (defaults to `127.0.0.1:8000`, device `auto`):

```bash
uv run --extra legacy --extra api --extra cu132 tts-ui-api
```

Interactive docs are at `/docs`. Generate speech:

```bash
curl -F text="Grüessech mitenand" -F language=ch_be -F speaker_wav=@data/ref.wav \
  http://localhost:7860/generate -o out.wav
```

Endpoints: `GET /health`, `GET /languages`, `POST /generate` (multipart: `text`, `language` code or name, `speaker_wav`) → `audio/wav`.

### Run in Docker (DGX Spark)

Requires the NVIDIA Container Toolkit (preinstalled on DGX OS). The checkpoint in `model/` is mounted, not copied into the image.

```bash
docker compose up -d --build
```

The container restarts on crash and comes back after a reboot (`restart: unless-stopped`, Docker itself starts on boot).
Set `PORT` / `DEVICE` in the environment or a `.env` file to override the defaults (`8000`, `cuda`).

Logs: `docker compose logs -f`. Update after `git pull`: `docker compose up -d --build`. Stop for good: `docker compose down`.
