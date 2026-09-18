FROM python:3.12-slim

RUN pip install --no-cache-dir edge-tts==7.2.8 pypdf trafilatura

RUN mkdir -p /app
WORKDIR /app
# self-cloning: REST ile olusturulan dockerfile-pack uygulamalarinda build context bostur
ARG GIT_REF=main
RUN apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1 && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 --branch $GIT_REF https://github.com/sercansolmaz/voice-stuqio.git /tmp/src \
    && cp /tmp/src/app.py /tmp/src/README.md . && cp -r /tmp/src/public ./public && rm -rf /tmp/src

ENV PORT=8000
EXPOSE 8000

CMD ["python", "app.py"]
