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

try:
    from faster_whisper import WhisperModel
    HAVE_WHISPER = True
except Exception:
    HAVE_WHISPER = False

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


# ---------- Transkripsiyon ----------

import queue
import threading
import tempfile
import time
import uuid as uuidlib

MAX_MEDIA_BYTES = int(os.environ.get("MAX_MEDIA_BYTES", str(100 * 1024 * 1024)))
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "turbo")
WHISPER_MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", "/models")
JOB_TTL_SECONDS = 3 * 3600
MAX_JOBS_KEPT = 40

JOBS = {}
JOBS_LOCK = threading.Lock()
_whisper_model = None
_whisper_lock = threading.Lock()
TRANS_Q = queue.Queue()

LANG_CODES = {"tr": "Turkce", "en": "Ingilizce", "de": "Almanca", "fr": "Fransizca",
              "es": "Ispanyolca", "it": "Italyanca", "az": "Azerbaycan Turkcesi",
              "ru": "Rusca", "ar": "Arapca", "ja": "Japonca"}


def get_whisper():
    global _whisper_model
    if not HAVE_WHISPER:
        raise ValueError("Transkripsiyon bu sunucuda kapali.")
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            mdir = WHISPER_MODEL_DIR if os.path.isdir(WHISPER_MODEL_DIR) and os.access(WHISPER_MODEL_DIR, os.W_OK) else None
            _whisper_model = WhisperModel(WHISPER_MODEL_NAME, device="cpu",
                                          compute_type="int8",
                                          download_root=mdir)
        return _whisper_model


def prune_jobs():
    now = time.time()
    with JOBS_LOCK:
        finished = [(jid, j) for jid, j in JOBS.items()
                    if j["status"] in ("done", "error")]
        # once eski isleri at
        finished.sort(key=lambda kv: kv[1].get("finished_at", 0))
        while len(finished) > MAX_JOBS_KEPT:
            jid, _ = finished.pop(0)
            JOBS.pop(jid, None)
        # TTL gecenler
        for jid in [jid for jid, j in JOBS.items()
                    if now - j.get("finished_at", now) > JOB_TTL_SECONDS
                    and j["status"] in ("done", "error")]:
            JOBS.pop(jid, None)


def trans_worker():
    while True:
        job_id = TRANS_Q.get()
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            continue
        try:
            job["status"] = "processing"
            model = get_whisper()
            segments, info = model.transcribe(
                job["path"], language=job.get("language") or None,
                vad_filter=True, condition_on_previous_text=False, beam_size=5)
            duration = float(info.duration or 0)
            job["duration"] = duration
            if getattr(info, "language", None):
                job["language_detected"] = str(info.language)
            out, full = [], []
            for seg in segments:
                text = seg.text.strip()
                if not text:
                    continue
                out.append({"start": float(seg.start), "end": float(seg.end), "text": text})
                full.append(text)
                if duration > 0:
                    job["progress"] = min(99, int(float(seg.end) / duration * 100))
            job["segments"] = out
            job["text"] = " ".join(full).strip()
            job["status"] = "done"
            job["progress"] = 100
        except Exception as e:
            job["status"] = "error"
            job["error"] = "Transkripsiyon basarisiz: %s" % e
        finally:
            job["finished_at"] = time.time()
            if job.get("path"):
                try:
                    os.unlink(job["path"])
                except Exception:
                    pass
                job["path"] = None


threading.Thread(target=trans_worker, daemon=True).start()


# ---------- export formatlari ----------

def _srt_ts(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms2)


