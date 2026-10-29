import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import hydra
import rootutils
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from omegaconf import DictConfig
from pydantic import BaseModel

from tts_ui.xtts import LANG_MAP, Synthesizer, load_tts, resolve_device, resolve_language


rootutils.setup_root(__file__, indicator=".project-root", pythonpath=True)
CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "configs")
log = logging.getLogger(__name__)


class Language(BaseModel):
    code: str
    name: str


class Health(BaseModel):
    status: str
    model: str
    device: str


def create_app(cfg: DictConfig, synthesizer: Synthesizer | None = None) -> FastAPI:
    """Build the API. Pass a synthesizer to skip model loading (e.g. in tests)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if synthesizer is None:
            device = resolve_device(cfg.device)
            app.state.synthesizer = Synthesizer(load_tts(cfg.model, device))
            app.state.device = str(device)
        else:
            app.state.synthesizer = synthesizer
            app.state.device = cfg.device
        yield

    app = FastAPI(title="TTS UI", version="0.1.0", lifespan=lifespan)

    @app.get("/health", response_model=Health)
    def health():
        return Health(status="ok", model=cfg.model.model_name, device=app.state.device)

    @app.get("/languages", response_model=list[Language])
    def languages():
        return [Language(code=code, name=name) for code, name in LANG_MAP.items()]

    @app.post(
        "/generate",
        response_class=Response,
        responses={200: {"content": {"audio/wav": {}}, "description": "Generated speech as 16-bit WAV"}},
    )
    async def generate(
        text: str = Form(..., description="Text to synthesize"),
        language: str = Form(..., description="Language code (e.g. ch_be) or name (e.g. Bern)"),
        speaker_wav: UploadFile = File(..., description="Reference recording of the target speaker"),
    ):
        text = text.strip()
        if not text:
            raise HTTPException(422, "text must not be empty")
        if len(text) > cfg.limits.max_text_chars:
            raise HTTPException(422, f"text exceeds {cfg.limits.max_text_chars} characters")
        try:
            lang_tag = resolve_language(language)
        except ValueError as e:
            raise HTTPException(422, str(e))

        try:
            speaker_audio, speaker_sr = sf.read(io.BytesIO(await speaker_wav.read()), dtype='float32')
        except Exception:
            raise HTTPException(422, "speaker_wav could not be decoded as audio")
        if len(speaker_audio) == 0:
            raise HTTPException(422, "speaker_wav is empty")
        if len(speaker_audio) / speaker_sr > cfg.limits.max_speaker_seconds:
            raise HTTPException(422, f"speaker_wav exceeds {cfg.limits.max_speaker_seconds} seconds")

        synth: Synthesizer = app.state.synthesizer
        wav = await run_in_threadpool(synth.synthesize, text, speaker_audio, speaker_sr, lang_tag)

        buf = io.BytesIO()
        sf.write(buf, wav, synth.sample_rate, format='WAV', subtype='PCM_16')
        return Response(content=buf.getvalue(), media_type="audio/wav")

    return app


@hydra.main(version_base="1.3", config_path=CONFIG_PATH, config_name="api.yaml")
def main(cfg: DictConfig):
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port)

if __name__ == "__main__":
    main()
