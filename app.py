#!/usr/bin/env python3
"""voice.stuqio.com - metni/dosyayi/linki sese ceviren servis (edge-tts + stdlib HTTP)."""
import io
import json
import os
import re
import sys
import html as htmllib
import ipaddress
import socket
import asyncio
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import edge_tts

try:
    from pypdf import PdfReader
    HAVE_PYPDF = True
except Exception:
    HAVE_PYPDF = False

try:
    import trafilatura
    HAVE_TRAFILATURA = True
except Exception:
    HAVE_TRAFILATURA = False

PORT = int(os.environ.get("PORT", "8000"))
MAX_TTS_CHARS = int(os.environ.get("MAX_TTS_CHARS", "4500"))
MAX_FETCH_BYTES = 3 * 1024 * 1024      # link ile cekilen sayfa ust siniri
MAX_UPLOAD_BYTES = 12 * 1024 * 1024    # dosya yukleme ust siniri
FETCH_TIMEOUT = 25

# Secilmis sesler (Microsoft Edge TTS). TR basta.
VOICES = [
    {"id": "tr-TR-AhmetNeural", "label": "Ahmet", "lang": "Turkce", "gender": "erkek"},
    {"id": "tr-TR-EmelNeural", "label": "Emel", "lang": "Turkce", "gender": "kadin"},
    {"id": "en-US-AndrewNeural", "label": "Andrew", "lang": "Ingilizce (US)", "gender": "erkek"},
    {"id": "en-US-AvaNeural", "label": "Ava", "lang": "Ingilizce (US)", "gender": "kadin"},
    {"id": "en-US-GuyNeural", "label": "Guy", "lang": "Ingilizce (US)", "gender": "erkek"},
    {"id": "en-US-JennyNeural", "label": "Jenny", "lang": "Ingilizce (US)", "gender": "kadin"},
    {"id": "en-GB-RyanNeural", "label": "Ryan", "lang": "Ingilizce (GB)", "gender": "erkek"},
    {"id": "en-GB-SoniaNeural", "label": "Sonia", "lang": "Ingilizce (GB)", "gender": "kadin"},
    {"id": "de-DE-ConradNeural", "label": "Conrad", "lang": "Almanca", "gender": "erkek"},
    {"id": "de-DE-KatjaNeural", "label": "Katja", "lang": "Almanca", "gender": "kadin"},
    {"id": "fr-FR-HenriNeural", "label": "Henri", "lang": "Fransizca", "gender": "erkek"},
    {"id": "fr-FR-DeniseNeural", "label": "Denise", "lang": "Fransizca", "gender": "kadin"},
    {"id": "es-ES-AlvaroNeural", "label": "Alvaro", "lang": "Ispanyolca", "gender": "erkek"},
    {"id": "es-ES-ElviraNeural", "label": "Elvira", "lang": "Ispanyolca", "gender": "kadin"},
    {"id": "it-IT-DiegoNeural", "label": "Diego", "lang": "Italyanca", "gender": "erkek"},
    {"id": "it-IT-ElsaNeural", "label": "Elsa", "lang": "Italyanca", "gender": "kadin"},
    {"id": "az-AZ-BabekNeural", "label": "Babek", "lang": "Azerbaycan Turkcesi", "gender": "erkek"},
    {"id": "az-AZ-BanuNeural", "label": "Banu", "lang": "Azerbaycan Turkcesi", "gender": "kadin"},
    {"id": "ru-RU-DmitryNeural", "label": "Dmitry", "lang": "Rusca", "gender": "erkek"},
    {"id": "ru-RU-SvetlanaNeural", "label": "Svetlana", "lang": "Rusca", "gender": "kadin"},
    {"id": "ar-SA-HamedNeural", "label": "Hamed", "lang": "Arapca", "gender": "erkek"},
    {"id": "ar-SA-ZariyahNeural", "label": "Zariyah", "lang": "Arapca", "gender": "kadin"},
    {"id": "ja-JP-KeitaNeural", "label": "Keita", "lang": "Japonca", "gender": "erkek"},
    {"id": "ja-JP-NanamiNeural", "label": "Nanami", "lang": "Japonca", "gender": "kadin"},
]
VOICE_IDS = {v["id"] for v in VOICES}

RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
PITCH_RE = re.compile(r"^[+-]\d{1,2}Hz$")

