import io
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import hydra
import redis
import rootutils
import soundfile as sf
from omegaconf import DictConfig

from tts_api import jobs
from tts_api.backends import Backend, load_backend


rootutils.setup_root(__file__, indicator=".project-root", pythonpath=True)
CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "configs")
log = logging.getLogger(__name__)

# weight of the newest generation time in the moving average used for the ETA
AVG_ALPHA = 0.2


class Worker:
    def __init__(self, cfg: DictConfig, backend: Backend):
        self.cfg = cfg.worker
        self.model_cfg = cfg.model
        self.model = cfg.model.id
        self.backend = backend
        self.name = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.stream = jobs.task_stream(self.model)
        self.r = redis.Redis.from_url(cfg.redis_url, decode_responses=True)
        self.rb = redis.Redis.from_url(cfg.redis_url)  # binary values (audio)
        self.current: str | None = None
        self._stop = threading.Event()

    def _publish(self, job_id: str):
        self.r.publish(jobs.events_channel(job_id), "changed")

    def _ensure_group(self):
        try:
            self.r.xgroup_create(self.stream, jobs.GROUP, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def _register_model(self):
        # the gateway learns which models exist, and what they accept, from this entry
        info = {
            "id": self.model,
            "name": self.model_cfg.model_name,
            "backend": self.model_cfg.backend,
            "ref_text": self.model_cfg.ref_text,
            "languages": dict(self.model_cfg.languages),
        }
        self.r.hset(jobs.MODELS_KEY, self.model, json.dumps(info))

    def _heartbeat_loop(self):
        # separate thread, so a long generation doesn't make the worker look dead
        ttl = self.cfg.heartbeat_ttl_seconds
        while not self._stop.is_set():
            info = json.dumps({"name": self.name, "task": self.current, "at": time.time()})
            self.r.set(jobs.worker_key(self.model, self.name), info, ex=ttl)
            self._stop.wait(ttl / 3)

    def _recover_dead_consumers(self):
        """Take over tasks from workers that crashed mid-generation (their heartbeat expired)."""
        for consumer in self.r.xinfo_consumers(self.stream, jobs.GROUP):
            name = consumer["name"]
            if name == self.name or self.r.exists(jobs.worker_key(self.model, name)):
                continue
            pending = self.r.xpending_range(self.stream, jobs.GROUP, min="-", max="+", count=100, consumername=name)
            if not pending:
                self.r.xgroup_delconsumer(self.stream, jobs.GROUP, name)
                continue
            log.warning(f"Recovering {len(pending)} task(s) from dead worker {name}")
            deliveries = {p["message_id"]: p["times_delivered"] for p in pending}
            claimed = self.r.xclaim(
                self.stream, jobs.GROUP, self.name,
                min_idle_time=self.cfg.heartbeat_ttl_seconds * 1000,
                message_ids=list(deliveries),
            )
            for task_id, fields in claimed:
                if not fields:  # entry was trimmed from the stream
                    self.r.xack(self.stream, jobs.GROUP, task_id)
                elif deliveries[task_id] >= self.cfg.max_deliveries:
                    self._fail(task_id, fields, "worker crashed repeatedly while generating this sample")
                else:
                    self._process(task_id, fields)

    def _fail(self, task_id: str, fields: dict, error: str):
        job_id, index = fields["job_id"], int(fields["index"])
        key = jobs.job_key(job_id)
        if self.r.exists(key):
            self.r.hset(key, mapping={
                jobs.sample_field(index, "status"): "failed",
                jobs.sample_field(index, "error"): error,
                jobs.sample_field(index, "finished_at"): time.time(),
            })
            self._publish(job_id)
        self.r.xack(self.stream, jobs.GROUP, task_id)

    def _process(self, task_id: str, fields: dict):
        job_id, index = fields["job_id"], int(fields["index"])
        key = jobs.job_key(job_id)
        job = self.r.hgetall(key)

        if not job:  # expired before anyone got to it
            self.r.xack(self.stream, jobs.GROUP, task_id)
            return
        if job.get("cancelled") == "1" or job.get(jobs.sample_field(index, "status")) == "cancelled":
            self.r.hset(key, jobs.sample_field(index, "status"), "cancelled")
            self._publish(job_id)
            self.r.xack(self.stream, jobs.GROUP, task_id)
            return

        self.current = f"{job_id}/{index}"
        started = time.time()
        self.r.hset(key, mapping={
            jobs.sample_field(index, "status"): "running",
            jobs.sample_field(index, "worker"): self.name,
            jobs.sample_field(index, "started_at"): started,
        })
        self._publish(job_id)

        try:
            speaker_wav = self.rb.get(jobs.speaker_key(job_id))
            if speaker_wav is None:
                raise RuntimeError("speaker recording expired")
            seed = int(job[jobs.sample_field(index, "seed")])
            wav, sr = self.backend.synthesize(job["text"], job["language"], speaker_wav,
                                              ref_text=job.get("ref_text") or None, seed=seed)

            buf = io.BytesIO()
            sf.write(buf, wav, sr, format='WAV', subtype='PCM_16')
            # results live as long as the job itself
            ttl = max(self.r.ttl(key), 60)
            self.rb.set(jobs.sample_key(job_id, index), buf.getvalue(), ex=ttl)

            elapsed = time.time() - started
            self.r.hset(key, mapping={
                jobs.sample_field(index, "status"): "completed",
                jobs.sample_field(index, "duration"): len(wav) / sr,
                jobs.sample_field(index, "finished_at"): time.time(),
            })
            self._update_avg(elapsed)
            log.info(f"Job {job_id} sample {index}: {len(wav) / sr:.1f}s audio in {elapsed:.1f}s")
        except Exception as e:
            log.exception(f"Job {job_id} sample {index} failed")
            self.r.hset(key, mapping={
                jobs.sample_field(index, "status"): "failed",
                jobs.sample_field(index, "error"): str(e),
                jobs.sample_field(index, "finished_at"): time.time(),
            })
        finally:
            self.current = None
            self._publish(job_id)
            self.r.xack(self.stream, jobs.GROUP, task_id)

    def _update_avg(self, elapsed: float):
        key = jobs.avg_seconds_key(self.model)
        prev = self.r.get(key)
        avg = elapsed if prev is None else (1 - AVG_ALPHA) * float(prev) + AVG_ALPHA * elapsed
        self.r.set(key, avg)

    def run(self):
        self._ensure_group()
        self._register_model()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        log.info(f"Worker {self.name} serving model '{self.model}'")

        last_recovery = 0.0
        try:
            while True:
                try:
                    if time.time() - last_recovery > self.cfg.recovery_interval_seconds:
                        self._recover_dead_consumers()
                        last_recovery = time.time()

                    # keep the block well below redis-py's default 5s socket timeout
                    resp = self.r.xreadgroup(jobs.GROUP, self.name, {self.stream: ">"}, count=1, block=2000)
                    for _, entries in resp or []:
                        for task_id, fields in entries:
                            self.r.decr(jobs.queued_key(self.model))
                            self._process(task_id, fields)
                except (redis.ConnectionError, redis.TimeoutError):
                    # Redis restarting or briefly unreachable; reloading the model would be far slower
                    log.warning("Lost connection to Redis, retrying", exc_info=True)
                    time.sleep(2)
        finally:
            self._stop.set()
            self.r.delete(jobs.worker_key(self.model, self.name))


@hydra.main(version_base="1.3", config_path=CONFIG_PATH, config_name="worker.yaml")
def main(cfg: DictConfig):
    # docker stop sends SIGTERM; exit normally so the heartbeat is removed and another
    # worker can take over an interrupted task without waiting for the heartbeat to expire
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    backend = load_backend(cfg.model, cfg.device, speaker_cache_size=cfg.worker.speaker_cache_size)
    Worker(cfg, backend).run()

if __name__ == "__main__":
    main()
