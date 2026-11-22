"""ربات استودیوی داستان — پایپ‌لاین ۵ مرحله‌ای (داستان ← شخصیت‌ها ← متن و صدا ← تصاویر ← ادیت ویدیو).

تمام کد توی همین یک فایل است. ورودی هر مرحله از «مستر پرامپت» چت‌جی‌پی‌تی می‌آید؛
فرمت‌های ورودی را با /format داخل ربات ببین.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import unicodedata
import wave
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
import arabic_reshaper
import edge_tts
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, ErrorEvent, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)
from aiogram.utils.chat_action import ChatActionSender

try:  # python-bidi >= 0.5
    from bidi import get_display
except ImportError:  # python-bidi 0.4.x
    from bidi.algorithm import get_display

log = logging.getLogger("storybot")

# ======================================================================
# config
# ======================================================================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_str(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


# --- Secrets: فقط از Variables ریلوی خونده می‌شن (هیچ توکنی توی کد نیست) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
ADMIN_ID = _get_int("ADMIN_ID", 6206120591)
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip() or None

# --- Image generation (OpenRouter Image API، با پشتیبانی از عکس مرجع) ---------------
# چند مدل با ویرگول؛ اگه اولی خطا داد بعدی امتحان می‌شه.
IMAGE_MODELS = [m.strip() for m in _get_str("IMAGE_MODEL", "google/gemini-2.5-flash-image").split(",") if m.strip()]
IMAGE_RESOLUTION = os.getenv("IMAGE_RESOLUTION", "").strip()      # اختیاری: 1K / 2K
# --- Cloudflare Workers AI (رایگان: FLUX.2 با تا ۴ عکس مرجع) ----------------------
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "").strip()
CF_IMAGE_MODEL = _get_str("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b")
# اگه CF_ACCOUNT_ID و CF_API_TOKEN ست باشن، خودکار cloudflare؛ وگرنه openrouter (پولی)
IMAGE_PROVIDER = _get_str("IMAGE_PROVIDER", "").lower() or ("cloudflare" if CF_ACCOUNT_ID and CF_API_TOKEN else "openrouter")
IMAGE_LABEL = CF_IMAGE_MODEL if IMAGE_PROVIDER == "cloudflare" else IMAGE_MODELS[0]
IMAGE_CONCURRENCY = max(1, _get_int("IMAGE_CONCURRENCY", 2))

# --- Limits ------------------------------------------------------------------
MAX_CHARACTERS = max(1, min(_get_int("MAX_CHARACTERS", 8), 12))
MAX_SCENES = max(3, min(_get_int("MAX_SCENES", 40), 80))
MAX_LINE_CHARS = 220          # خط بلندتر از این به جمله‌های کوتاه‌تر شکسته می‌شه

# --- Video -------------------------------------------------------------------
FPS = 25
ASPECTS = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1080, 1080)}
TRANSITION_SEC = 12 / FPS     # ۰٫۴۸ ثانیه؛ مضرب دقیق فریم (برای جلوگیری از ناهم‌زمانی صدا)
SCENE_GAP = 0.45              # سکوت بین صحنه‌ها
RENDER_CONCURRENCY = max(1, _get_int("RENDER_CONCURRENCY", 1))
TG_VIDEO_LIMIT = 49 * 1024 * 1024

# --- Voices (edge-tts) -------------------------------------------------------
VOICES = {   # (male, female)
    "fa": ("fa-IR-FaridNeural", "fa-IR-DilaraNeural"),
    "en": ("en-US-GuyNeural", "en-US-JennyNeural"),
    "ar": ("ar-SA-HamedNeural", "ar-SA-ZariyahNeural"),
    "tr": ("tr-TR-AhmetNeural", "tr-TR-EmelNeural"),
}
TTS_MALE_OVERRIDE = os.getenv("TTS_MALE_VOICE", "").strip()
TTS_FEMALE_OVERRIDE = os.getenv("TTS_FEMALE_VOICE", "").strip()
TTS_CONCURRENCY = 3

# --- Paths -------------------------------------------------------------------
# روی ریلوی یه Volume وصل کن و DATA_DIR=/data بذار تا پروژه‌ها بعد از دیپلوی بمونن.
DATA_DIR = Path(os.getenv("DATA_DIR", "").strip() or (BASE_DIR / "data"))
SESSIONS_DIR = DATA_DIR / "sessions"
PROJECTS_DIR = DATA_DIR / "projects"
FONT_DIR = BASE_DIR / "assets" / "fonts"


def ensure_dirs() -> None:
    for d in (DATA_DIR, SESSIONS_DIR, PROJECTS_DIR, FONT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def check() -> list[str]:
    problems: list[str] = []
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        problems.append("BOT_TOKEN توی Variables ریلوی تنظیم نشده یا نامعتبره.")
    if IMAGE_PROVIDER == "cloudflare":
        if not (CF_ACCOUNT_ID and CF_API_TOKEN):
            problems.append("برای ساخت تصویر با Cloudflare، هم CF_ACCOUNT_ID و هم CF_API_TOKEN لازمه.")
    elif not OPENROUTER_API_KEY:
        problems.append("برای ساخت تصویر یکی از این دو لازمه: CF_ACCOUNT_ID + CF_API_TOKEN (رایگان) یا OPENROUTER_API_KEY (پولی).")
    if not ADMIN_ID:
        problems.append("ADMIN_ID (آیدی عددی ادمین) تنظیم نشده.")
    return problems


# ======================================================================
# text helpers
# ======================================================================
_EMOJI_RE = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d\u200e\u200f]")


def clean_text(text) -> str:
    """Remove emojis/markdown leftovers and collapse whitespace."""
    text = _EMOJI_RE.sub("", str(text or ""))
    text = text.replace("*", "").replace("#", "").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def norm_name(s) -> str:
    """Normalise a name for fuzzy matching (ی/ي، ک/ك، نیم‌فاصله، اعراب، حروف بزرگ)."""
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = s.replace("ي", "ی").replace("ك", "ک")
    s = re.sub(r"[\u200c\u200d\u200e\u200f\u064b-\u065f]", "", s)
    s = re.sub(r"[^\w\s]", "", s, flags=re.U)
    return re.sub(r"\s+", " ", s).strip().lower()


NARRATOR = "راوی"
_NARRATOR_ALIASES = {norm_name(x) for x in (
    "narrator", "راوی", "نریتور", "voiceover", "voice over", "vo", "narration", "گوینده", "narrator voice")}


def is_narrator(raw) -> bool:
    n = norm_name(raw)
    return n == "" or n in _NARRATOR_ALIASES


def match_name(name, known: list[str]) -> str | None:
    n = norm_name(name)
    if not n:
        return None
    for k in known:
        if norm_name(k) == n:
            return k
    close = [k for k in known if n in norm_name(k) or norm_name(k) in n]
    return close[0] if len(close) == 1 else None


def pick(d: dict, *keys, default=None):
    """First non-empty value among several possible keys (case-insensitive)."""
    if not isinstance(d, dict):
        return default
    lower = {norm_name(k): v for k, v in d.items()}
    for k in keys:
        v = lower.get(norm_name(k))
        if v not in (None, "", [], {}):
            return v
    return default


def to_float(v, default: float | None = None) -> float | None:
    try:
        return float(str(v).replace("s", "").replace("ثانیه", "").strip())
    except (TypeError, ValueError):
        return default


def to_int(v, default: int | None = None) -> int | None:
    m = re.search(r"-?\d+", str(v if v is not None else ""))
    return int(m.group(0)) if m else default


# ---------------------------------------------------------------------- loose JSON
_SMART = str.maketrans({"“": '"', "”": '"', "„": '"', "‟": '"', "‘": "'", "’": "'"})


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[A-Za-z0-9_-]*[ \t]*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def is_json_like(text: str) -> bool:
    return _strip_fences(text)[:1] in ("{", "[")


def json_incomplete(text: str) -> bool:
    """True if braces/quotes are still open (the message was probably cut by Telegram's 4096 limit)."""
    depth, in_str, esc = 0, False, False
    for ch in _strip_fences(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return in_str or depth > 0


def load_loose_json(text: str):
    t = _strip_fences(text)
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        raise ValueError("چیزی که فرستادی JSON نیست.")
    start = min(starts)
    end = max(t.rfind("}"), t.rfind("]"))
    if end <= start:
        raise ValueError("JSON ناقصه (آخرش بسته نشده).")
    chunk = t[start:end + 1]
    last: Exception | None = None
    for cand in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk.translate(_SMART))):
        try:
            return json.loads(cand)
        except ValueError as exc:
            last = exc
    raise ValueError(f"JSON خراب است: {last}")


# ======================================================================
# story / characters parsing  (قرارداد با مستر پرامپت)
# ======================================================================
LANGS = {"fa": "fa", "farsi": "fa", "persian": "fa", "فارسی": "fa", "en": "en", "english": "en", "انگلیسی": "en",
         "ar": "ar", "arabic": "ar", "عربی": "ar", "tr": "tr", "turkish": "tr", "ترکی": "tr"}
DEFAULT_STYLE = ("high quality 3D animated film look, soft cinematic lighting, rich detailed environments, "
                 "expressive characters, consistent character design")


def _norm_aspect(v) -> str:
    raw = str(v or "").strip().lower()
    compact = raw.replace(" ", "").replace("/", ":").replace("×", ":")
    if compact in ASPECTS:
        return compact
    if any(k in raw for k in ("vertical", "portrait", "reels", "shorts", "tiktok", "story", "عمودی")):
        return "9:16"
    if any(k in raw for k in ("square", "مربع")):
        return "1:1"
    return "16:9"


def _norm_gender(value) -> str:
    v = str(value or "").strip().lower()
    return "female" if v in {"female", "f", "woman", "girl", "زن", "دختر", "خانم", "مونث"} else "male"


def parse_story(text: str) -> dict:
    if is_json_like(text):
        data = load_loose_json(text)
        if isinstance(data, dict) and isinstance(data.get("story"), dict):
            data = data["story"]
        if not isinstance(data, dict):
            raise ValueError("برای داستان یه آبجکت JSON لازمه (نه لیست).")
        synopsis = str(pick(data, "synopsis", "story", "text", "plot", "full_story", "داستان") or "").strip()
        title = clean_text(pick(data, "title", "name", "عنوان", default=""))
        logline = clean_text(pick(data, "logline", "summary", "خلاصه", default=""))
    else:
        body = (text or "").strip()
        first, _, rest = body.partition("\n")
        title = clean_text(first) if rest.strip() and len(first) <= 60 else ""
        synopsis = rest.strip() if title else body
        data, logline = {}, ""
    if len(synopsis) < 40:
        raise ValueError("متن داستان (synopsis) خیلی کوتاهه یا پیدا نشد.")
    narr = pick(data, "narrator", "راوی", default={})
    if isinstance(narr, str):
        narr = {"gender": narr}
    narr = narr if isinstance(narr, dict) else {}
    lang = LANGS.get(norm_name(pick(data, "language", "lang", "زبان", default="fa")), "fa")
    return {
        "title": title[:80] or "داستان من",
        "logline": logline[:300],
        "synopsis": synopsis[:12000],
        "language": lang,
        "aspect": _norm_aspect(pick(data, "aspect_ratio", "aspect", "format", "نسبت", default="16:9")),
        "style": str(pick(data, "style", "art_style", "visual_style", "سبک", default=DEFAULT_STYLE)).strip()[:700],
        "world": str(pick(data, "world", "setting", "فضا", default="")).strip()[:500],
        "narrator": {"gender": _norm_gender(narr.get("gender", "male")),
                     "persona": clean_text(narr.get("persona", narr.get("style", "")))[:200]},
    }


_KEY_MAP = {"name": "name", "نام": "name", "اسم": "name", "gender": "gender", "جنسیت": "gender",
            "role": "role", "نقش": "role", "prompt": "prompt", "پرامپت": "prompt", "توضیح": "prompt",
            "description": "prompt", "look": "look", "ظاهر": "look", "age": "age", "سن": "age"}


def parse_kv_blocks(text: str) -> list[dict]:
    """Fallback for plain text:  «نام: علی / جنسیت: مرد / پرامپت: ...» (blocks split by --- or a new «نام»)."""
    blocks: list[dict] = []
    cur: dict = {}
    last: str | None = None
    for line in text.splitlines():
        s = line.strip().lstrip("-•*> ").strip()
        if re.fullmatch(r"[-=*_]{3,}", line.strip()):
            if cur:
                blocks.append(cur)
            cur, last = {}, None
            continue
        if not s:
            continue
        m = re.match(r"^([^:：]{1,20})[:：]\s*(.*)$", s)
        key = _KEY_MAP.get(norm_name(m.group(1))) if m else None
        if key:
            if key == "name" and "name" in cur:
                blocks.append(cur)
                cur = {}
            cur[key] = m.group(2).strip()
            last = key
        elif last:
            cur[last] = f"{cur[last]} {s}".strip()
    if cur:
        blocks.append(cur)
    return blocks


