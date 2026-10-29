## TTS UI


Run the legacy UI on a NVIDIA DGX Spark:

```bash
uv run --extra legacy --extra cu132 tts-ui-legacy device=cuda
```


Run the FastAPI server (defaults to `127.0.0.1:8000`, device `auto`):

```bash
uv run --extra legacy --extra api --extra cu132 tts-ui-api server.host=0.0.0.0
```

Interactive docs are at `/docs`. Generate speech:

```bash
curl -F text="Grüessech mitenand" -F language=ch_be -F speaker_wav=@speaker.wav \
  http://localhost:8000/generate -o out.wav
```

Endpoints: `GET /health`, `GET /languages`, `POST /generate` (multipart: `text`, `language` code or name, `speaker_wav`) → `audio/wav`.
