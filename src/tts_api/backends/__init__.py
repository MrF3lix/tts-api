"""TTS backends a worker can serve. Each model config (configs/model/*.yaml) names its
backend; the backend's heavy dependencies are only imported when it is loaded, so a
worker image only needs the packages of the model it serves."""
from tts_api.backends.base import Backend, resolve_device


def load_backend(model_cfg, device_spec: str = "auto", speaker_cache_size: int = 32) -> Backend:
    device = resolve_device(device_spec)
    if model_cfg.backend == "xtts":
        from tts_api.backends.xtts import XTTSBackend
        return XTTSBackend(model_cfg, device, speaker_cache_size)
    if model_cfg.backend == "qwen3":
        from tts_api.backends.qwen3 import Qwen3Backend
        return Qwen3Backend(model_cfg, device, speaker_cache_size)
    raise ValueError(f"Unknown backend '{model_cfg.backend}' for model '{model_cfg.id}'")
