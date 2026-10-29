import os
import hashlib
import logging
import random
import tempfile
from collections import OrderedDict

import numpy as np
import torch
from TTS.api import TTS
from TTS.tts.layers.xtts.tokenizer import VoiceBpeTokenizer

from tts_ui.languages import LANG_MAP, INV_LANG_MAP, resolve_language  # noqa: F401 (re-exported for the legacy UI)


log = logging.getLogger(__name__)

OUTPUT_SAMPLE_RATE = 24000


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


class Synthesizer:
    """Runs XTTS directly instead of through tts.tts(), so the speaker conditioning
    computed from a reference recording can be reused across samples. Not thread-safe;
    a worker process runs one generation at a time."""

    def __init__(self, tts: TTS, speaker_cache_size: int = 32):
        self.model = tts.synthesizer.tts_model
        self.config = tts.synthesizer.tts_config
        self.sample_rate = OUTPUT_SAMPLE_RATE
        self._speakers: OrderedDict[str, tuple[torch.Tensor, torch.Tensor]] = OrderedDict()
        self._speaker_cache_size = speaker_cache_size

    def _conditioning(self, speaker_wav: bytes):
        key = hashlib.sha256(speaker_wav).hexdigest()
        if key in self._speakers:
            self._speakers.move_to_end(key)
            return self._speakers[key]

        # get_conditioning_latents only takes a path
        fd, path = tempfile.mkstemp(suffix='.wav')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(speaker_wav)
            latents = self.model.get_conditioning_latents(
                audio_path=path,
                gpt_cond_len=self.config.gpt_cond_len,
                gpt_cond_chunk_len=self.config.gpt_cond_chunk_len,
                max_ref_length=self.config.max_ref_len,
                sound_norm_refs=self.config.sound_norm_refs,
            )
        finally:
            os.remove(path)

        self._speakers[key] = latents
        if len(self._speakers) > self._speaker_cache_size:
            self._speakers.popitem(last=False)
        return latents

    def synthesize(self, text: str, language: str, speaker_wav: bytes, seed: int | None = None) -> np.ndarray:
        lang_tag = resolve_language(language)
        gpt_cond_latent, speaker_embedding = self._conditioning(speaker_wav)
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed % 2**32)
            torch.manual_seed(seed)
        out = self.model.inference(
            text,
            lang_tag,
            gpt_cond_latent,
            speaker_embedding,
            temperature=self.config.temperature,
            length_penalty=self.config.length_penalty,
            repetition_penalty=self.config.repetition_penalty,
            top_k=self.config.top_k,
            top_p=self.config.top_p,
        )
        return np.asarray(out["wav"], dtype=np.float32).squeeze()
