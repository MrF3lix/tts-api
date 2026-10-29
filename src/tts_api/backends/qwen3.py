import io
import logging

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

from tts_api.backends.base import Backend


log = logging.getLogger(__name__)


class Qwen3Backend(Backend):
    """Qwen3-TTS Base (voice cloning). With a reference transcript the model clones in
    ICL mode; without one it falls back to the speaker embedding only (x-vector mode),
    which the model card notes gives lower cloning quality."""

    def __init__(self, model_cfg, device, speaker_cache_size: int = 32):
        super().__init__(model_cfg, speaker_cache_size)
        log.info(f'Load TTS model {model_cfg.model_name} ({model_cfg.pretrained}) on {device}')
        self.model = Qwen3TTSModel.from_pretrained(
            model_cfg.pretrained,
            device_map=str(device),
            dtype=getattr(torch, model_cfg.dtype),
            attn_implementation=model_cfg.attn_implementation,
        )

    def build_speaker(self, speaker_wav: bytes, ref_text: str | None):
        audio, sr = sf.read(io.BytesIO(speaker_wav), dtype='float32')
        return self.model.create_voice_clone_prompt(
            ref_audio=(audio, sr),
            ref_text=ref_text,
            x_vector_only_mode=ref_text is None,
        )

    def generate(self, text: str, language_tag: str, speaker):
        wavs, sr = self.model.generate_voice_clone(
            text=text,
            language=language_tag,
            voice_clone_prompt=speaker,
            # the model's own limit (8192 tokens at 12Hz, ~11 min) is far beyond any request;
            # this bounds how long a generation that doesn't stop on its own can run
            max_new_tokens=self.cfg.max_new_tokens,
        )
        return wavs[0], sr
