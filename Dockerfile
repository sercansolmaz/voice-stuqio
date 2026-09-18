FROM python:3.12-slim

RUN pip install --no-cache-dir edge-tts==7.2.8 pypdf trafilatura

WORKDIR /app
COPY app.py .
COPY public ./public

ENV PORT=8000
EXPOSE 8000

CMD ["python", "app.py"]
