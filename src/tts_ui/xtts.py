import os
import logging
import tempfile
import threading

import numpy as np
import soundfile as sf
import torch
from TTS.api import TTS
from TTS.tts.layers.xtts.tokenizer import VoiceBpeTokenizer


log = logging.getLogger(__name__)

OUTPUT_SAMPLE_RATE = 24000

LANG_MAP = {
    'ch_be': 'Bern',
    'ch_bs': 'Basel',
    'ch_gr': 'Graubünden',
    'ch_in': 'Innerschweiz',
    'ch_os': 'Ostschweiz',
    'ch_vs': 'Wallis',
    'ch_zh': 'Zürich',
    'de': 'Deutsch'
}

INV_LANG_MAP = {v:k for k,v in LANG_MAP.items()}


# stock coqui tokenizer only cleans text for a fixed language list. For the dialects,
# clean the text with the German rules; the dialect token ([ch_be], ...) is still
# what gets prepended in encode(), so the model conditioning is unchanged
_preprocess_text = VoiceBpeTokenizer.preprocess_text

def _preprocess_text_dialects(self, txt, lang):
    if lang.startswith('ch_'):
        lang = 'de'
    return _preprocess_text(self, txt, lang)

VoiceBpeTokenizer.preprocess_text = _preprocess_text_dialects


def resolve_device(spec: str = "auto") -> torch.device:
    if spec != "auto":
        return torch.device(spec)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def resolve_language(language: str) -> str:
    """Accept either a language code (ch_be) or its display name (Bern)."""
    if language in LANG_MAP:
        return language
    if language in INV_LANG_MAP:
        return INV_LANG_MAP[language]
    raise ValueError(f"Unknown language '{language}', expected one of {list(LANG_MAP)}")


def load_tts(model_cfg, device: torch.device) -> TTS:
    log.info(f'Load TTS model {model_cfg.model_name} on {device}')
    tts = TTS(
        model_path=model_cfg.model_path,
        config_path=model_cfg.config_path,
        progress_bar=True
    ).to(device)
    tts.eval()
    tts.synthesizer.output_sample_rate = OUTPUT_SAMPLE_RATE
    return tts


def to_mono_float32(data: np.ndarray) -> np.ndarray:
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data.astype(np.float32)


class Synthesizer:
    """Thin wrapper around the coqui model. The model is not safe to call concurrently,
    so generation is serialized with a lock."""

    def __init__(self, tts: TTS):
        self.tts = tts
        self.sample_rate = OUTPUT_SAMPLE_RATE
        self._lock = threading.Lock()

    def synthesize(self, text: str, speaker_audio: np.ndarray, speaker_sr: int, language: str) -> np.ndarray:
        lang_tag = resolve_language(language)
        # XTTS computes the speaker conditioning from a file path, so hand it a clean
        # 16-bit mono copy of the reference (also avoids decoding issues with 64-bit wavs)
        fd, speaker_path = tempfile.mkstemp(suffix='.wav')
        os.close(fd)
        try:
            sf.write(speaker_path, to_mono_float32(speaker_audio), speaker_sr, subtype='PCM_16')
            with self._lock:
                wav = self.tts.tts(text=text, speaker_wav=speaker_path, language=lang_tag, split_sentences=False)
        finally:
            os.remove(speaker_path)
        return np.asarray(wav, dtype=np.float32)