def parse_characters(text: str) -> list[dict]:
    if is_json_like(text):
        data = load_loose_json(text)
        if isinstance(data, dict):
            inner = pick(data, "characters", "شخصیت‌ها", "cast")
            data = inner if inner is not None else [data]
        if not isinstance(data, list):
            raise ValueError("لیست شخصیت‌ها (characters) پیدا نشد.")
        raw_list = [x for x in data if isinstance(x, dict)]
    else:
        raw_list = parse_kv_blocks(text)
    if not raw_list:
        raise ValueError("هیچ شخصیتی پیدا نشد. فرمت درست رو با /format ببین.")
    out: list[dict] = []
    for i, raw in enumerate(raw_list[:MAX_CHARACTERS], 1):
        name = clean_text(pick(raw, "name", "نام", "اسم", "character", default=""))[:30]
        prompt = str(pick(raw, "prompt", "image_prompt", "پرامپت", "description", "توضیح", default="") or "").strip()
        if not name:
            raise ValueError(f"شخصیت شماره {i} اسم (name) نداره.")
        if len(prompt) < 15:
            raise ValueError(f"شخصیت «{name}» پرامپت (prompt) کافی نداره.")
        voice = pick(raw, "voice", default={})
        voice = voice if isinstance(voice, dict) else {}
        out.append({
            "name": name,
            "gender": _norm_gender(pick(raw, "gender", "جنسیت", default="male")),
            "role": clean_text(pick(raw, "role", "نقش", default=""))[:60],
            "age": clean_text(pick(raw, "age", "سن", default=""))[:20],
            "look": clean_text(pick(raw, "look", "appearance", "ظاهر", default=""))[:300],
            "prompt": prompt[:1500],
            "pitch": to_int(pick(voice, "pitch", default=pick(raw, "voice_pitch")), None),
            "rate": to_int(pick(voice, "rate", default=pick(raw, "voice_rate")), None),
        })
    return out


# ======================================================================
# script parsing + emotion → prosody
# ======================================================================
_EMOTION_WORDS = {
    "neutral": "neutral calm normal narration خنثی عادی آرام",
    "happy": "happy joy joyful cheerful glad شاد خوشحال شادی",
    "excited": "excited enthusiastic thrilled energetic هیجان هیجان‌زده پرانرژی",
    "sad": "sad sorrow grief melancholy غمگین غم ناراحت اندوه",
    "angry": "angry mad furious rage عصبانی خشم عصبی خشمگین",
    "fear": "fear afraid scared terrified fearful ترس ترسیده وحشت",
    "surprised": "surprised shocked amazed تعجب متعجب شوکه شگفت",
    "tender": "tender warm loving gentle soft مهربان محبت گرم لطیف",
    "serious": "serious stern grave جدی محکم",
    "mysterious": "mysterious eerie suspense suspenseful مرموز رازآلود پراسرار",
    "whisper": "whisper whispering نجوا پچپچ آهسته",
    "sarcastic": "sarcastic ironic mocking طعنه کنایه تمسخر",
    "tired": "tired exhausted weary خسته کلافه",
    "proud": "proud confident triumphant مغرور سربلند پیروز مطمئن",
    "worried": "worried anxious nervous concerned نگران مضطرب دلواپس",
    "determined": "determined resolute brave مصمم قاطع شجاع",
}
EMOTION_MAP = {norm_name(w): k for k, ws in _EMOTION_WORDS.items() for w in ws.split()}

# (rate %, pitch Hz, volume %) برای شدت ۳؛ بقیه‌ی شدت‌ها با ضریب مقیاس می‌شن
PROSODY = {
    "neutral": (0, 0, 0), "happy": (8, 6, 5), "excited": (18, 10, 10), "sad": (-18, -8, -12),
    "angry": (10, 3, 20), "fear": (14, 8, -4), "surprised": (6, 14, 10), "tender": (-12, 2, -12),
    "serious": (-8, -6, 4), "mysterious": (-14, -8, -15), "whisper": (-10, -4, -35),
    "sarcastic": (-4, 4, 0), "tired": (-22, -10, -18), "proud": (-4, -2, 10),
    "worried": (4, 4, -8), "determined": (-2, -4, 12),
}
INTENSITY_SCALE = {1: 0.4, 2: 0.7, 3: 1.0, 4: 1.3, 5: 1.6}
_PACE_WORDS = {"very_slow": "very_slow veryslow خیلی_کند خیلیکند", "slow": "slow کند آهسته",
               "normal": "normal medium عادی متوسط", "fast": "fast تند سریع", "very_fast": "very_fast veryfast خیلی_تند خیلیتند"}
PACE_MAP = {norm_name(w): k for k, ws in _PACE_WORDS.items() for w in ws.split()}
PACE_RATE = {"very_slow": -25, "slow": -12, "normal": 0, "fast": 12, "very_fast": 25}
EMOTION_FA = {"neutral": "عادی", "happy": "شاد", "excited": "هیجان‌زده", "sad": "غمگین", "angry": "عصبانی",
              "fear": "ترسیده", "surprised": "متعجب", "tender": "مهربان", "serious": "جدی", "mysterious": "مرموز",
              "whisper": "نجوا", "sarcastic": "کنایه‌دار", "tired": "خسته", "proud": "مغرور", "worried": "نگران",
              "determined": "مصمم"}

_CAMERA_WORDS = {"zoom_in": "zoom_in zoomin push_in pushin", "zoom_out": "zoom_out zoomout pull_back pullback",
                 "pan_left": "pan_left panleft", "pan_right": "pan_right panright", "static": "static still ثابت"}
CAMERA_MAP = {norm_name(w): k for k, ws in _CAMERA_WORDS.items() for w in ws.split()}


def _canon(table: dict, value, default: str) -> str:
    n = norm_name(str(value or "").replace("-", "_").replace(" ", "_"))
    return table.get(n) or table.get(norm_name(str(value or ""))) or default


def split_long_text(text: str, limit: int = MAX_LINE_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for sent in [p for p in re.split(r"(?<=[.!?؟…])\s+", text) if p]:
        if cur and len(cur) + 1 + len(sent) > limit:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    final: list[str] = []
    for chunk in out:
        if len(chunk) <= limit * 1.3:
            final.append(chunk)
            continue
        cur = ""
        for w in chunk.split():
            if cur and len(cur) + 1 + len(w) > limit:
                final.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            final.append(cur)
    return final


def parse_script(text: str, char_names: list[str]) -> tuple[list[dict], list[str]]:
    data = load_loose_json(text)
    if isinstance(data, dict):
        raw_scenes = pick(data, "scenes", "script", "صحنه‌ها")
    else:
        raw_scenes = data
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ValueError("آرایه‌ی صحنه‌ها (scenes) پیدا نشد.")
    warnings: list[str] = []
    unknown_emotions: set[str] = set()
    scenes: list[dict] = []
    for si, raw in enumerate(raw_scenes[:MAX_SCENES], 1):
        if not isinstance(raw, dict):
            raise ValueError(f"صحنه {si}: باید آبجکت باشه.")
        raw_lines = pick(raw, "lines", "dialogue", "dialogues", "narration", "script")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise ValueError(f"صحنه {si}: آرایه‌ی lines خالیه یا نیست.")
        lines: list[dict] = []
        for li, lr in enumerate(raw_lines, 1):
            if isinstance(lr, str):
                lr = {"speaker": "narrator", "text": lr}
            if not isinstance(lr, dict):
                raise ValueError(f"صحنه {si}، خط {li}: فرمت خط درست نیست.")
            who_raw = pick(lr, "speaker", "who", "character", "گوینده", default="")
            if is_narrator(who_raw):
                who = NARRATOR
            else:
                who = match_name(who_raw, char_names)
                if who is None:
                    raise ValueError(f"صحنه {si}، خط {li}: گوینده‌ی «{who_raw}» بین شخصیت‌ها نیست. "
                                     f"اسم‌های معتبر: {NARRATOR}، " + "، ".join(char_names))
            body = clean_text(pick(lr, "text", "line", "dialogue", "متن", default=""))
            say = clean_text(pick(lr, "say", "tts", "tts_text", "speak", default=""))
            if not body and not say:
                raise ValueError(f"صحنه {si}، خط {li}: متن (text) خالیه.")
            body = body or say
            emo_raw = pick(lr, "emotion", "feeling", "احساس", default="neutral")
            emo = _canon(EMOTION_MAP, emo_raw, "")
            if not emo:
                unknown_emotions.add(str(emo_raw))
                emo = "neutral"
            inten = min(5, max(1, to_int(pick(lr, "intensity", "شدت"), 3) or 3))
            pace = _canon(PACE_MAP, pick(lr, "pace", "speed", "سرعت", default="normal"), "normal")
            pb = to_float(pick(lr, "pause_before"), None)
            pa = to_float(pick(lr, "pause_after"), None)
            base = {"speaker": who, "emotion": emo, "intensity": inten, "pace": pace,
                    "pause_before": None if pb is None else min(3.0, max(0.0, pb))}
            parts = [body] if say else split_long_text(body)
            for pi, part in enumerate(parts):
                last = pi == len(parts) - 1
                lines.append({**base, "text": part, "say": say if say else "",
                              "pause_before": base["pause_before"] if pi == 0 else 0.0,
                              "pause_after": (None if pa is None else min(3.0, max(0.0, pa))) if last else 0.15,
                              "start": 0.0, "end": 0.0})
        speakers = []
        for ln in lines:
            if ln["speaker"] != NARRATOR and ln["speaker"] not in speakers:
                speakers.append(ln["speaker"])
        present_raw = pick(raw, "characters", "chars", "present", default=None)
        present: list[str] = []
        if isinstance(present_raw, list):
            for nm in present_raw:
                m = match_name(nm, char_names)
                if m and m not in present:
                    present.append(m)
                elif not m:
                    warnings.append(f"صحنه {si}: شخصیت «{nm}» تو لیست شخصیت‌ها نیست و نادیده گرفته شد.")
        for sp in speakers:
            if sp not in present:
                present.append(sp)
        visual = str(pick(raw, "visual", "image_prompt", "image", "prompt", "scene_prompt", "visual_prompt", default="") or "").strip()
        title = clean_text(pick(raw, "title", "name", "عنوان", default=""))[:60] or f"صحنه {si}"
        if not visual:
            warnings.append(f"صحنه {si}: توضیح تصویر (visual) نداشت؛ از عنوان/متن استفاده می‌شه.")
            visual = title if title != f"صحنه {si}" else lines[0]["text"]
        emotions = [ln["emotion"] for ln in lines if ln["emotion"] != "neutral"]
        mood = _canon(EMOTION_MAP, pick(raw, "mood", default=""), "") or (max(set(emotions), key=emotions.count) if emotions else "neutral")
        scenes.append({
            "id": si, "title": title, "visual": visual[:1500], "characters": present,
            "camera": _canon(CAMERA_MAP, pick(raw, "camera", "motion", default="auto"), "auto"),
            "mood": mood, "keep_background": bool(pick(raw, "keep_background", "continuity", default=False)),
            "lines": lines, "audio": "", "dur": 0.0, "tweak": {"rate": 0, "pitch": 0},
            "img": "", "img_ok": False,
        })
    if len(raw_scenes) > MAX_SCENES:
        warnings.append(f"فقط {MAX_SCENES} صحنه‌ی اول خونده شد.")
    if unknown_emotions:
        warnings.append("احساس‌های ناشناخته (عادی در نظر گرفته شدن): " + "، ".join(sorted(unknown_emotions)))
    return scenes, warnings


def _sgn(v: float, unit: str) -> str:
    return f"{int(round(v)):+d}{unit}"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def line_prosody(line: dict, tweak: dict, base_pitch: int = 0, base_rate: int = 0) -> tuple[str, str, str]:
    r, p, v = PROSODY.get(line["emotion"], PROSODY["neutral"])
    f = INTENSITY_SCALE.get(line["intensity"], 1.0)
    rate = r * f + PACE_RATE.get(line["pace"], 0) + base_rate + tweak.get("rate", 0)
    pitch = p * f + base_pitch + tweak.get("pitch", 0)
    vol = v * f
    return _sgn(_clamp(rate, -45, 60), "%"), _sgn(_clamp(pitch, -40, 40), "Hz"), _sgn(_clamp(vol, -50, 30), "%")


def voice_plan(story: dict, characters: list[dict]) -> dict[str, tuple[str, int, int]]:
    """name → (edge-tts voice, base pitch Hz, base rate %). صدای هم‌جنس‌ها با pitch فرق داده می‌شه."""
    male, female = VOICES.get(story.get("language", "fa"), VOICES["fa"])
    male, female = TTS_MALE_OVERRIDE or male, TTS_FEMALE_OVERRIDE or female
    plan = {NARRATOR: (female if story.get("narrator", {}).get("gender") == "female" else male, 0, -4)}
    spread = [0, 10, -8, 16, -14, 5, -4, 12]
    counts = {"male": 0, "female": 0}
    for c in characters:
        g = c.get("gender", "male")
        k = counts[g]
        counts[g] += 1
        pitch = c["pitch"] if c.get("pitch") is not None else spread[k % len(spread)]
        plan[c["name"]] = (female if g == "female" else male, pitch, c.get("rate") or 0)
    return plan


# ======================================================================
# session / project storage
# ======================================================================
PHASES = ("story", "characters", "script", "audio", "images", "edit")
STAGE_NUM = {"story": 1, "characters": 2, "script": 3, "audio": 3, "images": 4, "edit": 5}
STAGE_NAME = {1: "داستان", 2: "شخصیت‌ها", 3: "متن و صدا", 4: "تصاویر", 5: "ادیت و ویدیو"}
DEFAULT_SETTINGS = {"subs": True, "trans": "fade", "motion": True, "music": "", "img_mode": ""}


@dataclass
class Session:
    uid: int
    phase: str = "story"
    story: dict = field(default_factory=dict)
    characters: list = field(default_factory=list)
    scenes: list = field(default_factory=list)
    settings: dict = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    awaiting: str = ""        # "char_fb:c1" | "img_fb:3" | "music"
    buf: str = ""             # بخش‌های ناقصِ پیام‌های بلند (محدودیت ۴۰۹۶ تلگرام)
    counter: int = 0


def pdir(uid: int) -> Path:
    d = PROJECTS_DIR / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def apath(uid: int, rel: str) -> Path:
    return pdir(uid) / rel


class SessionStore:
    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self._cache: dict[int, Session] = {}

    def _path(self, uid: int) -> Path:
        return self.folder / f"{uid}.json"

    def get(self, uid: int) -> Session:
        cached = self._cache.get(uid)
        if cached is not None:
            return cached
        session = Session(uid=uid)
        path = self._path(uid)
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                allowed = {f.name for f in fields(Session)}
                session = Session(**{k: v for k, v in data.items() if k in allowed})
                session.uid = uid
                if session.phase not in PHASES:
                    session.phase = "story"
                for k, v in DEFAULT_SETTINGS.items():
                    session.settings.setdefault(k, v)
            except (OSError, ValueError, TypeError):
                session = Session(uid=uid)
        self._cache[uid] = session
        return session

    def save(self, session: Session) -> None:
        self._cache[session.uid] = session
        path = self._path(session.uid)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(session), ensure_ascii=False), "utf-8")
        os.replace(tmp, path)

    def reset(self, uid: int) -> Session:
        shutil.rmtree(PROJECTS_DIR / str(uid), ignore_errors=True)
        session = Session(uid=uid)
        self.save(session)
        return session


