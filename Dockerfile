FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y -qq git curl fonts-dejavu-core libsndfile1 ffmpeg >/dev/null 2>&1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "torch==2.6.0" "torchaudio==2.6.0" --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir edge-tts==7.2.8 pypdf trafilatura faster-whisper python-docx reportlab \
    "coqui-tts[codec]==0.27.5" "transformers==4.46.2"

# whisper turbo + xtts-v2 modellerini imaja gom
RUN mkdir -p /models \
    && python -c "from faster_whisper import WhisperModel; WhisperModel('turbo', device='cpu', compute_type='int8', download_root='/models')" \
    && COQUI_TOS_AGREED=1 python -c "from huggingface_hub import snapshot_download; snapshot_download('coqui/XTTS-v2', local_dir='/models/xtts', ignore_patterns=['*.whl','README.md','*.png'])" \
    || echo "model download skipped (runtime'ta indirilecek)"

RUN mkdir -p /app
WORKDIR /app
# self-cloning: REST ile olusturulan dockerfile-pack uygulamalarinda build context bostur
ARG GIT_REF=v1.5
RUN git clone --depth 1 --branch $GIT_REF https://github.com/sercansolmaz/voice-stuqio.git /tmp/src \
    && cp /tmp/src/app.py /tmp/src/README.md . && cp -r /tmp/src/public ./public && rm -rf /tmp/src

ENV PORT=8000 COQUI_TOS_AGREED=1
EXPOSE 8000

CMD ["python", "app.py"]
