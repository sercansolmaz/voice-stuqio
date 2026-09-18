# voice.stuqio.com

Metin / dosya / link → sunucu taraflı seslendirme (MP3) · ses/video → transkript (SRT/TXT/DOCX/PDF) · ses klonlama (XTTS-v2).

## Özellikler

| Sekme | İşlev |
|---|---|
| ✍️ Metin | Yapıştır → 24 dil/sesle MP3 (edge-tts), uzun metin otomatik parçalanır |
| 📄 Dosya | .txt/.md/.csv/.pdf yükle → metni çıkar → seslendir |
| 🔗 Link | URL ver → makale metnini ayıkla (trafilatura) → seslendir |
| 🎙️ Transkript | Ses/video yükle (100MB) → Whisper turbo ile yazıya çevir → SRT/TXT/DOCX/PDF indir |
| 🧬 Klonla | 6-30 sn ses örneği (mikrofon kaydı veya dosya) → XTTS-v2 ile o sesle her metni oku (WAV) |

## Mimari

- **Backend:** Python 3.12 stdlib `http.server` (ThreadingHTTPServer), framework yok.
  - `edge-tts` (TTS) · `faster-whisper` turbo (transkripsiyon) · `trafilatura` (link→metin) · `pypdf` · `python-docx` + `reportlab` (export) · `coqui-tts` XTTS-v2 (klonlama)
  - Async job kuyrukları (transkript + klon), geçici dosyalar iş bitince silinir, hiçbiri kalıcı saklanmaz.
- **Frontend:** tek dosya vanilla JS, glassmorphism tema, tam mobil uyumlu.
- **Sürüm pinleri (kritik):** torch 2.6.0+cpu · coqui-tts 0.27.5[codec] · transformers 4.57.1 — torchcodec/transformers import kırıkları nedeniyle blind upgrade yapılmamalı.
- XTTS-v2 lisansı **CPML** (ticari olmayan kullanım).

## API

| Endpoint | Açıklama |
|---|---|
| `GET /api/voices` | ses listesi + parça karakter sınırı |
| `POST /api/tts` | `{text, voice, rate, pitch}` → `audio/mpeg` |
| `POST /api/fetch` | `{url}` → `{text, title}` (HTML ayıklama, SSRF korumalı) |
| `POST /api/extract` | ham txt/md/csv/pdf gövdesi → `{text}` |
| `POST /api/transcribe` | ham ses/video (≤100MB) → `{jobId}` |
| `GET /api/transcribe/{id}/status` | durum + ilerleme + sonuç |
| `GET /api/transcribe/{id}/download/{srt\|txt\|docx\|pdf}` | transkript indir |
| `POST /api/clone?consent=1&language=tr&text=...` | ham ses örneği gövdesi → `{jobId}` |
| `GET /api/clone/{id}/status` · `/download` | klon durumu / WAV indir |
| `GET /health` | sağlık kontrolü |

## Geliştirme

```bash
pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cpu
pip install edge-tts pypdf trafilatura faster-whisper python-docx reportlab "coqui-tts[codec]" "transformers==4.57.1"
PORT=8901 COQUI_TOS_AGREED=1 python app.py
```

## Dağıtım

Coolify (dockerfile pack, GIT_REF tag'i ile self-cloning), repo `sercansolmaz/voice-stuqio`, domain `https://voice.stuqio.com` (Cloudflare proxied → 89.167.11.23). Kod değişikliği = yeni git tag + Dockerfile'da `ARG GIT_REF` güncelle + uygulamayı yeniden oluştur (dockerfile REST ile patch'lenemiyor).
