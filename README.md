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

### Run as a background service (DGX Spark)

Install a systemd service that starts on boot and restarts on failure (asks for sudo):

```bash
deploy/install-service.sh 8000 cuda cu132
```

Manage it with `sudo systemctl {status,restart,stop} tts-ui-api`, follow logs with `journalctl -u tts-ui-api -f`.
After `git pull` or dependency changes, re-run the install script (it re-syncs the env and restarts the service).
