import os
import tempfile

from tts_ui.backends.base import Backend
from tts_ui.xtts import load_tts


class XTTSBackend(Backend):
    """Coqui XTTS, run directly instead of through tts.tts() so the speaker conditioning
    can be reused. The reference transcript is not used by XTTS."""

    def __init__(self, model_cfg, device, speaker_cache_size: int = 32):
        super().__init__(model_cfg, speaker_cache_size)
        tts = load_tts(model_cfg, device)
        self.model = tts.synthesizer.tts_model
        self.config = tts.synthesizer.tts_config
        self.sample_rate = tts.synthesizer.output_sample_rate

    def build_speaker(self, speaker_wav: bytes, ref_text: str | None):
        # get_conditioning_latents only takes a path
        fd, path = tempfile.mkstemp(suffix='.wav')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(speaker_wav)
            return self.model.get_conditioning_latents(
                audio_path=path,
                gpt_cond_len=self.config.gpt_cond_len,
                gpt_cond_chunk_len=self.config.gpt_cond_chunk_len,
                max_ref_length=self.config.max_ref_len,
                sound_norm_refs=self.config.sound_norm_refs,
            )
        finally:
            os.remove(path)

    def generate(self, text: str, language_tag: str, speaker):
        gpt_cond_latent, speaker_embedding = speaker
        out = self.model.inference(
            text,
            language_tag,
            gpt_cond_latent,
            speaker_embedding,
            temperature=self.config.temperature,
            length_penalty=self.config.length_penalty,
            repetition_penalty=self.config.repetition_penalty,
            top_k=self.config.top_k,
            top_p=self.config.top_p,
        )
        return out["wav"], self.sample_rate
