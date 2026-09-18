FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y -qq git curl fonts-dejavu-core libsndfile1 ffmpeg >/dev/null 2>&1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "torch==2.6.0" "torchaudio==2.6.0" --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir edge-tts==7.2.8 pypdf trafilatura faster-whisper python-docx reportlab \
    librosa==0.10.2 scipy munch einops descript-audio-codec soundfile "transformers==4.46.3" pydub

# Seed-VC kaynagi (GPL-3 kod) + whisper turbo modeli
RUN git clone --depth 1 https://github.com/Plachtaa/seed-vc.git /seedvc \
    && rm -rf /seedvc/.git

# modelleri imaja on_indir: whisper turbo + seed-vc agirliklari
RUN mkdir -p /models /seedvc/checkpoints/hf_cache \
    && python -c "from faster_whisper import WhisperModel; WhisperModel('turbo', device='cpu', compute_type='int8', download_root='/models')" \
    ; HF_HOME=/seedvc/checkpoints/hf_cache python -c "\
import sys; sys.path.insert(0, '/seedvc'); import os; os.chdir('/seedvc'); \
from hf_utils import load_custom_model_from_hf; \
load_custom_model_from_hf('Plachta/Seed-VC', 'DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned.pth', 'config_dit_mel_seed_uvit_whisper_small_wavenet.yml')" \
    || echo "model download skipped (runtime'ta indirilecek)"

RUN mkdir -p /app
WORKDIR /app
# self-cloning: REST ile olusturulan dockerfile-pack uygulamalarinda build context bostur
ARG GIT_REF=v2.1
RUN git clone --depth 1 --branch $GIT_REF https://github.com/sercansolmaz/voice-stuqio.git /tmp/src \
    && cp /tmp/src/app.py /tmp/src/README.md /tmp/src/seed_vc_infer.py . \
    && cp -r /tmp/src/public ./public && rm -rf /tmp/src

ENV PORT=8000 HF_HOME=/seedvc/checkpoints/hf_cache
EXPOSE 8000

CMD ["python", "app.py"]
