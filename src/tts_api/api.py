import io
import json
import logging
import math
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import hydra
import redis.asyncio as redis
import rootutils
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from omegaconf import DictConfig
from pydantic import BaseModel

from tts_api import jobs


rootutils.setup_root(__file__, indicator=".project-root", pythonpath=True)
CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "configs")
log = logging.getLogger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # nginx and similar proxies buffer responses by default, which would delay events
    "X-Accel-Buffering": "no",
}


class Language(BaseModel):
    code: str
    name: str


class ModelInfo(BaseModel):
    id: str
    name: str
    backend: str
    # whether the model uses a transcript of the reference recording: ignored | optional | required
    ref_text: str
    languages: list[Language]
    workers: int


def create_app(cfg: DictConfig) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.r = redis.Redis.from_url(cfg.redis_url, decode_responses=True)
        app.state.rb = redis.Redis.from_url(cfg.redis_url)  # binary values (audio)
        yield
        await app.state.r.aclose()
        await app.state.rb.aclose()

    app = FastAPI(title="TTS API", version="0.2.0", lifespan=lifespan)

    async def worker_count(model: str) -> int:
        return len([k async for k in app.state.r.scan_iter(match=jobs.worker_pattern(model))])

    async def registered_models() -> dict[str, dict]:
        # workers register their model (and create its task stream) on startup
        entries = await app.state.r.hgetall(jobs.MODELS_KEY)
        return {model_id: json.loads(info) for model_id, info in entries.items()}

    async def get_model(model_id: str | None) -> dict:
        model_id = model_id or cfg.default_model
        models = await registered_models()
        if model_id not in models:
            raise HTTPException(422, f"Unknown model '{model_id}', expected one of {sorted(models)}")
        return models[model_id]

    def resolve_language(model: dict, language: str) -> str:
        """Accept either a language code (ch_be) or its display name (Bern) of the given model."""
        languages = model["languages"]
        if language in languages:
            return language
        by_name = {name: code for code, name in languages.items()}
        if language in by_name:
            return by_name[language]
        raise HTTPException(422, f"Language '{language}' is not supported by model '{model['id']}', "
                                 f"expected one of {list(languages)}")

    async def model_info(model: dict) -> ModelInfo:
        return ModelInfo(
            id=model["id"], name=model["name"], backend=model["backend"], ref_text=model["ref_text"],
            languages=[Language(code=c, name=n) for c, n in model["languages"].items()],
            workers=await worker_count(model["id"]),
        )

    async def load_job(job_id: str) -> dict:
        fields = await app.state.r.hgetall(jobs.job_key(job_id))
        if not fields:
            raise HTTPException(404, "job not found (unknown id or expired)")
        return fields

    async def job_snapshot(fields: dict) -> dict:
        """Public job view plus queue position and a rough ETA."""
        r = app.state.r
        view = jobs.job_view(fields)
        model = fields["model"]
        view["queue_position"] = None
        view["eta_seconds"] = None
        if jobs.is_finished(fields):
            return view

        remaining = view["samples_total"] - sum(s["status"] in jobs.FINISHED for s in view["samples"])
        ahead = 0
        if view["status"] == "queued":
            # tasks between the last one handed to a worker and this job's first task
            stream = jobs.task_stream(model)
            groups = await r.xinfo_groups(stream)
            last_delivered = next((g["last-delivered-id"] for g in groups if g["name"] == jobs.GROUP), "0-0")
            first_task = fields.get(jobs.sample_field(0, "task_id"))
            if first_task:
                ahead = len(await r.xrange(stream, min=f"({last_delivered}", max=f"({first_task}"))
            view["queue_position"] = ahead

        avg = await r.get(jobs.avg_seconds_key(model))
        workers = await worker_count(model)
        if avg is not None and workers:
            view["eta_seconds"] = math.ceil(math.ceil((ahead + remaining) / workers) * float(avg))
        return view

    async def read_speaker(upload: UploadFile) -> bytes:
        try:
            audio, sr = sf.read(io.BytesIO(await upload.read()), dtype='float32')
        except Exception:
            raise HTTPException(422, "speaker_wav could not be decoded as audio")
        if len(audio) == 0:
            raise HTTPException(422, "speaker_wav is empty")
        if len(audio) / sr > cfg.limits.max_speaker_seconds:
            raise HTTPException(422, f"speaker_wav exceeds {cfg.limits.max_speaker_seconds} seconds")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        # store a clean 16-bit mono copy; also avoids decoding issues with 64-bit wavs in the worker
        buf = io.BytesIO()
        sf.write(buf, audio, sr, format='WAV', subtype='PCM_16')
        return buf.getvalue()

    async def create_job(text, language, speaker_wav, ref_text, num_samples, model_id, seed) -> dict:
        text = text.strip()
        if not text:
            raise HTTPException(422, "text must not be empty")
        if len(text) > cfg.limits.max_text_chars:
            raise HTTPException(422, f"text exceeds {cfg.limits.max_text_chars} characters")
        spec = await get_model(model_id)
        model = spec["id"]
        lang_tag = resolve_language(spec, language)
        ref_text = (ref_text or "").strip()
        if len(ref_text) > cfg.limits.max_ref_text_chars:
            raise HTTPException(422, f"ref_text exceeds {cfg.limits.max_ref_text_chars} characters")
        if spec["ref_text"] == "required" and not ref_text:
            raise HTTPException(422, f"model '{model}' requires ref_text, the transcript of speaker_wav")
        num_samples = num_samples or cfg.jobs.default_samples
        if not 1 <= num_samples <= cfg.jobs.max_samples:
            raise HTTPException(422, f"num_samples must be between 1 and {cfg.jobs.max_samples}")
        speaker = await read_speaker(speaker_wav)

        r = app.state.r
        # reserve queue capacity first; reject instead of accepting work we can't finish
        queued = await r.incrby(jobs.queued_key(model), num_samples)
        if queued > cfg.jobs.max_queued_samples:
            await r.decrby(jobs.queued_key(model), num_samples)
            raise HTTPException(429, "queue is full, try again later", headers={"Retry-After": "30"})

        job_id = uuid.uuid4().hex
        seeds = [seed + i if seed is not None else secrets.randbelow(2**31) for i in range(num_samples)]
        fields = {
            "id": job_id, "model": model, "text": text, "language": lang_tag, "ref_text": ref_text,
            "samples_total": num_samples, "created_at": time.time(), "cancelled": "0",
        }
        for i, s in enumerate(seeds):
            fields[jobs.sample_field(i, "status")] = "queued"
            fields[jobs.sample_field(i, "seed")] = s

        ttl = cfg.jobs.result_ttl_seconds
        try:
            pipe = app.state.rb.pipeline()
            pipe.hset(jobs.job_key(job_id), mapping=fields)
            pipe.expire(jobs.job_key(job_id), ttl)
            pipe.set(jobs.speaker_key(job_id), speaker, ex=ttl)
            await pipe.execute()

            # the job record exists before any worker can see a task for it
            pipe = r.pipeline()
            for i in range(num_samples):
                pipe.xadd(jobs.task_stream(model), {"job_id": job_id, "index": i},
                          maxlen=cfg.jobs.stream_maxlen, approximate=True)
            task_ids = await pipe.execute()
            await r.hset(jobs.job_key(job_id),
                         mapping={jobs.sample_field(i, "task_id"): t for i, t in enumerate(task_ids)})
        except Exception:
            await r.decrby(jobs.queued_key(model), num_samples)
            raise
        return await load_job(job_id)

    async def wait_for_job(job_id: str, timeout: float):
        """Yield the job hash on every change until it is finished or the timeout passes."""
        pubsub = app.state.r.pubsub()
        # subscribe before reading, so no change between the read and the subscription is missed
        await pubsub.subscribe(jobs.events_channel(job_id))
        deadline = time.monotonic() + timeout
        try:
            while True:
                fields = await load_job(job_id)
                yield fields
                if jobs.is_finished(fields) or time.monotonic() > deadline:
                    return
                await pubsub.get_message(ignore_subscribe_messages=True,
                                         timeout=max(0, min(cfg.jobs.event_interval_seconds, deadline - time.monotonic())))
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

    @app.get("/health")
    async def health():
        await app.state.r.ping()
        return {"status": "ok", "workers": {m: await worker_count(m) for m in await registered_models()}}

    @app.get("/languages", response_model=list[Language])
    async def languages(model: str | None = None):
        """Languages of one model (the default model if none is given)."""
        return (await model_info(await get_model(model))).languages

    @app.get("/models", response_model=list[ModelInfo])
    async def models():
        return [await model_info(m) for m in (await registered_models()).values()]

    @app.post("/jobs", status_code=202)
    async def submit_job(
        text: str = Form(..., description="Text to synthesize"),
        language: str = Form(..., description="Language code (e.g. ch_be) or name (e.g. Bern)"),
        speaker_wav: UploadFile = File(..., description="Reference recording of the target speaker"),
        ref_text: str | None = Form(None, description="Transcript of speaker_wav (see ref_text in /models)"),
        num_samples: int | None = Form(None, description="Number of samples to generate (default from config)"),
        model: str | None = Form(None, description="Model id (see /models); all samples use this model"),
        seed: int | None = Form(None, description="Base seed; sample i uses seed + i. Random if omitted"),
    ):
        fields = await create_job(text, language, speaker_wav, ref_text, num_samples, model, seed)
        return JSONResponse(await job_snapshot(fields), status_code=202,
                            headers={"Location": f"/jobs/{fields['id']}"})

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str):
        view = await job_snapshot(await load_job(job_id))
        headers = {} if view["status"] in jobs.FINISHED else {"Retry-After": "2"}
        return JSONResponse(view, headers=headers)

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str):
        await load_job(job_id)  # 404 before starting the stream

        async def stream():
            last = None
            # the stream stays open as long as the job runs; snapshots also refresh queue position and ETA
            async for fields in wait_for_job(job_id, timeout=cfg.jobs.events_timeout_seconds):
                data = json.dumps(await job_snapshot(fields))
                if data != last:
                    yield f"event: job\ndata: {data}\n\n"
                    last = data
                else:
                    yield ": keepalive\n\n"
            yield f"event: end\ndata: {json.dumps({'status': json.loads(last)['status']})}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/jobs/{job_id}/samples/{index}.wav", response_class=Response,
             responses={200: {"content": {"audio/wav": {}}}})
    async def get_sample(job_id: str, index: int):
        fields = await load_job(job_id)
        if not 0 <= index < int(fields["samples_total"]):
            raise HTTPException(404, "no such sample")
        status = fields.get(jobs.sample_field(index, "status"))
        if status != "completed":
            raise HTTPException(409, f"sample is {status}")
        audio = await app.state.rb.get(jobs.sample_key(job_id, index))
        if audio is None:
            raise HTTPException(404, "sample expired")
        return Response(content=audio, media_type="audio/wav")

    @app.delete("/jobs/{job_id}")
    async def cancel_job(job_id: str):
        """Cancel the samples that haven't started; running samples still finish."""
        fields = await load_job(job_id)
        updates = {"cancelled": "1"}
        for i in range(int(fields["samples_total"])):
            if fields.get(jobs.sample_field(i, "status")) == "queued":
                updates[jobs.sample_field(i, "status")] = "cancelled"
        await app.state.r.hset(jobs.job_key(job_id), mapping=updates)
        await app.state.r.publish(jobs.events_channel(job_id), "changed")
        return await job_snapshot(await load_job(job_id))

    @app.post("/generate", response_class=Response,
              responses={200: {"content": {"audio/wav": {}}, "description": "Generated speech as 16-bit WAV"}})
    async def generate(
        text: str = Form(..., description="Text to synthesize"),
        language: str = Form(..., description="Language code (e.g. ch_be) or name (e.g. Bern)"),
        speaker_wav: UploadFile = File(..., description="Reference recording of the target speaker"),
        ref_text: str | None = Form(None, description="Transcript of speaker_wav (see ref_text in /models)"),
        model: str | None = Form(None, description="Model id (see /models)"),
        seed: int | None = Form(None),
    ):
        """Blocking single-sample generation, kept for simple clients. Runs through the same queue."""
        fields = await create_job(text, language, speaker_wav, ref_text, 1, model, seed)
        job_id = fields["id"]
        async for fields in wait_for_job(job_id, timeout=cfg.jobs.generate_timeout_seconds):
            pass
        status = fields.get(jobs.sample_field(0, "status"))
        if status == "completed":
            return await get_sample(job_id, 0)
        if status == "failed":
            raise HTTPException(500, f"generation failed: {fields.get(jobs.sample_field(0, 'error'))}")
        raise HTTPException(504, f"generation did not finish in time; poll /jobs/{job_id}",
                            headers={"Location": f"/jobs/{job_id}"})

    return app


@hydra.main(version_base="1.3", config_path=CONFIG_PATH, config_name="api.yaml")
def main(cfg: DictConfig):
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port)

if __name__ == "__main__":
    main()