# ---------- HTML -> metin ----------

TAG_BLOCK = re.compile(
    r"<(script|style|noscript|svg|template|iframe|nav|header|footer|aside|form|button|select|option|label|figure)\b[^>]*>.*?</\1\s*>",
    re.S | re.I)
TAG_COMMENT = re.compile(r"<!--.*?-->", re.S)
TAG_BREAK = re.compile(r"<(?:br|/p|/div|/li|/h[1-6]|/tr|/section|/article|/blockquote|/td)\b[^>]*>", re.I)
TAG_ANY = re.compile(r"<[^>]+>")
WS_SPACES = re.compile(r"[ \t\xa0]+")
MANY_NEWLINES = re.compile(r"\n{3,}")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def html_to_text(raw_html: str) -> str:
    h = TAG_BLOCK.sub(" ", raw_html)
    h = TAG_COMMENT.sub(" ", h)
    h = TAG_BREAK.sub("\n", h)
    h = TAG_ANY.sub("", h)
    h = htmllib.unescape(h)
    h = WS_SPACES.sub(" ", h)
    h = MANY_NEWLINES.sub("\n\n", h)
    lines = [ln.strip() for ln in h.split("\n")]
    return "\n".join(lines).strip()


MD_TABLE_SEP = re.compile(r"^\|?[\s:\-|]+\|?$", re.M)
MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def tts_friendly(text: str) -> str:
    """Markdown kalintilarini TTS icin temizle (tablo ayraclari, linkler)."""
    text = MD_LINK.sub(r"\1", text)
    text = MD_TABLE_SEP.sub("", text)
    text = text.replace("|", " ")
    text = MANY_NEWLINES.sub("\n\n", text)
    return text.strip()


def decode_bytes(data: bytes, content_type: str = "") -> str:
    charset = None
    m = re.search(r"charset=[\"']?([\w\-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m = re.search(rb"charset=[\"']?([\w\-]+)", data[:4096])
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for cs in [charset, "utf-8"]:
        if not cs:
            continue
        try:
            return data.decode(cs)
        except Exception:
            continue
    return data.decode("utf-8", "replace")


# ---------- SSRF korumasi ----------

def host_is_public(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def fetch_url_text(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Sadece http/https linkler desteklenir.")
    if not host_is_public(p.hostname):
        raise ValueError("Bu adrese izin verilmiyor.")
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; stuqio-voice/1.0)",
        "Accept": "text/html,text/plain,application/json,*/*",
    })
    with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        ctype = resp.headers.get("Content-Type", "")
        data = resp.read(MAX_FETCH_BYTES + 1)
    if len(data) > MAX_FETCH_BYTES:
        raise ValueError("Sayfa cok buyuk (3MB siniri).")
    text = decode_bytes(data, ctype)
    looks_html = "html" in ctype.lower() or re.search(r"<\s*(html|body|div|p)\b", text[:5000], re.I)
    title = None
    if looks_html:
        m = TITLE_RE.search(text)
        if m:
            title = WS_SPACES.sub(" ", htmllib.unescape(TAG_ANY.sub("", m.group(1)))).strip() or None
        extracted = None
        if HAVE_TRAFILATURA:
            try:
                extracted = trafilatura.extract(text, include_comments=False,
                                                include_tables=True, favor_recall=True)
            except Exception:
                extracted = None
        if extracted and len(extracted.strip()) > 120:
            text = extracted.strip()
        else:
            text = html_to_text(text)
    if not text.strip():
        raise ValueError("Sayfadan metin cikarilamadi.")
    return {"text": text, "title": title, "contentType": ctype}


