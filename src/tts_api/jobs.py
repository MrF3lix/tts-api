"""Redis layout shared by the gateway and the workers.

A job is one request for N samples. Each sample is one task on the model's stream,
so N samples spread over all free workers. Per job:

    tts:job:{id}                  hash   job fields + per-sample fields "s{i}.<field>"
    tts:job:{id}:speaker          bytes  normalized reference recording (16-bit mono wav)
    tts:job:{id}:sample:{i}       bytes  generated wav
    tts:job:{id}:events           pubsub "changed" notifications for live progress

Models:

    tts:models                    hash   model id -> JSON metadata (name, languages, ref_text policy),
                                         written by the model's workers on startup

Per model:

    tts:tasks:{model}             stream one entry per sample, consumer group "workers"
    tts:queued:{model}            int    samples enqueued but not yet picked up (queue cap)
    tts:stats:{model}:avg_seconds float  moving average of generation time (for the ETA)
    tts:worker:{model}:{name}     string heartbeat with TTL, one per live worker
"""

GROUP = "workers"
SAMPLE_FIELDS = ("status", "seed", "task_id", "worker", "duration", "error", "started_at", "finished_at")
FINISHED = {"completed", "failed", "cancelled"}


MODELS_KEY = "tts:models"


def job_key(job_id: str) -> str:
    return f"tts:job:{job_id}"

def speaker_key(job_id: str) -> str:
    return f"tts:job:{job_id}:speaker"

def sample_key(job_id: str, index: int) -> str:
    return f"tts:job:{job_id}:sample:{index}"

def events_channel(job_id: str) -> str:
    return f"tts:job:{job_id}:events"

def task_stream(model: str) -> str:
    return f"tts:tasks:{model}"

def queued_key(model: str) -> str:
    return f"tts:queued:{model}"

def avg_seconds_key(model: str) -> str:
    return f"tts:stats:{model}:avg_seconds"

def worker_key(model: str, name: str) -> str:
    return f"tts:worker:{model}:{name}"

def worker_pattern(model: str) -> str:
    return f"tts:worker:{model}:*"


def sample_field(index: int, field: str) -> str:
    return f"s{index}.{field}"


def job_status(fields: dict) -> str:
    total = int(fields["samples_total"])
    statuses = [fields.get(sample_field(i, "status"), "queued") for i in range(total)]
    if fields.get("cancelled") == "1":
        return "cancelled"
    if all(s in FINISHED for s in statuses):
        return "completed" if "completed" in statuses else "failed"
    if any(s != "queued" for s in statuses):
        return "running"
    return "queued"


def is_finished(fields: dict) -> bool:
    total = int(fields["samples_total"])
    return all(fields.get(sample_field(i, "status"), "queued") in FINISHED for i in range(total))


def job_view(fields: dict) -> dict:
    """Public representation of a job, built from its Redis hash."""
    job_id = fields["id"]
    total = int(fields["samples_total"])
    samples = []
    for i in range(total):
        s = {f: fields.get(sample_field(i, f)) for f in SAMPLE_FIELDS}
        sample = {"index": i, "status": s["status"] or "queued", "seed": int(s["seed"])}
        if sample["status"] == "completed":
            sample["url"] = f"/jobs/{job_id}/samples/{i}.wav"
            sample["duration_s"] = round(float(s["duration"]), 2)
        if s["error"]:
            sample["error"] = s["error"]
        samples.append(sample)

    return {
        "id": job_id,
        "status": job_status(fields),
        "model": fields["model"],
        "language": fields["language"],
        "text": fields["text"],
        "ref_text": fields.get("ref_text") or None,
        "created_at": float(fields["created_at"]),
        "samples_total": total,
        "samples_completed": sum(s["status"] == "completed" for s in samples),
        "samples": samples,
    }