class Access:
    def __init__(self, path: Path, admin_id: int):
        self.path = path
        self.admin_id = admin_id
        self.users: set[int] = set()
        try:
            data = json.loads(self.path.read_text("utf-8"))
            self.users = {int(u) for u in data.get("users", [])}
        except (OSError, ValueError, TypeError, AttributeError):
            self.users = set()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"users": sorted(self.users)}), "utf-8")
        os.replace(tmp, self.path)

    def is_admin(self, uid: int) -> bool:
        return uid == self.admin_id

    def is_allowed(self, uid: int) -> bool:
        return self.is_admin(uid) or uid in self.users

    def add(self, uid: int) -> None:
        self.users.add(uid)
        self._save()

    def remove(self, uid: int) -> bool:
        existed = uid in self.users
        self.users.discard(uid)
        self._save()
        return existed


# ======================================================================
# image generation  (OpenRouter Image API + عکس مرجع)
# ======================================================================
class ImageError(Exception):
    """Error with a user-friendly Persian message."""


_img_sem = asyncio.Semaphore(IMAGE_CONCURRENCY)


def _img_friendly(status: int, body: str) -> str:
    low = body.lower()
    if status == 401:
        return "کلید OpenRouter نامعتبره. OPENROUTER_API_KEY رو توی Variables ریلوی چک کن."
    if status == 402:
        return "اعتبار OpenRouter برای ساخت تصویر کافی نیست. حساب رو شارژ کن (مدل‌های تصویر رایگان نیستن)."
    if status == 403:
        return "دسترسی به مدل تصویر رد شد (کلید، منطقه یا فیلتر محتوا)."
    if status == 404:
        return "مدل تصویر پیدا نشد. مقدار IMAGE_MODEL رو چک کن."
    if status == 429:
        return "سهمیه‌ی OpenRouter موقتاً پر شده. چند دقیقه صبر کن."
    if status == 400 and any(k in low for k in ("safety", "policy", "blocked", "moderation")):
        return "مدل این تصویر رو به‌خاطر فیلتر ایمنی نساخت. توضیح صحنه رو کمی ملایم‌تر کن."
    return f"ساخت تصویر ناموفق بود (HTTP {status}): {body[:140]}"


def _data_url(path: Path, max_side: int = 1024) -> str:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


async def _post(url: str, payload: dict) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json",
               "X-Title": "Story Studio Bot"}
    timeout = aiohttp.ClientTimeout(total=240)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.post(url, json=payload, headers=headers) as resp:
            return resp.status, await resp.text()


async def _download(url: str) -> bytes:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
        async with http.get(url) as resp:
            if resp.status != 200:
                raise ImageError("دانلود تصویر ساخته‌شده ناموفق بود.")
            return await resp.read()


def _decode_b64(s: str) -> bytes:
    if s.startswith("data:"):
        s = s.split(",", 1)[1]
    return base64.b64decode(s)


async def _via_images_api(model: str, prompt: str, aspect: str, refs: list[str]) -> tuple[int, bytes | str]:
    base: dict = {"model": model, "prompt": prompt}
    if refs:
        base["input_references"] = [{"type": "image_url", "image_url": {"url": u}} for u in refs]
    opt = {"aspect_ratio": aspect}
    if IMAGE_RESOLUTION:
        opt["resolution"] = IMAGE_RESOLUTION
    for payload in ({**base, **opt}, base):          # اگه پارامترهای اختیاری رد شدن، بدونشون تلاش کن
        status, body = await _post("https://openrouter.ai/api/v1/images", payload)
        if status == 400 and payload is not base:
            continue
        break
    if status != 200:
        return status, body
    try:
        item = json.loads(body)["data"][0]
        if item.get("b64_json"):
            return 200, _decode_b64(item["b64_json"])
        if item.get("url"):
            return 200, await _download(item["url"])
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    return 502, "empty image response"


async def _via_chat_api(model: str, prompt: str, aspect: str, refs: list[str]) -> tuple[int, bytes | str]:
    content: list[dict] = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in refs]
    payload = {"model": model, "messages": [{"role": "user", "content": content}],
               "modalities": ["image", "text"], "image_config": {"aspect_ratio": aspect}}
    status, body = await _post("https://openrouter.ai/api/v1/chat/completions", payload)
    if status == 400:
        payload.pop("image_config")
        status, body = await _post("https://openrouter.ai/api/v1/chat/completions", payload)
    if status != 200:
        return status, body
    try:
        msg = json.loads(body)["choices"][0]["message"]
        url = msg["images"][0]["image_url"]["url"]
        return 200, _decode_b64(url) if url.startswith("data:") else await _download(url)
    except (ValueError, KeyError, IndexError, TypeError):
        return 502, "empty image response"


CF_DIMS = {"16:9": (1024, 576), "9:16": (576, 1024), "1:1": (1024, 1024)}


def _cf_friendly(status: int, body: str) -> str:
    low = body.lower()
    if "daily free allocation" in low or "4006" in low or status == 429:
        return ("سهمیه‌ی رایگان امروز Cloudflare تموم شد. ساعت ۰۳:۳۰ بامداد به وقت تهران (۰۰:۰۰ UTC) ریست می‌شه؛ "
                "بعدش دوباره امتحان کن.")
    if status in (401, 403):
        return "توکن یا Account ID کلادفلر نامعتبره (یا دسترسی Workers AI رو نداره). CF_API_TOKEN و CF_ACCOUNT_ID رو چک کن."
    if status == 404:
        return "مدل کلادفلر پیدا نشد. مقدار CF_IMAGE_MODEL رو چک کن."
    return f"ساخت تصویر با Cloudflare ناموفق بود (HTTP {status}): {body[:140]}"


def _cf_ref_png(path: Path) -> bytes:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((512, 512))                       # کلادفلر عکس مرجع بزرگ‌تر از ۵۱۲ رو قبول نمی‌کنه
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def _cf_post(prompt: str, w: int, h: int, refs: list[bytes]) -> tuple[int, bytes | str]:
    form = aiohttp.FormData()
    form.add_field("prompt", prompt[:3000])
    form.add_field("width", str(w))
    form.add_field("height", str(h))
    for i, data in enumerate(refs[:4]):
        form.add_field(f"input_image_{i}", data, filename=f"ref{i}.png", content_type="image/png")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_IMAGE_MODEL}"
    timeout = aiohttp.ClientTimeout(total=240)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.post(url, data=form, headers={"Authorization": f"Bearer {CF_API_TOKEN}"}) as resp:
            body = await resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            status = resp.status
    if status == 200 and ctype.startswith("image/"):
        return 200, body
    text = body.decode("utf-8", errors="ignore")
    try:
        data = json.loads(text)
    except ValueError:
        return (status if status != 200 else 502), text
    if status == 200 and data.get("success", True):
        res = data.get("result")
        b64 = res.get("image") if isinstance(res, dict) else res if isinstance(res, str) else None
        if b64:
            try:
                return 200, _decode_b64(b64)
            except Exception:  # noqa: BLE001
                pass
    return (status if status != 200 else 502), text


async def _gen_cloudflare(prompt: str, aspect: str, refs: list[Path], size: tuple[int, int] | None) -> Image.Image:
    w, h = CF_DIMS.get(aspect, CF_DIMS["16:9"])
    ref_bytes = [await asyncio.to_thread(_cf_ref_png, p) for p in refs if p.exists()][:4]
    last: ImageError | None = None
    delay = 4.0
    async with _img_sem:
        for attempt in range(1, 4):
            try:
                status, out = await _cf_post(prompt, w, h, ref_bytes)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                status, out = 599, f"network: {exc!r}"
            if status == 200 and isinstance(out, bytes):
                try:
                    def _open() -> Image.Image:
                        im = Image.open(io.BytesIO(out))
                        im.load()
                        im = im.convert("RGB")
                        return ImageOps.fit(im, size, Image.LANCZOS) if size else im
                    return await asyncio.to_thread(_open)
                except Exception:  # noqa: BLE001
                    status, out = 502, "undecodable image"
            last = ImageError(_cf_friendly(status, str(out)))
            log.warning("cloudflare image failed status=%s attempt=%s: %s", status, attempt, str(out)[:200])
            if status in (401, 403, 404, 429) or "daily free allocation" in str(out).lower():
                raise last
            if attempt < 3:
                await asyncio.sleep(delay)
                delay *= 2.5
    raise last or ImageError("ساخت تصویر ناموفق بود.")


async def gen_image(prompt: str, aspect: str, refs: list[Path] | None = None, size: tuple[int, int] | None = None) -> Image.Image:
    """Generate one image (optionally guided by reference images). Raises ImageError."""
    if IMAGE_PROVIDER == "cloudflare":
        return await _gen_cloudflare(prompt, aspect, refs or [], size)
    ref_urls = [await asyncio.to_thread(_data_url, p) for p in (refs or []) if p.exists()][:6]
    last: ImageError | None = None
    async with _img_sem:
        for model in IMAGE_MODELS:
            delay = 4.0
            for attempt in range(1, 4):
                try:
                    status, out = await _via_images_api(model, prompt, aspect, ref_urls)
                    if status in (404, 405):                       # Image API این مدل رو نداره → chat API
                        status, out = await _via_chat_api(model, prompt, aspect, ref_urls)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    status, out = 599, f"network: {exc!r}"
                if status == 200 and isinstance(out, bytes):
                    try:
                        def _open() -> Image.Image:
                            im = Image.open(io.BytesIO(out))
                            im.load()
                            im = im.convert("RGB")
                            return ImageOps.fit(im, size, Image.LANCZOS) if size else im
                        return await asyncio.to_thread(_open)
                    except Exception:  # noqa: BLE001
                        status, out = 502, "undecodable image"
                last = ImageError(_img_friendly(status, str(out)))
                log.warning("image gen failed model=%s status=%s attempt=%s: %s", model, status, attempt, str(out)[:200])
                if status in (401, 402, 403):
                    raise last
                if status in (429, 500, 502, 503, 504, 599) and attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2.5
                    continue
                break
    raise last or ImageError("ساخت تصویر ناموفق بود.")


# ---------------------------------------------------------------------- prompts
def char_sheet_prompt(story: dict, ch: dict, feedback: str | None = None) -> str:
    parts = [
        f"{story.get('style') or DEFAULT_STYLE}.",
        f"Character reference sheet of {ch['name']}: {ch['prompt']}",
        "Full body, front-facing neutral standing pose, entire figure visible head to toe, plain soft light-gray "
        "studio background, even lighting, clear readable face and outfit details, no text, no watermark.",
    ]
    if feedback:
        parts.append(f"Apply this change while keeping the same character identity: {feedback}")
    return " ".join(parts)


def scene_prompt(story: dict, scene: dict, chars: dict[str, dict], ref_names: list[str], feedback: str | None = None,
                 has_prev: bool = False, has_old: bool = False) -> str:
    lines = [f"{story.get('style') or DEFAULT_STYLE}.", f"Scene: {scene['visual']}"]
    if story.get("world"):
        lines.append(f"World/setting notes: {story['world']}")
    off = 0 if IMAGE_PROVIDER == "cloudflare" else 1       # کلادفلر عکس‌ها رو از صفر شماره‌گذاری می‌کنه
    ref_idx = off - 1
    if has_old:
        ref_idx += 1
        lines.append(f"Image {ref_idx} is the current version of this scene: keep its composition and only apply the requested change.")
    for nm in ref_names:
        ref_idx += 1
        c = chars.get(nm, {})
        desc = c.get("look") or c["prompt"][:160] if c else ""
        lines.append(f"Reference image {ref_idx} shows the character {nm} ({desc}). Keep this character's face, hair, outfit, "
                     "body proportions and colors EXACTLY the same as in that reference.")
    if has_prev:
        lines.append(f"Reference image {ref_idx + 1} is the previous scene: keep the same location, lighting and color palette.")
    absent = [n for n in scene.get("characters", []) if n not in ref_names]
    if absent:
        lines.append("Also in the scene: " + ", ".join(absent) + ".")
    if scene.get("mood") and scene["mood"] != "neutral":
        lines.append(f"Emotional mood of the scene: {scene['mood']}; show it in faces, posture and lighting.")
    if feedback:
        lines.append(f"Requested change: {feedback}")
    lines.append("Cinematic composition, characters clearly visible and consistent, no text, no captions, no watermark, no logo.")
    return "\n".join(lines)


# ======================================================================
# TTS  (edge-tts؛ احساس از روی rate/pitch/volume ساخته می‌شه)
# ======================================================================
class TTSError(Exception):
    pass


def _speakable(text: str) -> str:
    text = clean_text(text)
    for ch in ("«", "»", "“", "”", '"', "…"):
        text = text.replace(ch, " ")
    return " ".join(text.split())


async def synth(text: str, voice: str, out_path: Path, rate: str, pitch: str, volume: str) -> None:
    text = _speakable(text)
    if len(text) < 1:
        raise TTSError("متن یکی از خط‌ها برای تبدیل به صدا خالیه.")
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
            await asyncio.wait_for(comm.save(str(out_path)), timeout=90)
            if out_path.exists() and out_path.stat().st_size > 800:
                return
            last = TTSError("empty audio")
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("edge-tts attempt %s failed: %r", attempt, exc)
        await asyncio.sleep(1.5 * attempt)
    raise TTSError("ساخت صدا با edge-tts ناموفق بود. اینترنت/VPN سرور رو چک کن و دوباره تلاش کن."
                   f" ({type(last).__name__})")


