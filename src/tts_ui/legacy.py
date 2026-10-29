import os
import torch
import hydra
import logging
import rootutils
import librosa
import gradio as gr
import soundfile as sf
from TTS.api import TTS
from TTS.tts.layers.xtts.tokenizer import VoiceBpeTokenizer
from pathlib import Path
from omegaconf import DictConfig, OmegaConf


rootutils.setup_root(__file__, indicator=".project-root", pythonpath=True)
CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "configs")
log = logging.getLogger(__name__)

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

def ensure_dirs(cfg):
    os.makedirs(cfg.paths.speaker_dir, exist_ok=True)
    os.makedirs(SPEAKER_WAV_BPATH, exist_ok=True)
    os.makedirs(SPEAKER_RAW_WAV_BPATH, exist_ok=True)
    os.makedirs(OUTPUT_WAV_BPATH, exist_ok=True)


SPEAKER_WAV_BPATH = 'tmp/speaker_wavs'
SPEAKER_RAW_WAV_BPATH = 'tmp/speaker_raw_wavs'
OUTPUT_WAV_BPATH = 'tmp/generated_audios'


global tts


def generate_speech(text, speaker_wav, language):
    lang_tag = INV_LANG_MAP[language]

    #save speaker wav into tmp/speaker_wavs with increasing numbering
    #get max number in speaker_wavs if any and increment by 1
    generated_wavs = list(os.listdir(OUTPUT_WAV_BPATH))
    if len(generated_wavs) == 0:
        max_number = 0
    else:
        max_number = max([int(x.replace('.wav', '')) for x in generated_wavs])

    speaker_wav_path = os.path.join(SPEAKER_WAV_BPATH, f'{max_number+1}.wav')
    out_wav_path = os.path.join(OUTPUT_WAV_BPATH, f'{max_number+1}.wav')

    # read the upload ourselves: gradio's pydub-based decoding fails on 64-bit wavs
    data, sample_rate = sf.read(speaker_wav, dtype='float32')
    if data.ndim > 1:
        data = data.mean(axis=1)
    sf.write(speaker_wav_path, data, sample_rate, subtype='PCM_16')

    tts.tts_to_file(text=text, speaker_wav=speaker_wav_path, language=lang_tag, split_sentences=False, file_path=out_wav_path)
    #load audio file
    audio, sr = librosa.load(out_wav_path, sr=None)
    return sr, audio



def run(cfg):
    global tts
    ensure_dirs(cfg)
    device = resolve_device(cfg.device)

    log.info(f'Load TTS model {cfg.model.model_name}')
    tts = TTS(
        model_path=cfg.model.model_path,
        config_path=cfg.model.config_path,
        progress_bar=True
    ).to(device)
    tts.eval()

    tts.synthesizer.output_sample_rate = 24000

    app = gr.Interface(
        fn = generate_speech,
        inputs=[
            gr.Textbox(lines=2, label="text"),
            gr.Audio(label="speaker_wav", type="filepath"),
            gr.Dropdown(list(LANG_MAP.values()), label="Language")],
        outputs="audio")
    app.launch()


@hydra.main(version_base="1.3", config_path=CONFIG_PATH, config_name="legacy.yaml")
def main(cfg: DictConfig):
    return run(cfg)

if __name__ == "__main__":
    main()