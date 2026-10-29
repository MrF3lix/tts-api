import logging

import torch
from TTS.api import TTS
from TTS.tts.layers.xtts.tokenizer import VoiceBpeTokenizer

from tts_api.backends.base import resolve_device  # noqa: F401 (re-exported for the legacy UI)


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