# ======================================================================
# ffmpeg helpers + scene audio assembly
# ======================================================================
class VideoError(Exception):
    pass


class RenderCancelled(Exception):
    pass


async def run_cmd(*args: str, timeout: int = 1200) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise VideoError("ffmpeg بیش از حد طول کشید و متوقف شد.")
    except asyncio.CancelledError:
        proc.kill()
        raise
    return proc.returncode or 0, (err or b"").decode(errors="ignore") + (out or b"").decode(errors="ignore")


PCM_RATE = 24000


async def to_pcm_wav(src: Path, dst: Path) -> None:
    code, msg = await run_cmd("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                              "-ac", "1", "-ar", str(PCM_RATE), "-c:a", "pcm_s16le", str(dst), timeout=120)
    if code != 0 or not dst.exists():
        raise VideoError("تبدیل صدا به wav ناموفق بود: " + msg[-200:])


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != PCM_RATE or w.getsampwidth() != 2:
            raise VideoError("فرمت wav غیرمنتظره است.")
        return w.readframes(w.getnframes())


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(round(max(0.0, seconds) * PCM_RATE))


def _write_pcm(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(PCM_RATE)
        w.writeframes(pcm)


def default_pause(line: dict, nxt: dict | None) -> float:
    """Pause after a line when the script doesn't specify one."""
    if line["pause_after"] is not None:
        return line["pause_after"]
    base = {"sad": 0.7, "tired": 0.7, "mysterious": 0.7, "tender": 0.6, "serious": 0.5}.get(line["emotion"], 0.35)
    if nxt is not None and nxt["speaker"] != line["speaker"]:
        base += 0.15
    return base


async def build_scene_audio(uid: int, story: dict, characters: list[dict], scene: dict,
                            is_cancelled: Callable[[], bool] = lambda: False) -> None:
    """Synthesize every line of a scene, glue them with pauses → scene wav + ogg (for Telegram)."""
    plan = voice_plan(story, characters)
    sdir = apath(uid, f"scenes/{scene['id']:02d}")
    sdir.mkdir(parents=True, exist_ok=True)
    lines = scene["lines"]
    sem = asyncio.Semaphore(TTS_CONCURRENCY)

    async def one(ln: dict) -> Path:
        voice, bp, br = plan.get(ln["speaker"], plan[NARRATOR])
        rate, pitch, vol = line_prosody(ln, scene.get("tweak", {}), bp, br)
        text = ln["say"] or ln["text"]
        key = hashlib.md5(f"{text}|{voice}|{rate}|{pitch}|{vol}".encode("utf-8")).hexdigest()[:12]
        wav = sdir / f"l_{key}.wav"
        if not wav.exists():
            mp3 = sdir / f"l_{key}.mp3"
            async with sem:
                await synth(text, voice, mp3, rate, pitch, vol)
            await to_pcm_wav(mp3, wav)
            mp3.unlink(missing_ok=True)
        return wav

    wavs = await asyncio.gather(*(one(ln) for ln in lines))
    if is_cancelled():
        raise RenderCancelled()
    pcm = bytearray()
    t = 0.0
    for i, (ln, wav) in enumerate(zip(lines, wavs)):
        pcm += _silence(ln["pause_before"] or 0.0)
        t += ln["pause_before"] or 0.0
        data = await asyncio.to_thread(_read_pcm, wav)
        ln["start"] = round(t, 3)
        pcm += data
        t += len(data) / 2 / PCM_RATE
        ln["end"] = round(t, 3)
        if i < len(lines) - 1:
            pause = default_pause(ln, lines[i + 1])
            pcm += _silence(pause)
            t += pause
    master = sdir / "audio.wav"
    await asyncio.to_thread(_write_pcm, master, bytes(pcm))
    ogg = sdir / "audio.ogg"
    code, _ = await run_cmd("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(master),
                            "-c:a", "libopus", "-b:a", "48k", "-application", "voip", str(ogg), timeout=120)
    if code != 0:
        ogg.unlink(missing_ok=True)
    scene["audio"] = f"scenes/{scene['id']:02d}/audio.wav"
    scene["dur"] = round(t, 3)
    for old in sdir.glob("l_*.wav"):               # فایل‌های خط‌های قدیمی (متن/احساس عوض‌شده) پاک بشن
        if old not in wavs:
            old.unlink(missing_ok=True)


# ======================================================================
# fonts / subtitles
# ======================================================================
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS
PALETTE = ["#e63946", "#1d6fd1", "#2a9d4a", "#f08c00", "#8e44ad", "#00897b",
           "#d81b60", "#5d4037", "#546e7a", "#b08900", "#3949ab", "#00838f"]
_FONT_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf", "C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


@lru_cache(maxsize=1)
def font_path() -> str | None:
    env = os.getenv("FONT_PATH", "").strip()
    if env and Path(env).exists():
        return env
    for pattern in ("*Regular*.ttf", "*wght*.ttf", "*Vazir*.ttf", "*.ttf", "*.otf"):
        found = sorted(FONT_DIR.glob(pattern))
        if found:
            return str(found[0])
    for candidate in _FONT_FALLBACKS:
        if Path(candidate).exists():
            return candidate
    return None


_BASIC_LAYOUT = getattr(getattr(ImageFont, "Layout", None), "BASIC", getattr(ImageFont, "LAYOUT_BASIC", None))


@lru_cache(maxsize=32)
def get_font(size: int):
    path = font_path()
    if path:
        if _BASIC_LAYOUT is not None:   # متن فارسی دستی شکل‌دهی می‌شه؛ چیدمان خودکار Pillow باید خاموش باشه
            return ImageFont.truetype(path, size, layout_engine=_BASIC_LAYOUT)
        return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def shape(text: str) -> str:
    """Prepare Persian/Arabic text for Pillow (joining + right-to-left)."""
    if "vazir" not in (font_path() or "").lower():
        text = text.replace("\u200c", " ")
    return get_display(arabic_reshaper.reshape(text))


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _wrap(text: str, draw: ImageDraw.ImageDraw, font, max_w: float) -> list[str]:
    lines: list[str] = []
    cur: list[str] = []
    for word in text.split():
        trial = cur + [word]
        if not cur or draw.textlength(shape(" ".join(trial)), font=font) <= max_w:
            cur = trial
        else:
            lines.append(" ".join(cur))
            cur = [word]
    if cur:
        lines.append(" ".join(cur))
    return lines


def make_subtitle_png(text: str, speaker: str | None, color: str | None, size: tuple[int, int], out: Path) -> None:
    """Transparent full-frame PNG with a rounded subtitle box (+ speaker tag for dialogue)."""
    W, H = size
    k = max(W, H) / 1280
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(img)
    margin = int(W * 0.06)
    max_w = W - 2 * margin - int(40 * k)
    lines, font, lh = [], get_font(int(38 * k)), int(51 * k)
    for px in (38, 34, 30, 27, 24):
        font = get_font(max(12, int(px * k)))
        lh = int(px * k * 1.35)
        lines = _wrap(text, od, font, max_w)
        if len(lines) <= 3:
            break
    lines = lines[:4]
    pad = int(14 * k)
    box_h = len(lines) * lh + 2 * pad
    y1 = H - int(H * (0.06 if W >= H else 0.12))
    y0 = y1 - box_h
    od.rounded_rectangle((margin, y0, W - margin, y1), radius=int(18 * k), fill=(0, 0, 0, 178))
    y = y0 + pad
    for line in lines:
        s = shape(line)
        tw = od.textlength(s, font=font)
        od.text(((W - tw) / 2, y), s, font=font, fill=(255, 255, 255, 255))
        y += lh
    if speaker:
        tag_font = get_font(max(12, int(24 * k)))
        s = shape(clean_text(speaker))
        tw = od.textlength(s, font=tag_font)
        bx1 = W - margin
        box = (bx1 - tw - int(28 * k), y0 - int(40 * k), bx1, y0 - int(4 * k))
        od.rounded_rectangle(box, radius=int(14 * k), fill=_hex(color or "#444444") + (255,))
        od.text((box[0] + int(14 * k), box[1] + int(2 * k)), s, font=tag_font, fill=(255, 255, 255, 255))
    img.save(out)


# ======================================================================
# video: segments → crossfade join → narration track → mux
# ======================================================================
@dataclass
class RenderResult:
    path: Path
    duration: float
    size_mb: float
    shrunk: bool = False


async def make_segment(img: Path, overlays: list[tuple[Path, float, float]], length: float, motion: str,
                       out: Path, size: tuple[int, int], fade_in: bool, fade_out: bool) -> None:
    w, h = size
    frames = max(2, int(round(length * FPS)))
    zw, zh = int(w * 1.6) // 2 * 2, int(h * 1.6) // 2 * 2
    Z = 0.12
    cx, cy = "x='iw/2-(iw/zoom/2)'", "y='ih/2-(ih/zoom/2)'"
    zp = {
        "zoom_in": f"z='1+{Z}*on/{frames}':{cx}:{cy}",
        "zoom_out": f"z='{1 + Z}-{Z}*on/{frames}':{cx}:{cy}",
        "pan_right": f"z='1.1':x='(iw-iw/zoom)*on/{frames}':{cy}",
        "pan_left": f"z='1.1':x='(iw-iw/zoom)*(1-on/{frames})':{cy}",
    }.get(motion, "z='1':x='0':y='0'")
    parts = [f"[0:v]scale={zw}:{zh}:flags=lanczos,zoompan={zp}:d={frames}:s={w}x{h}:fps={FPS},format=yuv420p[v0]"]
    cur = "v0"
    for k, (_, a, b) in enumerate(overlays, 1):
        parts.append(f"[{cur}][{k}:v]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})':format=auto[v{k}]")
        cur = f"v{k}"
    fx = []
    if fade_in:
        fx.append("fade=t=in:st=0:d=0.5")
    if fade_out:
        fx.append(f"fade=t=out:st={max(length - 0.7, 0):.2f}:d=0.7")
    parts.append(f"[{cur}]{','.join(fx) if fx else 'null'}[vout]")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(img)]
    for png, _, _ in overlays:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{length:.3f}", "-i", str(png)]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS), "-an", str(out)]
    code, msg = await run_cmd(*cmd)
    if code != 0 or not out.exists():
        log.error("segment failed: %s", msg[-700:])
        raise VideoError("ساخت یکی از صحنه‌های ویدیو با ffmpeg ناموفق بود.")


async def join_segments(segs: list[Path], lengths: list[float], trans: str, out: Path) -> None:
    if len(segs) == 1 or trans == "cut":
        listing = out.parent / "list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), "utf-8")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", str(listing), "-c", "copy", str(out)]
    else:
        name = "fade" if trans == "fade" else "fadeblack"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for p in segs:
            cmd += ["-i", str(p)]
        chain, label = [], "[0:v]"
        for k in range(1, len(segs)):
            offset = sum(lengths[:k]) - k * TRANSITION_SEC
            chain.append(f"{label}[{k}:v]xfade=transition={name}:duration={TRANSITION_SEC:.3f}:offset={offset:.3f}[x{k}]")
            label = f"[x{k}]"
        cmd += ["-filter_complex", ";".join(chain), "-map", label, "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS), str(out)]
    code, msg = await run_cmd(*cmd)
    if code != 0 or not out.exists():
        log.error("join failed: %s", msg[-700:])
        raise VideoError("چسباندن صحنه‌ها به هم ناموفق بود.")


def build_narration(scenes: list[dict], uid: int, lengths: list[float], t_eff: float, lead: float, out: Path) -> float:
    """One wav for the whole film. Scene i starts exactly lead + Σ(L_k − T) seconds in."""
    pcm = bytearray(_silence(lead))
    for i, sc in enumerate(scenes):
        data = _read_pcm(apath(uid, sc["audio"]))
        pcm += data
        dur = len(data) / 2 / PCM_RATE
        if i < len(scenes) - 1:
            pcm += _silence(lengths[i] - t_eff - dur)
        else:
            pcm += _silence(lengths[i] - lead - dur)
    _write_pcm(out, bytes(pcm))
    return len(pcm) / 2 / PCM_RATE


async def mux(video: Path, narr: Path, music: Path | None, out: Path, total: float) -> None:
    fade = f"afade=t=out:st={max(total - 0.8, 0):.2f}:d=0.8"
    for normalize in (":normalize=0", ""):          # normalize=0 روی ffmpeg قدیمی وجود نداره
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(narr)]
        if music:
            cmd += ["-stream_loop", "-1", "-i", str(music)]
            fc = ("[1:a]aresample=44100,asplit=2[n1][n2];"
                  "[2:a]aresample=44100,volume=0.30,afade=t=in:st=0:d=1.5[m];"
                  "[m][n1]sidechaincompress=threshold=0.02:ratio=9:attack=20:release=400[md];"
                  f"[n2][md]amix=inputs=2:duration=first{normalize},{fade},loudnorm=I=-16:TP=-1.5:LRA=11[a]")
        else:
            fc = f"[1:a]aresample=44100,{fade},loudnorm=I=-16:TP=-1.5:LRA=11[a]"
        cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                "-ac", "2", "-ar", "44100", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
        code, msg = await run_cmd(*cmd)
        if code == 0 and out.exists():
            return
        log.error("mux failed: %s", msg[-600:])
        if not music:
            break
    raise VideoError("ترکیب صدا و تصویر ناموفق بود.")


async def shrink(src: Path, size: tuple[int, int]) -> Path:
    """Re-encode smaller if the file exceeds Telegram's 50 MB bot upload limit."""
    out = src.with_name("story_small.mp4")
    w, h = size
    scale = "scale=854:480" if w >= h else "scale=480:854"
    code, _ = await run_cmd("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vf", scale,
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k",
                            "-movflags", "+faststart", str(out))
    return out if code == 0 and out.exists() else src


