# voice.stuqio.com

Metin / dosya / link → sunucu taraflı seslendirme (MP3).

- **Backend:** Python 3.12 stdlib HTTP + [edge-tts](https://pypi.org/project/edge-tts/) (Microsoft Edge neural sesler), PDF için `pypdf`.
- **Frontend:** tek dosya vanilla JS arayüz (`public/index.html`).
- Uzun metinler istemcide ~4500 karakterlik parçalara bölünür, her parça sunucuda MP3'e çevrilir, tarayıcıda tek dosya olarak birleştirilir.

## API

| Endpoint | Açıklama |
|---|---|
| `GET /api/voices` | ses listesi + parça karakter sınırı |
| `POST /api/tts` | `{text, voice, rate, pitch}` → `audio/mpeg` |
| `POST /api/fetch` | `{url}` → `{text, title}` (HTML ayıklama, SSRF korumalı) |
| `POST /api/extract` | ham dosya gövdesi (txt/md/csv/pdf) → `{text}` |
| `GET /health` | sağlık kontrolü |

## Geliştirme

```bash
pip install edge-tts pypdf
PORT=8000 python app.py
```

## Dağıtım

Coolify (dockerfile pack), `sercansolmaz/voice-stuqio` repo, domain `https://voice.stuqio.com` (Cloudflare proxied → 89.167.11.23).
