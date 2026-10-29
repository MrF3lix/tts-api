import hashlib
import random
from abc import ABC, abstractmethod
from collections import OrderedDict

import numpy as np
import torch


def resolve_device(spec: str = "auto") -> torch.device:
    if spec != "auto":
        return torch.device(spec)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed % 2**32)
    torch.manual_seed(seed)


class Backend(ABC):
    """One loaded model. Not thread-safe; a worker runs one generation at a time.

    Subclasses build a speaker conditioning from the reference recording (and its
    transcript, if the model uses one); it is cached, since all samples of a job
    share the same reference."""

    def __init__(self, model_cfg, speaker_cache_size: int = 32):
        self.cfg = model_cfg
        self._speakers: OrderedDict[str, object] = OrderedDict()
        self._speaker_cache_size = speaker_cache_size

    def language_tag(self, code: str) -> str:
        """What the model expects for a language code, e.g. de -> German."""
        tags = self.cfg.get("language_tags") or {}
        return tags.get(code, code)

    def speaker(self, speaker_wav: bytes, ref_text: str | None):
        key = hashlib.sha256(speaker_wav + b"\0" + (ref_text or "").encode()).hexdigest()
        if key in self._speakers:
            self._speakers.move_to_end(key)
            return self._speakers[key]
        conditioning = self.build_speaker(speaker_wav, ref_text)
        self._speakers[key] = conditioning
        if len(self._speakers) > self._speaker_cache_size:
            self._speakers.popitem(last=False)
        return conditioning

    @abstractmethod
    def build_speaker(self, speaker_wav: bytes, ref_text: str | None):
        """Model-specific conditioning from a 16-bit mono wav and an optional transcript."""

    @abstractmethod
    def generate(self, text: str, language_tag: str, speaker) -> tuple[np.ndarray, int]:
        """Return (mono float32 waveform, sample rate)."""

    def synthesize(self, text: str, language: str, speaker_wav: bytes,
                   ref_text: str | None = None, seed: int | None = None) -> tuple[np.ndarray, int]:
        speaker = self.speaker(speaker_wav, ref_text)
        if seed is not None:
            set_seed(seed)
        wav, sr = self.generate(text, self.language_tag(language), speaker)
        return np.asarray(wav, dtype=np.float32).squeeze(), sr