def pick_motion(scene: dict, i: int) -> str:
    if scene["camera"] != "auto":
        return scene["camera"]
    mood = scene.get("mood", "neutral")
    if mood in ("sad", "tired", "tender", "mysterious", "fear", "serious", "worried", "angry", "surprised"):
        return "zoom_in"
    if mood in ("excited", "happy", "proud", "determined"):
        return ("pan_right", "pan_left")[i % 2]
    return ("zoom_in", "pan_right", "zoom_out", "pan_left")[i % 4]


ProgressCb = Callable[[int, int, str], Awaitable[None]]


async def render_video(uid: int, s: Session, progress: ProgressCb, is_cancelled: Callable[[], bool]) -> RenderResult:
    size = ASPECTS.get(s.story.get("aspect", "16:9"), ASPECTS["16:9"])
    st = s.settings
    scenes = s.scenes
    work = apath(uid, "work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    out_dir = apath(uid, "out")
    out_dir.mkdir(exist_ok=True)
    t_eff = 0.0 if st.get("trans") == "cut" else TRANSITION_SEC
    lead = t_eff / 2 + 0.12
    colors = {c["name"]: PALETTE[i % len(PALETTE)] for i, c in enumerate(s.characters)}
    n = len(scenes)
    lengths: list[float] = []
    segs: list[Path] = []
    for i, sc in enumerate(scenes):
        if is_cancelled():
            raise RenderCancelled()
        await progress(i + 1, n, "ساخت صحنه")
        img, aud = apath(uid, sc["img"]), apath(uid, sc["audio"]) if sc["audio"] else None
        if not sc["img"] or not img.exists() or aud is None or not aud.exists():
            raise VideoError(f"صحنه {sc['id']}: تصویر یا صدا آماده نیست.")
        length = round((sc["dur"] + t_eff + SCENE_GAP) * FPS) / FPS
        overlays: list[tuple[Path, float, float]] = []
        if st.get("subs", True):
            for j, ln in enumerate(sc["lines"]):
                png = work / f"sub_{i:02d}_{j:02d}.png"
                spk = None if ln["speaker"] == NARRATOR else ln["speaker"]
                await asyncio.to_thread(make_subtitle_png, ln["text"], spk, colors.get(spk), size, png)
                overlays.append((png, lead + ln["start"], lead + ln["end"] + 0.1))
        motion = pick_motion(sc, i) if st.get("motion", True) else "static"
        seg = work / f"seg_{i:02d}.mp4"
        await make_segment(img, overlays, length, motion, seg, size, fade_in=(i == 0), fade_out=(i == n - 1))
        lengths.append(length)
        segs.append(seg)
    if is_cancelled():
        raise RenderCancelled()
    await progress(n, n, "چسباندن صحنه‌ها")
    video = work / "video.mp4"
    await join_segments(segs, lengths, st.get("trans", "fade"), video)
    narr = work / "narration.wav"
    total = await asyncio.to_thread(build_narration, scenes, uid, lengths, t_eff, lead, narr)
    music = apath(uid, st["music"]) if st.get("music") and apath(uid, st["music"]).exists() else None
    await progress(n, n, "ترکیب صدا، موسیقی و خروجی نهایی")
    final = out_dir / "story.mp4"
    await mux(video, narr, music, final, total)
    mb = final.stat().st_size / 1024 / 1024
    shrunk = False
    if final.stat().st_size > TG_VIDEO_LIMIT:
        small = await shrink(final, size)
        shrunk = small != final
        final, mb = small, small.stat().st_size / 1024 / 1024
    shutil.rmtree(work, ignore_errors=True)
    return RenderResult(path=final, duration=total, size_mb=mb, shrunk=shrunk)


# ======================================================================
# bot — state, keyboards, helpers
# ======================================================================
router = Router()
store: SessionStore
access: Access

busy: set[int] = set()
cancel_flags: set[int] = set()
render_sem = asyncio.Semaphore(RENDER_CONCURRENCY)

PRAISE = ["عالی شد! 🎉", "دمت گرم، ادامه می‌دیم 🚀", "خیلی‌خوب! 😍", "تأیید شد ✅ بریم جلو", "این یکی حرف نداشت 👌"]
TRANS_FA = {"fade": "محو متقاطع", "fadeblack": "محو به سیاه", "cut": "برش مستقیم"}
TRANS_NEXT = {"fade": "fadeblack", "fadeblack": "cut", "cut": "fade"}
LANG_FA = {"fa": "فارسی", "en": "انگلیسی", "ar": "عربی", "tr": "ترکی"}


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def kb_char(cid: str) -> InlineKeyboardMarkup:
    return kb([("✅ تأیید", f"ch:ok:{cid}")],
              [("🔁 بازسازی", f"ch:redo:{cid}"), ("✏️ تغییر با توضیح", f"ch:fb:{cid}")],
              [("📷 عکس مرجع خودم", f"ch:photo:{cid}")])


def kb_audio(sid: int) -> InlineKeyboardMarkup:
    return kb([("🐢 کندتر", f"au:slow:{sid}"), ("🐇 تندتر", f"au:fast:{sid}")],
              [("🔉 بم‌تر", f"au:low:{sid}"), ("🔊 زیرتر", f"au:high:{sid}")],
              [("↺ برگشت به حالت اولیه", f"au:reset:{sid}")])


def kb_img(sid: int, mode: str) -> InlineKeyboardMarkup:
    ok = "✅ تأیید و صحنه‌ی بعد" if mode == "seq" else "✅ تأیید"
    return kb([(ok, f"im:ok:{sid}")], [("🔁 بازسازی", f"im:redo:{sid}"), ("✏️ تغییر با توضیح", f"im:fb:{sid}")])


def hdr(phase: str) -> str:
    n = STAGE_NUM[phase]
    return f"🧭 مرحله {n} از 5 — {STAGE_NAME[n]}\n{'▰' * n}{'▱' * (5 - n)}"


def bar(i: int, n: int, width: int = 12) -> str:
    done = round(width * i / max(n, 1))
    return "▰" * done + "▱" * (width - done) + f" {i}/{n}"


def try_lock(uid: int) -> bool:
    if uid in busy:
        return False
    busy.add(uid)
    return True


def unlock(uid: int) -> None:
    busy.discard(uid)


async def safe(coro) -> None:
    """Run a Telegram call and ignore harmless API errors (message gone, not modified...)."""
    try:
        await coro
    except TelegramAPIError:
        pass


def split_text(text: str, limit: int = 3900) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


async def send_long(target: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        await target.answer(chunk, reply_markup=markup if i == len(chunks) - 1 else None)


def find_char(s: Session, cid: str) -> dict | None:
    return next((c for c in s.characters if c["cid"] == cid), None)


def find_scene(s: Session, sid: int) -> dict | None:
    return next((x for x in s.scenes if x["id"] == sid), None)


def char_icon(c: dict) -> str:
    return "👩" if c.get("gender") == "female" else "👨"


async def report(target: Message, exc: Exception, retry: tuple[str, str] | None = None) -> None:
    log.warning("stage error: %r", exc)
    text = f"❌ {exc}" if isinstance(exc, (ImageError, TTSError, VideoError, ValueError)) else \
        f"❌ یه خطای غیرمنتظره پیش اومد: {type(exc).__name__}"
    await target.answer(text, reply_markup=kb([retry]) if retry else None)


# ---------------------------------------------------------------------- formats (/format)
FORMAT_STORY = """📖 فرمت مرحله ۱ — داستان (JSON)
{
  "title": "عنوان داستان",
  "language": "fa",
  "aspect_ratio": "16:9",
  "logline": "خلاصه‌ی یک‌خطی",
  "synopsis": "متن کامل و تأییدشده‌ی داستان",
  "style": "visual style in English (مثلاً: 3D Pixar-like, warm cinematic light)",
  "world": "توضیح فضا/مکان‌ها (اختیاری)",
  "narrator": {"gender": "male", "persona": "گرم و قصه‌گو"}
}
• language: fa / en / ar / tr
• aspect_ratio: 16:9 (افقی) · 9:16 (ریلز/شورت) · 1:1
• فقط title و synopsis الزامیه؛ متن ساده هم قبوله."""

FORMAT_CHARS = """👥 فرمت مرحله ۲ — شخصیت‌ها (JSON)
{"characters": [
  {"name": "علی", "gender": "male", "role": "قهرمان", "age": "10",
   "look": "short look summary in English",
   "prompt": "full English description for the character image: face, hair, outfit, colors, personality",
   "voice": {"pitch": "+0Hz", "rate": "+0%"}}
]}
• name و prompt الزامیه؛ بقیه اختیاری
• می‌تونی همه رو با هم بفرستی یا هر شخصیت رو جدا (یه آبجکت)
• متن ساده هم قبوله:  نام: علی / جنسیت: مرد / پرامپت: ..."""

FORMAT_SCRIPT = """🎙 فرمت مرحله ۳ — متن راوی و دیالوگ‌ها (JSON)
{"scenes": [
  {"title": "عنوان صحنه",
   "visual": "English description of the image: place, action, framing, light",
   "characters": ["علی", "مریم"],
   "camera": "zoom_in",
   "lines": [
     {"speaker": "narrator", "text": "متن راوی", "emotion": "mysterious", "intensity": 3, "pace": "slow", "pause_after": 0.6},
     {"speaker": "علی", "text": "دیالوگ", "emotion": "excited", "intensity": 4}
   ]}
]}
• speaker: narrator (یا «راوی») یا اسم دقیق یکی از شخصیت‌ها
• emotion: neutral, happy, excited, sad, angry, fear, surprised, tender, serious, mysterious, whisper, sarcastic, tired, proud, worried, determined
• intensity: ۱ تا ۵ (پیش‌فرض ۳) · pace: very_slow, slow, normal, fast, very_fast
• pause_before / pause_after: ثانیه (اختیاری) · say: متن اصلاح‌شده‌ی تلفظ فقط برای صدا (اختیاری)
• camera: auto, zoom_in, zoom_out, pan_left, pan_right, static (اختیاری)
• اگه پیام بلنده و تلگرام نمی‌ذاره، چند پیام پشت‌سرهم بفرست یا فایل .json بده."""


# ---------------------------------------------------------------------- stage screens
STORY_PROMPT = (
    "📖 داستانت رو بفرست — همون خروجی نهایی‌ای که با چت‌جی‌پی‌تی تأییدش کردی.\n\n"
    "• JSON باشه یا متن ساده، هر دو قبوله\n"
    "• اگه خیلی بلنده و تلگرام یه‌جا نمی‌ذاره، چند پیام پشت‌سرهم بفرست یا به‌شکل فایل .json/.txt\n"
    "• فرمت دقیق‌ها: /format")


def story_card(st: dict) -> str:
    narr = "زن" if st["narrator"]["gender"] == "female" else "مرد"
    text = st["logline"] or st["synopsis"]
    return (f"🎬 «{st['title']}»\n"
            f"🌐 {LANG_FA.get(st['language'], st['language'])} · 📐 {st['aspect']} · 🎙 راوی: {narr}\n"
            f"🎨 سبک: {st['style'][:140]}\n\n"
            f"📝 {text[:420]}{'…' if len(text) > 420 else ''}\n\n"
            f"({len(st['synopsis'])} کاراکتر متن داستان دریافت شد)")


async def enter_story(target: Message, s: Session) -> None:
    s.phase = "story"
    store.save(s)
    if s.story:
        await target.answer(f"{hdr('story')}\n\n{story_card(s.story)}\n\nاگه می‌خوای عوضش کنی، داستان جدید بفرست.",
                            reply_markup=kb([("✅ تأیید، برو به شخصیت‌ها", "st:ok")]))
    else:
        await target.answer(f"{hdr('story')}\n\n{STORY_PROMPT}")


def chars_status(s: Session) -> str:
    if not s.characters:
        return "هنوز شخصیتی ثبت نشده."
    return "\n".join(f"{'✅' if c.get('approved') else '⏳'} {char_icon(c)} {c['name']}" +
                     (f" — {c['role']}" if c.get("role") else "") for c in s.characters)


async def enter_characters(target: Message, s: Session) -> None:
    s.phase = "characters"
    store.save(s)
    text = (f"{hdr('characters')}\n\nپرامپت شخصیت‌ها رو از چت‌جی‌پی‌تی بگیر و بفرست. همه رو با هم (JSON) یا یکی‌یکی؛ "
            "برای هر کدوم یه تصویر مرجع می‌سازم و از تو تأیید می‌گیرم.\n"
            "📷 عکس خودت رو برای یه شخصیت داری؟ زیر کارتش «عکس مرجع خودم» رو بزن.\n\n"
            f"{chars_status(s)}")
    markup = kb([("➡️ همه تأییدن، برو به مرحله ۳", "ch:next")]) if s.characters and all(c.get("approved") for c in s.characters) else None
    await target.answer(text, reply_markup=markup)


async def enter_script(target: Message, s: Session) -> None:
    s.phase = "script"
    store.save(s)
    names = "، ".join([NARRATOR] + [c["name"] for c in s.characters])
    text = (f"{hdr('script')}\n\nحالا متن روایت و دیالوگ‌ها رو بفرست؛ همون خروجی «پرامپت مکالمات راوی» از چت‌جی‌پی‌تی "
            "(با احساس هر خط).\n\n"
            f"🗣 گوینده‌های معتبر: {names}\n"
            "اگه پیام بلنده، چند پیام پشت‌سرهم یا فایل .json بفرست. فرمت دقیق: /format")
    if s.scenes:
        text += f"\n\n(الان {len(s.scenes)} صحنه ثبت شده؛ اگه اسکریپت جدید بفرستی جایگزینش می‌شه.)"
        await target.answer(text, reply_markup=kb([("🎙 ساخت صداها", "sc:ok")]))
    else:
        await target.answer(text)


def script_summary(s: Session, warns: list[str]) -> str:
    n_lines = sum(len(x["lines"]) for x in s.scenes)
    chars = sum(len(ln["text"]) for x in s.scenes for ln in x["lines"])
    secs = int(chars / 14 + n_lines * 0.4)
    rows = []
    for x in s.scenes[:20]:
        moods = EMOTION_FA.get(x["mood"], x["mood"])
        rows.append(f"{x['id']}. {x['title']} — {len(x['lines'])} خط · {moods}")
    more = f"\n… و {len(s.scenes) - 20} صحنه‌ی دیگه" if len(s.scenes) > 20 else ""
    text = (f"✅ اسکریپت خونده شد: {len(s.scenes)} صحنه، {n_lines} خط، حدود {secs // 60} دقیقه و {secs % 60} ثانیه\n\n"
            + "\n".join(rows) + more)
    if warns:
        text += "\n\n⚠️ " + "\n⚠️ ".join(warns)
    return text


async def enter_images(target: Message, s: Session) -> None:
    s.phase = "images"
    store.save(s)
    done = sum(1 for x in s.scenes if x["img"])
    text = (f"{hdr('images')}\n\n{len(s.scenes)} صحنه = {len(s.scenes)} تصویر. هر تصویر با عکس مرجع شخصیت‌های همون صحنه ساخته می‌شه "
            "تا چهره و لباسشون ثابت بمونه.\n"
            f"🤖 مدل: {IMAGE_LABEL}" + (" (رایگان)" if IMAGE_PROVIDER == "cloudflare" else " (هزینه طبق تعرفه‌ی OpenRouter)") + "\n")
    if done:
        text += f"\n📊 تا الان {done} از {len(s.scenes)} ساخته شده.\n"
    text += "\nچطور جلو بریم؟"
    await target.answer(text, reply_markup=kb(
        [("🖼 یکی‌یکی؛ هر تصویر رو تأیید کنم", "im:mode:seq")],
        [("⚡ همه رو بساز، بعد بررسی می‌کنم", "im:mode:auto")]))


def edit_text(s: Session) -> str:
    st = s.settings
    total = sum(x["dur"] for x in s.scenes)
    music = "✅ دارم" if st.get("music") else "ندارم"
    return (f"{hdr('edit')}\n\nهمه‌چی آماده‌ست! 🎉 {len(s.scenes)} صحنه، حدود {int(total // 60)} دقیقه و {int(total % 60)} ثانیه صدا.\n\n"
            f"💬 زیرنویس: {'روشن' if st['subs'] else 'خاموش'}\n"
            f"🔀 انتقال بین صحنه‌ها: {TRANS_FA[st['trans']]}\n"
            f"🎥 حرکت دوربین: {'روشن' if st['motion'] else 'خاموش'}\n"
            f"🎵 موسیقی پس‌زمینه: {music}\n\n"
            "تنظیمات رو عوض کن یا مستقیم بزن «بساز».")


def kb_edit(s: Session) -> InlineKeyboardMarkup:
    st = s.settings
    return kb([(f"💬 زیرنویس: {'روشن' if st['subs'] else 'خاموش'}", "ed:subs")],
              [(f"🔀 انتقال: {TRANS_FA[st['trans']]}", "ed:trans")],
              [(f"🎥 حرکت دوربین: {'روشن' if st['motion'] else 'خاموش'}", "ed:motion")],
              [("🗑 حذف موسیقی", "ed:nomusic") if st.get("music") else ("🎵 افزودن موسیقی", "ed:music")],
              [("🎬 بساز!", "ed:go")],
              [("⬅️ برگشت به تصاویر", "nav:back")])


async def enter_edit(target: Message, s: Session) -> None:
    s.phase = "edit"
    s.awaiting = ""
    store.save(s)
    await target.answer(edit_text(s), reply_markup=kb_edit(s))


ENTER = {"story": enter_story, "characters": enter_characters, "script": enter_script,
         "audio": enter_script, "images": enter_images, "edit": enter_edit}
BACK = {"characters": "story", "script": "characters", "audio": "script", "images": "script", "edit": "images"}


async def go_phase(target: Message, s: Session, phase: str) -> None:
    s.awaiting, s.buf = "", ""
    await ENTER[phase](target, s)


# ---------------------------------------------------------------------- stage 2: characters
async def gen_character(target: Message, s: Session, cid: str, feedback: str | None = None) -> None:
    c = find_char(s, cid)
    if c is None:
        return
    old = apath(s.uid, c["ref"]) if c.get("ref") else None
    refs = [old] if feedback and old and old.exists() else []
    status = await target.answer(f"🎨 دارم «{c['name']}» رو می‌کشم… (حدود نیم دقیقه)")
    try:
        async with ChatActionSender.upload_photo(bot=target.bot, chat_id=target.chat.id):
            img = await gen_image(char_sheet_prompt(s.story, c, feedback), "1:1", refs)
    finally:
        await safe(status.delete())
    rel = f"chars/{cid}.png"
    path = apath(s.uid, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(img.save, path)
    c["ref"], c["approved"] = rel, False
    store.save(s)
    await send_char_card(target, s, c)


async def send_char_card(target: Message, s: Session, c: dict) -> None:
    caption = f"{char_icon(c)} {c['name']}" + (f" — {c['role']}" if c.get("role") else "") + \
              "\n\nبه نظرت همینه؟ تأیید کن یا تغییرش بده."
    await target.answer_photo(FSInputFile(apath(s.uid, c["ref"])), caption=caption, reply_markup=kb_char(c["cid"]))


def merge_characters(s: Session, parsed: list[dict]) -> list[str]:
    """Merge parsed characters into the session; return cids that need a (new) reference image."""
    need: list[str] = []
    by_key = {norm_name(c["name"]): c for c in s.characters}
    for p in parsed:
        key = norm_name(p["name"])
        if key in by_key:
            c = by_key[key]
            changed = c["prompt"] != p["prompt"]
            c.update(p)
            if changed or not c.get("ref"):
                c["approved"] = False
                need.append(c["cid"])
        else:
            if len(s.characters) >= MAX_CHARACTERS:
                continue
            s.counter += 1
            c = {**p, "cid": f"c{s.counter}", "ref": "", "approved": False}
            s.characters.append(c)
            by_key[key] = c
            need.append(c["cid"])
    store.save(s)
    return need


async def run_characters(target: Message, s: Session, need: list[str]) -> None:
    uid = s.uid
    if not try_lock(uid):
        await target.answer("⏳ هنوز دارم روی مرحله‌ی قبلی کار می‌کنم…")
        return
    try:
        for cid in need:
            await gen_character(target, s, cid)
    except Exception as exc:  # noqa: BLE001
        await report(target, exc, ("🔄 تلاش دوباره", f"ch:retry:{cid}"))
    finally:
        unlock(uid)


# ---------------------------------------------------------------------- stage 3: audio
def voice_caption(s: Session, sc: dict) -> str:
    rows = [f"🎙 صحنه {sc['id']}: {sc['title']} · ⏱ {sc['dur']:.0f} ثانیه"]
    for ln in sc["lines"][:8]:
        who = ln["speaker"]
        txt = ln["text"] if len(ln["text"]) <= 46 else ln["text"][:44] + "…"
        rows.append(f"• {who} ({EMOTION_FA.get(ln['emotion'], ln['emotion'])}): {txt}")
    if len(sc["lines"]) > 8:
        rows.append(f"… و {len(sc['lines']) - 8} خط دیگه")
    tw = sc.get("tweak", {})
    if tw.get("rate") or tw.get("pitch"):
        rows.append(f"🎚 تنظیم: سرعت {tw.get('rate', 0):+d}% · زیری {tw.get('pitch', 0):+d}Hz")
    return "\n".join(rows)[:1000]


async def send_scene_voice(target: Message, s: Session, sc: dict) -> None:
    ogg = apath(s.uid, f"scenes/{sc['id']:02d}/audio.ogg")
    markup = kb_audio(sc["id"])
    if ogg.exists():
        try:
            await target.answer_voice(FSInputFile(ogg), caption=voice_caption(s, sc), reply_markup=markup)
            return
        except TelegramAPIError as exc:
            log.warning("send_voice failed: %s", exc)
    await target.answer_document(FSInputFile(apath(s.uid, sc["audio"])), caption=voice_caption(s, sc), reply_markup=markup)


async def run_audio(target: Message, s: Session, only: list[int] | None = None) -> None:
    uid = s.uid
    if not try_lock(uid):
        await target.answer("⏳ هنوز دارم روی مرحله‌ی قبلی کار می‌کنم…")
        return
    cancel_flags.discard(uid)
    s.phase = "audio"
    store.save(s)
    todo = [x for x in s.scenes if only is None or x["id"] in only]
    status = await target.answer(f"🎙 شروع ساخت صداها…\n{bar(0, len(todo))}")
    try:
        for i, sc in enumerate(todo, 1):
            async with ChatActionSender.record_voice(bot=target.bot, chat_id=target.chat.id):
                await build_scene_audio(uid, s.story, s.characters, sc, lambda: uid in cancel_flags)
            store.save(s)
            await safe(status.edit_text(f"🎙 صحنه {i} از {len(todo)} آماده شد\n{bar(i, len(todo))}"))
            await send_scene_voice(target, s, sc)
        await safe(status.delete())
        intro = ("🎧 همه‌ی صداها آماده‌ست! گوش بده و اگه صحنه‌ای رو خواستی با دکمه‌های زیرش کندتر/تندتر/بم‌تر/زیرتر کن.\n"
                 "برای تغییر متن یا احساس، اسکریپت اصلاح‌شده رو دوباره بفرست."
                 if only is None else "🎧 صحنه‌ی جدید آماده شد. اگه راضی هستی ادامه بده، وگرنه باز تنظیمش کن.")
        await target.answer(intro, reply_markup=kb([("✅ همه خوبه، برو به تصاویر", "au:all")],
                                                   [("📝 ارسال دوباره‌ی اسکریپت", "au:resend")]))
    except RenderCancelled:
        await safe(status.edit_text("⛔ ساخت صدا لغو شد."))
    except Exception as exc:  # noqa: BLE001
        await safe(status.delete())
        await report(target, exc, ("🔄 تلاش دوباره", "sc:ok"))
    finally:
        cancel_flags.discard(uid)
        unlock(uid)


# ---------------------------------------------------------------------- stage 4: images
async def gen_scene_image(target: Message, s: Session, idx: int, feedback: str | None = None) -> None:
    sc = s.scenes[idx]
    uid = s.uid
    size = ASPECTS.get(s.story.get("aspect", "16:9"), ASPECTS["16:9"])
    by_name = {c["name"]: c for c in s.characters}
    ref_names = [n for n in sc["characters"]
                 if n in by_name and by_name[n].get("ref") and apath(uid, by_name[n]["ref"]).exists()][:4]
    refs: list[Path] = []
    old = apath(uid, sc["img"]) if sc["img"] else None
    has_old = bool(feedback and old and old.exists())
    max_refs = 4 if IMAGE_PROVIDER == "cloudflare" else 6          # کلادفلر حداکثر ۴ عکس مرجع می‌گیره
    ref_names = ref_names[:max_refs - (1 if has_old else 0)]
    if has_old:
        refs.append(old)
    refs += [apath(uid, by_name[n]["ref"]) for n in ref_names]
    prev = None
    if (sc.get("keep_background") and idx > 0 and s.scenes[idx - 1].get("img")
            and len(refs) < max_refs):
        prev = apath(uid, s.scenes[idx - 1]["img"])
        if prev.exists():
            refs.append(prev)
        else:
            prev = None
    prompt = scene_prompt(s.story, sc, by_name, ref_names, feedback, has_prev=prev is not None, has_old=has_old)
    status = await target.answer(f"🖼 دارم تصویر صحنه {sc['id']} از {len(s.scenes)} رو می‌سازم… (۳۰ تا ۶۰ ثانیه)")
    try:
        async with ChatActionSender.upload_photo(bot=target.bot, chat_id=target.chat.id):
            img = await gen_image(prompt, s.story.get("aspect", "16:9"), refs, size)
    finally:
        await safe(status.delete())
    rel = f"scenes/{sc['id']:02d}/img.png"
    path = apath(uid, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(img.save, path)
    sc["img"], sc["img_ok"] = rel, False
    store.save(s)
    mode = s.settings.get("img_mode") or "seq"
    await target.answer_photo(FSInputFile(path), caption=f"🖼 صحنه {sc['id']} از {len(s.scenes)}: {sc['title']}",
                              reply_markup=kb_img(sc["id"], mode))


async def run_images(target: Message, s: Session, redo: tuple[int, str | None] | None = None) -> None:
    """Drive stage 4. `redo=(index, feedback)` regenerates one scene; otherwise generate what's missing."""
    uid = s.uid
    if not try_lock(uid):
        await target.answer("⏳ هنوز دارم روی مرحله‌ی قبلی کار می‌کنم…")
        return
    try:
        if redo is not None:
            await gen_scene_image(target, s, redo[0], redo[1])
            return
        mode = s.settings.get("img_mode") or "seq"
        while True:
            idx = next((i for i, x in enumerate(s.scenes) if not x["img"]), None)
            if idx is None:
                break
            await gen_scene_image(target, s, idx)
            if mode == "seq":
                return
        await target.answer(
            "🎞 همه‌ی تصویرها ساخته شد! یه نگاه بنداز؛ هر کدوم رو خواستی بازسازی یا ویرایش کن. "
            "وقتی راضی بودی، ادامه بده.",
            reply_markup=kb([("✅ همه خوبن، برو به ادیت", "im:all")]))
    except Exception as exc:  # noqa: BLE001
        await report(target, exc, ("🔄 تلاش دوباره", "im:go"))
    finally:
        unlock(uid)


# ======================================================================
# access control
# ======================================================================
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return None
        if isinstance(event, Message) and event.chat.type != "private":
            return None
        if not access.is_allowed(user.id):
            if isinstance(event, CallbackQuery):
                await safe(event.answer("⛔ دسترسی نداری", show_alert=True))
            elif isinstance(event, Message):
                await safe(event.answer(f"⛔ این ربات خصوصیه.\nآیدی عددی تو: {user.id}"))
            return None
        return await handler(event, data)


# ======================================================================
# commands
# ======================================================================
WELCOME = (
    "سلام! 👋 به استودیوی داستان خوش اومدی 🎬\n\n"
    "اینجا داستانت رو قدم‌به‌قدم، با تأیید خودت، به ویدیو تبدیل می‌کنم:\n"
    "1️⃣ داستان — همون که با چت‌جی‌پی‌تی نهایی کردی\n"
    "2️⃣ شخصیت‌ها — از پرامپتشون تصویر مرجع می‌سازم\n"
    "3️⃣ متن و صدا — راوی و دیالوگ‌ها با احساس هر خط\n"
    "4️⃣ تصاویر — هر صحنه، با چهره‌ی ثابت شخصیت‌ها\n"
    "5️⃣ ادیت — صدا، زیرنویس، انتقال و موسیقی روی ویدیو\n\n"
    "دستورها:\n"
    "/format — فرمت ورودی هر مرحله\n"
    "/status — وضعیت پروژه\n"
    "/back — برگشت به مرحله‌ی قبل\n"
    "/new — پروژه‌ی جدید\n"
    "/cancel — لغو کار در حال انجام\n"
    "/clear — پاک‌کردن پیام‌های نیمه‌کاره"
)


def status_text(s: Session) -> str:
    n = STAGE_NUM[s.phase]
    appr = sum(1 for c in s.characters if c.get("approved"))
    lines = [f"📊 وضعیت پروژه — مرحله {n} از 5 ({STAGE_NAME[n]})"]
    lines.append(f"📖 داستان: {('«' + s.story['title'] + '»') if s.story else 'هنوز نیست'}")
    lines.append(f"👥 شخصیت‌ها: {appr} تأییدشده از {len(s.characters)}")
    if s.scenes:
        lines.append(f"🎞 صحنه‌ها: {len(s.scenes)} · صدا: {sum(1 for x in s.scenes if x['audio'])} · "
                     f"تصویر: {sum(1 for x in s.scenes if x['img'])} (تأییدشده {sum(1 for x in s.scenes if x['img_ok'])})")
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message):
    s = store.get(message.from_user.id)
    await message.answer(WELCOME)
    if s.story or s.characters or s.scenes:
        await message.answer(status_text(s) + "\n\nبرای ادامه دکمه‌ی زیر رو بزن، یا /new برای پروژه‌ی تازه.",
                             reply_markup=kb([("▶️ ادامه‌ی پروژه", "nav:resume")]))
    else:
        await enter_story(message, s)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(WELCOME)


@router.message(Command("format"))
async def cmd_format(message: Message):
    for block in (FORMAT_STORY, FORMAT_CHARS, FORMAT_SCRIPT):
        await send_long(message, block)


@router.message(Command("new"))
async def cmd_new(message: Message):
    await message.answer("پروژه‌ی فعلی (داستان، شخصیت‌ها، صداها و تصاویر) پاک بشه و از اول شروع کنیم؟",
                         reply_markup=kb([("🆕 آره، پروژه‌ی جدید", "new:yes"), ("❌ نه", "new:no")]))


@router.message(Command("status"))
async def cmd_status(message: Message):
    s = store.get(message.from_user.id)
    extra = "\n⏳ الان دارم روی یه کار کار می‌کنم." if s.uid in busy else ""
    await message.answer(status_text(s) + extra)


@router.message(Command("back"))
async def cmd_back(message: Message):
    s = store.get(message.from_user.id)
    if s.uid in busy:
        await message.answer("⏳ الان دارم کار می‌کنم؛ صبر کن تموم بشه یا /cancel بزن.")
        return
    prev = BACK.get(s.phase)
    if not prev:
        await message.answer("این اولین مرحله‌ست؛ جایی برای برگشت نیست.")
        return
    await go_phase(message, s, prev)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    s = store.get(message.from_user.id)
    if s.uid in busy:
        cancel_flags.add(s.uid)
        await message.answer("⛔ درخواست لغو ثبت شد؛ به محض رسیدن به یه نقطه‌ی امن متوقف می‌شم.")
    elif s.awaiting or s.buf:
        s.awaiting, s.buf = "", ""
        store.save(s)
        await message.answer("باشه، منتظر ورودی قبلی نیستم. هر وقت خواستی ادامه بده.")
    else:
        await message.answer("الان کاری در حال انجام نیست.")


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    s = store.get(message.from_user.id)
    s.buf = ""
    store.save(s)
    await message.answer("🧹 پیام‌های نیمه‌کاره پاک شد. از اول بفرست.")


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject):
    if not access.is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("استفاده: /adduser 123456789")
        return
    access.add(int(arg))
    await message.answer(f"✅ کاربر {arg} اضافه شد.")


@router.message(Command("removeuser"))
async def cmd_removeuser(message: Message, command: CommandObject):
    if not access.is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("استفاده: /removeuser 123456789")
        return
    await message.answer("✅ حذف شد." if access.remove(int(arg)) else "این کاربر توی لیست نبود.")


@router.message(Command("users"))
async def cmd_users(message: Message):
    if not access.is_admin(message.from_user.id):
        return
    users = sorted(access.users)
    await message.answer("👥 کاربران مجاز:\n" + ("\n".join(map(str, users)) if users else "(فقط ادمین)"))


# ======================================================================
# text / file / photo input
# ======================================================================
async def take_text(message: Message, s: Session, text: str, buffered: bool = True) -> str | None:
    """Collect JSON that Telegram split into several messages. Returns the full text, or None while waiting."""
    if buffered and (s.buf or is_json_like(text)):
        cand = s.buf + text
        if json_incomplete(cand):
            s.buf = cand
            store.save(s)
            await message.answer(f"📥 یه بخش دریافت شد ({len(cand)} کاراکتر تا اینجا). بقیه‌ش رو بفرست؛ "
                                 "اگه اشتباه شد /clear بزن.")
            return None
        s.buf = ""
        store.save(s)
        return cand
    return text


def _reset_scene_audio(uid: int) -> None:
    root = apath(uid, "scenes")
    if not root.exists():
        return
    for f in list(root.glob("*/audio.*")) + list(root.glob("*/l_*")):
        f.unlink(missing_ok=True)


async def handle_input(message: Message, s: Session, text: str, kind: str, buffered: bool = True) -> None:
    full = await take_text(message, s, text, buffered)
    if full is None:
        return
    uid = s.uid
    try:
        if kind == "story":
            s.story = parse_story(full)
            store.save(s)
            await message.answer(story_card(s.story), reply_markup=kb([("✅ تأیید، برو به شخصیت‌ها", "st:ok")]))
        elif kind == "characters":
            parsed = parse_characters(full)
            need = merge_characters(s, parsed)
            names = "، ".join(p["name"] for p in parsed)
            if not need:
                await message.answer(f"👌 {names} قبلاً ثبت شده بود و تغییری نکرده.")
                await enter_characters(message, s)
                return
            await message.answer(f"✅ {len(parsed)} شخصیت دریافت شد ({names}). شروع می‌کنم به کشیدنشون 🎨")
            await run_characters(message, s, need)
        else:  # script
            names = [c["name"] for c in s.characters]
            scenes, warns = parse_script(full, names)
            old = {x["id"]: x for x in s.scenes}
            for sc in scenes:                                   # تصویرهای سالم رو نگه دار
                o = old.get(sc["id"])
                if o and o["img"] and o["visual"] == sc["visual"] and o["characters"] == sc["characters"]:
                    sc["img"], sc["img_ok"] = o["img"], o["img_ok"]
            keep = {sc["id"] for sc in scenes if sc["img"]}
            _reset_scene_audio(uid)
            for x in s.scenes:
                if x["id"] not in keep:
                    shutil.rmtree(apath(uid, f"scenes/{x['id']:02d}"), ignore_errors=True)
            s.scenes, s.phase = scenes, "script"
            store.save(s)
            await send_long(message, script_summary(s, warns),
                            kb([("🎙 ساخت صداها و ارسال برای تأیید", "sc:ok")]))
    except ValueError as exc:
        s.buf = ""
        store.save(s)
        await message.answer(f"⚠️ {exc}\n\nاصلاحش کن و دوباره بفرست (فرمت‌ها: /format).")


async def route_text(message: Message, s: Session, text: str, buffered: bool = True) -> None:
    if s.uid in busy:
        await message.answer("⏳ هنوز دارم روی مرحله‌ی قبلی کار می‌کنم؛ چند لحظه صبر کن (یا /cancel).")
        return
    aw = s.awaiting
    if aw.startswith("char_fb:"):
        cid = aw.split(":", 1)[1]
        s.awaiting = ""
        store.save(s)
        await locked(message, s.uid, lambda: gen_character(message, s, cid, feedback=text), ("🔄 تلاش دوباره", f"ch:retry:{cid}"))
        return
    if aw.startswith("img_fb:"):
        sid = int(aw.split(":", 1)[1])
        s.awaiting = ""
        store.save(s)
        sc = find_scene(s, sid)
        if sc:
            await run_images(message, s, redo=(s.scenes.index(sc), text))
        return
    if aw.startswith("char_photo:") or aw == "music":
        await message.answer("📎 منتظر " + ("عکس" if aw.startswith("char_photo:") else "فایل صوتی") +
                             " هستم. اگه پشیمون شدی /cancel بزن.")
        return
    if s.phase == "story":
        await handle_input(message, s, text, "story", buffered)
    elif s.phase == "characters":
        if not s.story:
            await message.answer("اول داستان رو بفرست و تأیید کن.")
            return
        await handle_input(message, s, text, "characters", buffered)
    elif s.phase in ("script", "audio"):
        await handle_input(message, s, text, "script", buffered)
    else:
        await message.answer("توی این مرحله با دکمه‌ها جلو می‌ریم 👆 (برای تغییر مراحل قبلی /back بزن).")


async def locked(target: Message, uid: int, work, retry: tuple[str, str] | None = None) -> None:
    if not try_lock(uid):
        await target.answer("⏳ هنوز دارم روی مرحله‌ی قبلی کار می‌کنم…")
        return
    try:
        await work()
    except Exception as exc:  # noqa: BLE001
        await report(target, exc, retry)
    finally:
        unlock(uid)


MUSIC_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
             "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac"}


@router.message(F.photo)
async def on_photo(message: Message):
    s = store.get(message.from_user.id)
    if not s.awaiting.startswith("char_photo:"):
        await message.answer("📷 عکس رو فقط وقتی برای یه شخصیت «عکس مرجع خودم» رو زده باشی می‌تونم استفاده کنم.")
        return
    cid = s.awaiting.split(":", 1)[1]
    c = find_char(s, cid)
    if c is None:
        s.awaiting = ""
        store.save(s)
        return
    buf = io.BytesIO()
    await message.bot.download(message.photo[-1], destination=buf)
    rel = f"chars/{cid}.png"
    path = apath(s.uid, rel)
    path.parent.mkdir(parents=True, exist_ok=True)

    def _save() -> None:
        im = ImageOps.exif_transpose(Image.open(buf)).convert("RGB")
        im.thumbnail((1536, 1536))
        im.save(path)

    await asyncio.to_thread(_save)
    c["ref"], c["approved"], s.awaiting = rel, False, ""
    store.save(s)
    await message.answer("📷 عکست رو به‌عنوان مرجع «" + c["name"] + "» گذاشتم:")
    await send_char_card(message, s, c)


@router.message(F.document | F.audio | F.voice)
async def on_file(message: Message):
    s = store.get(message.from_user.id)
    obj = message.audio or message.voice or message.document
    if s.awaiting == "music":
        mime = (getattr(obj, "mime_type", "") or "").lower()
        if not (mime.startswith("audio/") or mime in ("application/ogg",)):
            await message.answer("این فایل صوتی به نظر نمی‌رسه. یه mp3/m4a بفرست یا /cancel.")
            return
        if (obj.file_size or 0) > 20 * 1024 * 1024:
            await message.answer("فایل بزرگ‌تر از ۲۰ مگابایته؛ تلگرام اجازه‌ی دانلودش رو به ربات نمی‌ده. یه فایل کوچک‌تر بفرست.")
            return
        ext = MUSIC_EXT.get(mime, ".mp3")
        rel = f"music{ext}"
        await message.bot.download(obj, destination=apath(s.uid, rel))
        s.settings["music"], s.awaiting = rel, ""
        store.save(s)
        await message.answer("🎵 موسیقی ثبت شد! زیر صدای راوی خودکار کم‌صدا می‌شه (Ducking).")
        await message.answer(edit_text(s), reply_markup=kb_edit(s))
        return
    name = (getattr(obj, "file_name", "") or "").lower()
    if message.document and name.endswith((".json", ".txt", ".md")) and (obj.file_size or 0) < 1_500_000:
        buf = io.BytesIO()
        await message.bot.download(obj, destination=buf)
        try:
            text = buf.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError:
            await message.answer("فایل باید UTF-8 باشه.")
            return
        await route_text(message, s, text, buffered=False)
        return
    await message.answer("این فایل رو الان لازم ندارم. 🙂 (متن/JSON یا فایل .json/.txt برای ورودی مراحل، و صوت فقط برای موسیقی)")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    await route_text(message, store.get(message.from_user.id), message.text)


@router.message()
async def on_other(message: Message):
    await message.answer("متوجه نشدم 🤔 — /help رو بزن یا متن/JSON مرحله‌ی فعلی رو بفرست.")


# ======================================================================
# callbacks
# ======================================================================
async def _begin(cb: CallbackQuery, need_idle: bool = False) -> Session | None:
    if cb.message is None:
        await safe(cb.answer())
        return None
    uid = cb.from_user.id
    if need_idle and uid in busy:
        await safe(cb.answer("⏳ هنوز دارم روی مرحله‌ی قبلی کار می‌کنم…"))
        return None
    await safe(cb.answer())
    return store.get(uid)


async def _strip(cb: CallbackQuery, note: str | None = None) -> None:
    await safe(cb.message.edit_reply_markup(reply_markup=None))
    if note and cb.message.caption is not None:
        await safe(cb.message.edit_caption(caption=f"{cb.message.caption.split(chr(10))[0]}\n{note}"))


@router.callback_query(F.data == "st:ok")
async def cb_story_ok(cb: CallbackQuery):
    s = await _begin(cb)
    if s is None or not s.story:
        return
    await _strip(cb)
    await cb.message.answer(random.choice(PRAISE))
    await go_phase(cb.message, s, "characters")


@router.callback_query(F.data.startswith("ch:"))
async def cb_chars(cb: CallbackQuery):
    parts = cb.data.split(":")
    op, cid = parts[1], parts[2] if len(parts) > 2 else ""
    s = await _begin(cb, need_idle=op in ("redo", "retry", "next"))
    if s is None:
        return
    if op == "next":
        if s.characters and all(c.get("approved") for c in s.characters):
            await _strip(cb)
            await go_phase(cb.message, s, "script")
        return
    c = find_char(s, cid)
    if c is None:
        return
    if op == "ok":
        c["approved"] = True
        store.save(s)
        await _strip(cb, "✅ تأیید شد")
        if all(x.get("approved") for x in s.characters):
            await cb.message.answer(f"{random.choice(PRAISE)}\nهمه‌ی شخصیت‌ها تأیید شدن. اگه شخصیت دیگه‌ای هم داری همین‌جا بفرست؛ وگرنه بریم مرحله‌ی بعد.",
                                    reply_markup=kb([("➡️ برو به مرحله ۳: متن و صدا", "ch:next")]))
    elif op in ("redo", "retry"):
        await _strip(cb, "🔁 نسخه‌ی قبلی")
        await locked(cb.message, s.uid, lambda: gen_character(cb.message, s, cid), ("🔄 تلاش دوباره", f"ch:retry:{cid}"))
    elif op == "fb":
        s.awaiting = f"char_fb:{cid}"
        store.save(s)
        await cb.message.answer(f"✏️ بنویس چی توی «{c['name']}» عوض بشه (مثلاً: «موهاش بلندتر باشه و کت آبی بپوشه»).")
    elif op == "photo":
        s.awaiting = f"char_photo:{cid}"
        store.save(s)
        await cb.message.answer(f"📷 عکس مرجع «{c['name']}» رو بفرست (ترجیحاً تمام‌قد یا نیم‌تنه، با نور خوب).")


@router.callback_query(F.data == "sc:ok")
async def cb_script_ok(cb: CallbackQuery):
    s = await _begin(cb, need_idle=True)
    if s is None or not s.scenes:
        return
    await _strip(cb)
    await run_audio(cb.message, s)


@router.callback_query(F.data.startswith("au:"))
async def cb_audio(cb: CallbackQuery):
    _, op, *rest = cb.data.split(":")
    s = await _begin(cb, need_idle=op not in ("resend",))
    if s is None:
        return
    if op == "all":
        if all(x["audio"] for x in s.scenes):
            await _strip(cb)
            await cb.message.answer(random.choice(PRAISE))
            await go_phase(cb.message, s, "images")
        return
    if op == "resend":
        await _strip(cb)
        s.phase = "script"
        store.save(s)
        await cb.message.answer("باشه. اسکریپت اصلاح‌شده رو بفرست (جایگزین قبلی می‌شه و صداها دوباره ساخته می‌شن).")
        return
    sc = find_scene(s, int(rest[0])) if rest else None
    if sc is None:
        return
    tw = sc.setdefault("tweak", {"rate": 0, "pitch": 0})
    if op == "slow":
        tw["rate"] = max(-30, tw["rate"] - 8)
    elif op == "fast":
        tw["rate"] = min(30, tw["rate"] + 8)
    elif op == "low":
        tw["pitch"] = max(-16, tw["pitch"] - 4)
    elif op == "high":
        tw["pitch"] = min(16, tw["pitch"] + 4)
    elif op == "reset":
        tw["rate"], tw["pitch"] = 0, 0
    store.save(s)
    await safe(cb.message.delete())
    await run_audio(cb.message, s, only=[sc["id"]])


@router.callback_query(F.data.startswith("im:"))
async def cb_images(cb: CallbackQuery):
    _, op, *rest = cb.data.split(":")
    s = await _begin(cb, need_idle=True)
    if s is None:
        return
    if op == "mode":
        s.settings["img_mode"] = rest[0] if rest and rest[0] in ("seq", "auto") else "seq"
        store.save(s)
        await _strip(cb)
        await run_images(cb.message, s)
        return
    if op == "go":
        await run_images(cb.message, s)
        return
    if op == "all":
        for x in s.scenes:
            if x["img"]:
                x["img_ok"] = True
        if not all(x["img"] for x in s.scenes):
            await cb.message.answer("هنوز بعضی صحنه‌ها تصویر ندارن؛ بذار بقیه رو بسازم.")
            await run_images(cb.message, s)
            return
        store.save(s)
        await _strip(cb)
        await go_phase(cb.message, s, "edit")
        return
    sc = find_scene(s, int(rest[0])) if rest else None
    if sc is None:
        return
    idx = s.scenes.index(sc)
    if op == "ok":
        sc["img_ok"] = True
        store.save(s)
        await _strip(cb, "✅ تأیید شد")
        if (s.settings.get("img_mode") or "seq") == "seq":
            await run_images(cb.message, s)
        elif all(x["img_ok"] for x in s.scenes):
            await cb.message.answer(f"{random.choice(PRAISE)}\nهمه‌ی تصاویر تأیید شد.",
                                    reply_markup=kb([("➡️ برو به ادیت و ساخت ویدیو", "im:all")]))
    elif op == "redo":
        await _strip(cb, "🔁 نسخه‌ی قبلی")
        await run_images(cb.message, s, redo=(idx, None))
    elif op == "fb":
        s.awaiting = f"img_fb:{sc['id']}"
        store.save(s)
        await cb.message.answer(f"✏️ بنویس توی تصویر صحنه {sc['id']} چی عوض بشه (مثلاً: «شب باشه و بارون بیاد»).")


async def run_render(msg: Message, s: Session) -> None:
    uid = s.uid
    if not try_lock(uid):
        await msg.answer("⏳ هنوز دارم روی مرحله‌ی قبلی کار می‌کنم…")
        return
    cancel_flags.discard(uid)
    status = await msg.answer("🎬 شروع ساخت ویدیو…")
    last = [0.0]

    async def progress(i: int, n: int, label: str) -> None:
        now = time.monotonic()
        if now - last[0] < 1.5 and i < n:
            return
        last[0] = now
        await safe(status.edit_text(f"🎬 {label}…\n{bar(i, n)}"))

    try:
        async with render_sem:
            async with ChatActionSender.upload_video(bot=msg.bot, chat_id=msg.chat.id):
                res = await render_video(uid, s, progress, lambda: uid in cancel_flags)
        await safe(status.edit_text("📤 ویدیو آماده شد، دارم می‌فرستمش…"))
        size = ASPECTS.get(s.story.get("aspect", "16:9"), ASPECTS["16:9"])
        w, h = (size if not res.shrunk else ((854, 480) if size[0] >= size[1] else (480, 854)))
        mins, secs = int(res.duration // 60), int(res.duration % 60)
        caption = f"🎬 «{s.story.get('title', '')}» · ⏱ {mins}:{secs:02d} · {res.size_mb:.1f}MB"
        if res.shrunk:
            caption += "\n(برای محدودیت حجم تلگرام کیفیت کم شد)"
        await msg.answer_video(FSInputFile(res.path), duration=int(res.duration), width=w, height=h,
                               supports_streaming=True, caption=caption)
        await safe(status.delete())
        await msg.answer("تمومه! 🎉 خوشت اومد؟ می‌تونی با تنظیمات دیگه (زیرنویس، انتقال، موسیقی) دوباره بسازی، یا سراغ داستان بعدی بری.",
                         reply_markup=kb([("🎛 تنظیمات دیگه و ساخت دوباره", "ed:again")], [("🆕 پروژه‌ی جدید", "new:ask")]))
    except RenderCancelled:
        await safe(status.edit_text("⛔ ساخت ویدیو لغو شد."))
    except TelegramAPIError as exc:
        await safe(status.delete())
        await msg.answer(f"❌ ارسال ویدیو به تلگرام ناموفق بود ({str(exc)[:120]}). احتمالاً حجمش زیاده؛ "
                         "زیرنویس یا موسیقی رو خاموش کن یا صحنه‌ها رو کمتر کن و دوباره بساز.",
                         reply_markup=kb([("🎛 تنظیمات", "ed:again")]))
    except Exception as exc:  # noqa: BLE001
        await safe(status.delete())
        await report(msg, exc, ("🔄 تلاش دوباره", "ed:go"))
    finally:
        cancel_flags.discard(uid)
        unlock(uid)


@router.callback_query(F.data.startswith("ed:"))
async def cb_edit(cb: CallbackQuery):
    op = cb.data.split(":")[1]
    s = await _begin(cb, need_idle=True)
    if s is None:
        return
    st = s.settings
    if op == "again":
        await enter_edit(cb.message, s)
        return
    if op == "go":
        if not all(x["img"] and x["audio"] for x in s.scenes):
            await cb.message.answer("بعضی صحنه‌ها هنوز تصویر یا صدا ندارن؛ مرحله‌های قبل رو کامل کن (/back).")
            return
        await _strip(cb)
        await run_render(cb.message, s)
        return
    if op == "subs":
        st["subs"] = not st["subs"]
    elif op == "trans":
        st["trans"] = TRANS_NEXT.get(st["trans"], "fade")
    elif op == "motion":
        st["motion"] = not st["motion"]
    elif op == "nomusic":
        st["music"] = ""
    elif op == "music":
        s.awaiting = "music"
        store.save(s)
        await cb.message.answer("🎵 فایل موسیقی (mp3/m4a) رو بفرست — هر چی تا ۲۰ مگابایت. بدون کپی‌رایت باشه ها 😉\nپشیمون شدی؟ /cancel")
        return
    store.save(s)
    await safe(cb.message.edit_text(edit_text(s), reply_markup=kb_edit(s)))


@router.callback_query(F.data.startswith("nav:"))
async def cb_nav(cb: CallbackQuery):
    op = cb.data.split(":")[1]
    s = await _begin(cb, need_idle=True)
    if s is None:
        return
    await _strip(cb)
    if op == "resume":
        await go_phase(cb.message, s, s.phase)
    elif op == "back":
        prev = BACK.get(s.phase)
        if prev:
            await go_phase(cb.message, s, prev)


@router.callback_query(F.data.startswith("new:"))
async def cb_new(cb: CallbackQuery):
    op = cb.data.split(":")[1]
    s = await _begin(cb, need_idle=True)
    if s is None:
        return
    if op == "ask":
        await cb.message.answer("پروژه‌ی فعلی پاک بشه و از اول شروع کنیم؟",
                                reply_markup=kb([("🆕 آره، پروژه‌ی جدید", "new:yes"), ("❌ نه", "new:no")]))
    elif op == "yes":
        await _strip(cb)
        s = store.reset(s.uid)
        await cb.message.answer("🧹 پروژه‌ی قبلی پاک شد. بریم سراغ یه داستان تازه! ✨")
        await enter_story(cb.message, s)
    else:
        await safe(cb.message.edit_reply_markup(reply_markup=None))


@router.callback_query()
async def cb_unknown(cb: CallbackQuery):
    await safe(cb.answer())


# ======================================================================
# errors / startup
# ======================================================================
@router.error()
async def on_error(event: ErrorEvent):
    log.exception("Unhandled error", exc_info=event.exception)
    return True


async def main() -> None:
    global store, access
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    problems = check()
    if problems:
        for p in problems:
            print("❌", p)
        sys.exit(1)
    ensure_dirs()
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("❌ ffmpeg/ffprobe نصب نیست. (روی ریلوی با Dockerfile خودکار نصب می‌شه)")
        sys.exit(1)
    if font_path() is None:
        log.warning("هیچ فونتی پیدا نشد! فونت Vazirmatn رو توی assets/fonts بذار.")
    elif "vazir" not in (font_path() or "").lower():
        log.warning("فونت پیش‌فرض سیستم استفاده می‌شه. برای زیرنویس زیباتر Vazirmatn رو توی assets/fonts بذار.")

    store = SessionStore(SESSIONS_DIR)
    access = Access(DATA_DIR / "users.json", ADMIN_ID)

    session = AiohttpSession(proxy=TELEGRAM_PROXY, timeout=300)
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher()
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    dp.include_router(router)

    me = await bot.get_me()
    log.info("Bot @%s started (admin=%s, image provider=%s, model=%s)", me.username, ADMIN_ID, IMAGE_PROVIDER, IMAGE_LABEL)
    await bot.set_my_commands([
        BotCommand(command="new", description="پروژه‌ی جدید"),
        BotCommand(command="back", description="برگشت به مرحله‌ی قبل"),
        BotCommand(command="status", description="وضعیت پروژه"),
        BotCommand(command="format", description="فرمت ورودی هر مرحله"),
        BotCommand(command="cancel", description="لغو کار در حال انجام"),
        BotCommand(command="clear", description="پاک‌کردن پیام‌های نیمه‌کاره"),
        BotCommand(command="help", description="راهنما"),
    ])
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