def build_srt(segments) -> bytes:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append("%s --> %s" % (_srt_ts(seg["start"]), _srt_ts(seg["end"])))
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def build_docx(title: str, text: str, duration: float) -> bytes:
    import docx as docxlib
    d = docxlib.Document()
    d.add_heading(title or "Transkript", 0)
    meta = []
    if duration:
        meta.append("Sure: %d dk %02d sn" % (duration // 60, int(duration % 60)))
    meta.append("voice.stuqio.com")
    p = d.add_paragraph(" · ".join(meta))
    p.runs[0].italic = True
    d.add_paragraph("")
    for para in text.split("\n"):
        d.add_paragraph(para)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def build_pdf(title: str, text: str, duration: float) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font = "Helvetica"
    bold = "Helvetica-Bold"
    if os.path.exists(DEJAVU):
        pdfmetrics.registerFont(TTFont("DejaVu", DEJAVU))
        font = "DejaVu"
        if os.path.exists(DEJAVU_BOLD):
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", DEJAVU_BOLD))
            bold = "DejaVu-Bold"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm)
    h = ParagraphStyle("h", fontName=bold, fontSize=18, leading=22, spaceAfter=6)
    m = ParagraphStyle("m", fontName=font, fontSize=9, leading=12, textColor="#8b949e")
    b = ParagraphStyle("b", fontName=font, fontSize=11, leading=16, alignment=TA_LEFT)
    story = [Paragraph((title or "Transkript").replace("&", "&amp;").replace("<", "&lt;"), h)]
    meta = []
    if duration:
        meta.append("Sure: %d dk %02d sn" % (duration // 60, int(duration % 60)))
    meta.append("voice.stuqio.com")
    story.append(Paragraph(" · ".join(meta), m))
    story.append(Spacer(1, 12))
    for para in text.split("\n"):
        if para.strip():
            story.append(Paragraph(para.strip().replace("&", "&amp;").replace("<", "&lt;"), b))
            story.append(Spacer(1, 6))
    doc.build(story)
    return buf.getvalue()


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
        m = re.match(r"^/api/transcribe/([a-f0-9]{4,32})/status$", path)
        if m:
            return self.handle_status_path(m.group(1))
        m = re.match(r"^/api/transcribe/([a-f0-9]{4,32})/download$", path)
        if m:
            return self.handle_download(m.group(1), None)
        m = re.match(r"^/api/transcribe/([a-f0-9]{4,32})/download/(srt|txt|docx|pdf)$", path)
        if m:
            return self.handle_download(m.group(1), m.group(2))
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
            if path == "/api/transcribe":
                return self.handle_transcribe()
            self.send_json({"error": "Bulunamadi."}, 404)
        except Exception as e:
            logging.exception("hata: %s", e)
            try:
                self.send_json({"error": "Sunucu hatasi: %s" % e}, 500)
            except Exception:
                pass

    def handle_transcribe(self):
        prune_jobs()
        if not HAVE_WHISPER:
            return self.send_json({"error": "Transkripsiyon bu sunucuda kapali."}, 503)
        # gorunmez queue doluluk kontrolu
        if TRANS_Q.qsize() >= 2:
            return self.send_json({"error": "Sunucu musait degil, birazdan tekrar dene."}, 429)
        try:
            data = self.read_body(MAX_MEDIA_BYTES)
        except ValueError as e:
            return self.send_json({"error": "%s (sinir %d MB)." % (e, MAX_MEDIA_BYTES // (1024 * 1024))}, 400)
        if not data or len(data) < 1000:
            return self.send_json({"error": "Dosya bos veya cok kucuk."}, 400)
        # basit format dedeksi (mp3/wav/m4a/mp4/webm/ogg/flac)
        head = data[:64]
        known = False
        sigs = [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xe3", b"RIFF", b"OggS", b"fLaC"]
        for s in sigs:
            if head.startswith(s):
                known = True
                break
        if not known:
            # mp4/m4a/webm: ftyp kutusu ilk bolumlerde
            if b"ftyp" in head:
                known = True
            # clipler: 1sn ~ 1MB alti olabilir
            elif len(data) >= 1000:
                known = True  # stream demuxer genelde halleder, whisper'a birak
        # gecici dosya
        tmpdir = os.environ.get("TMPDIR") or "/tmp"
        os.makedirs(tmpdir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="media-", suffix=".bin", dir=tmpdir)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        job_id = uuidlib.uuid4().hex[:16]
        job = {
            "id": job_id, "status": "queued", "progress": 0,
            "path": tmp_path, "filename": "media", "language": None,
            "created_at": time.time(), "finished_at": 0,
        }
        with JOBS_LOCK:
            JOBS[job_id] = job
        TRANS_Q.put(job_id)
        self.send_json({"jobId": job_id, "status": "queued"})

    def handle_status_path(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            return self.send_json({"error": "Is bulunamadi (suresi dolmus olabilir)."}, 404)
        out = {"status": job["status"], "progress": job.get("progress", 0)}
        if job["status"] == "done":
            out["text"] = job.get("text", "")
            out["segments"] = job.get("segments", [])
            out["duration"] = job.get("duration", 0)
            lang = job.get("language_detected")
            out["language"] = LANG_CODES.get(lang, lang)
        if job["status"] == "error":
            out["error"] = job.get("error", "Bilinmeyen hata.")
        self.send_json(out)

    def handle_download(self, job_id, fmt):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            return self.send_json({"error": "Is bulunamadi."}, 404)
        if job["status"] != "done":
            return self.send_json({"error": "Is henuz tamamlanmadi."}, 400)
        fmt = fmt or "srt"
        base = "transkript-" + job_id
        text = job.get("text", "")
        segments = job.get("segments", [])
        duration = job.get("duration", 0)
        if fmt == "srt":
            return self.send_bytes(build_srt(segments), "application/x-subrip", base + ".srt")
        if fmt == "txt":
            return self.send_bytes(text.encode("utf-8"), "text/plain; charset=utf-8", base + ".txt")
        if fmt == "docx":
            return self.send_bytes(build_docx("Transkript", text, duration),
                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                   base + ".docx")
        if fmt == "pdf":
            return self.send_bytes(build_pdf("Transkript", text, duration),
                                   "application/pdf", base + ".pdf")
        self.send_json({"error": "Bilinmeyen format."}, 400)

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