def extract_upload_text(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        if not HAVE_PYPDF:
            raise ValueError("PDF destegi bu sunucuda kapali.")
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    continue
            text = "\n\n".join(pages).strip()
        except Exception:
            raise ValueError("PDF okunamadi (bozuk veya sifreli olabilir).")
        if not text:
            raise ValueError("PDF icinde metin bulunamadi (taranmis goruntu olabilir).")
        return text
    # duz metin dosyasi (.txt, .md, .csv ...)
    if b"\x00" in data[:4096]:
        raise ValueError("Bu dosya turu desteklenmiyor (sadece txt, md, pdf).")
    return decode_bytes(data, "text/plain; charset=utf-8")


# ---------- TTS ----------

def synth_to_mp3(text: str, voice: str, rate: str, pitch: str) -> bytes:
    async def _run():
        com = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        buf = io.BytesIO()
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    return asyncio.run(asyncio.wait_for(_run(), timeout=120))


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "stuqio-voice/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stdout.write("%s [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    # --- yardimcilar ---
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data: bytes, ctype: str, filename: str = None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.end_headers()
        self.wfile.write(data)

    def read_body(self, cap: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > cap:
            raise ValueError("Istek cok buyuk.")
        return self.rfile.read(length)

    # --- GET ---
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(os.path.dirname(__file__), "public", "index.html"), "rb") as f:
                    return self.send_bytes(f.read(), "text/html; charset=utf-8")
            except Exception:
                return self.send_json({"error": "Arayuz yuklenemedi."}, 500)
        if path == "/health":
            return self.send_json({"ok": True, "service": "voice.stuqio.com"})
        if path == "/api/voices":
            return self.send_json({"voices": VOICES, "maxChars": MAX_TTS_CHARS})
        if path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            return self.send_bytes(body, "text/plain")
        if path == "/favicon.ico":
            return self.send_bytes(b"", "image/x-icon")
        self.send_json({"error": "Bulunamadi."}, 404)

    # --- POST ---
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/tts":
                return self.handle_tts()
            if path == "/api/fetch":
                return self.handle_fetch()
            if path == "/api/extract":
                return self.handle_extract()
            self.send_json({"error": "Bulunamadi."}, 404)
        except Exception as e:
            logging.exception("hata: %s", e)
            try:
                self.send_json({"error": "Sunucu hatasi: %s" % e}, 500)
            except Exception:
                pass

    def handle_tts(self):
        body = self.read_body(2 * 1024 * 1024)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self.send_json({"error": "Gecersiz JSON."}, 400)
        text = (payload.get("text") or "").strip()
        text = tts_friendly(text) if text else text
        voice = payload.get("voice") or "tr-TR-AhmetNeural"
        rate = payload.get("rate") or "+0%"
        pitch = payload.get("pitch") or "+0Hz"
        if not text:
            return self.send_json({"error": "Metin bos."}, 400)
        if len(text) > MAX_TTS_CHARS:
            return self.send_json({"error": "Metin cok uzun (sinir %d karakter)." % MAX_TTS_CHARS}, 400)
        if voice not in VOICE_IDS:
            return self.send_json({"error": "Bilinmeyen ses."}, 400)
        if not RATE_RE.match(rate):
            return self.send_json({"error": "Gecersiz hiz."}, 400)
        if not PITCH_RE.match(pitch):
            return self.send_json({"error": "Gecersiz perde."}, 400)
        try:
            mp3 = synth_to_mp3(text, voice, rate, pitch)
        except Exception as e:
            return self.send_json({"error": "Seslendirme servisi yanit vermedi (%s)." % e}, 502)
        if not mp3:
            return self.send_json({"error": "Ses olusturulamadi."}, 502)
        self.send_bytes(mp3, "audio/mpeg")

    def handle_fetch(self):
        body = self.read_body(64 * 1024)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self.send_json({"error": "Gecersiz JSON."}, 400)
        url = (payload.get("url") or "").strip()
        if not url:
            return self.send_json({"error": "Link bos."}, 400)
        if not re.match(r"^https?://", url, re.I):
            return self.send_json({"error": "Link http:// veya https:// ile baslamali."}, 400)
        try:
            result = fetch_url_text(url)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception:
            return self.send_json({"error": "Sayfa cekilemedi (site erisimi engellemis olabilir)."}, 502)
        if len(result["text"]) > 200000:
            result["text"] = result["text"][:200000] + "\n\n... (metin kirpildi)"
        self.send_json(result)

    def handle_extract(self):
        try:
            data = self.read_body(MAX_UPLOAD_BYTES)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if not data:
            return self.send_json({"error": "Dosya bos."}, 400)
        try:
            text = extract_upload_text(data)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if len(text) > 200000:
            text = text[:200000] + "\n\n... (metin kirpildi)"
        self.send_json({"text": text})


def main():
    logging.basicConfig(level=logging.INFO)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("voice server %d portunda calisiyor" % PORT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
