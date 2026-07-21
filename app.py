import os
import re
import io
import csv
import json
import statistics
import base64
import time
import secrets
import hashlib
from urllib.parse import quote_plus
from datetime import datetime, timedelta
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response, stream_with_context, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import random

load_dotenv()

# ---------------------------------------------------------------------------
# App / config
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "pic")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB
_database_url = os.environ.get("DATABASE_URL", "sqlite:///prism.db")
# Render (and some other hosts) hand out "postgres://" URLs; SQLAlchemy needs
# "postgresql://", and we point it at the psycopg3 driver (not psycopg2) since
# psycopg2 has no prebuilt wheels for newer Python versions yet.
if _database_url.startswith("postgres://"):
    _database_url = _database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif _database_url.startswith("postgresql://"):
    _database_url = _database_url.replace("postgresql://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

# Render (and most PaaS hosts) terminate TLS at a load balancer and forward
# plain HTTP to the app, setting X-Forwarded-Proto/X-Forwarded-For headers.
# Without ProxyFix, `request.is_secure` and `request.remote_addr` would be
# wrong on every request — breaking the HTTPS redirect below and making
# every rate-limit/lockout keyed off the load balancer's IP instead of the
# real visitor's.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Render sets RENDER=true on every deployed service; locally that's unset,
# so HTTPS enforcement and secure-only cookies stay off for local dev (where
# there's no TLS) and turn on automatically once deployed.
IS_PRODUCTION = os.environ.get("RENDER") == "true" or os.environ.get("FORCE_HTTPS") == "true"
app.config["SESSION_COOKIE_HTTPONLY"] = True       # JS can never read the session cookie
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"       # blocks it being sent on cross-site requests
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION  # HTTPS-only cookie once deployed


@app.before_request
def _enforce_https():
    if IS_PRODUCTION and not request.is_secure:
        return redirect(request.url.replace("http://", "https://", 1), code=301)

db = SQLAlchemy(app)

# --- Security: CSRF protection on every state-changing request --------------
# Covers both classic HTML <form> posts (login/signup/settings) and the
# dashboard's JSON fetch() calls (dashboard.js sends the token back via the
# X-CSRFToken header, read from the <meta name="csrf-token"> tag).
app.config["WTF_CSRF_TIME_LIMIT"] = None  # tokens last the whole session, not just 1h
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    # A generic, non-technical message — don't leak *why* validation failed.
    if request.path.startswith("/api/"):
        return jsonify({"error": "Your session has expired or this request looks invalid. Please refresh the page and try again."}), 400
    flash("Your session expired — please try that again.", "error")
    return redirect(request.referrer or url_for("index"))


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": generate_csrf}


# --- Security: rate limiting -------------------------------------------------
# Keyed by IP address. Generous global default so normal use is never
# affected; individual sensitive routes (login, signup, password reset, AI
# endpoints) apply their own tighter limits below via @limiter.limit(...).
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["600 per hour", "60 per minute"],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)

# --- Admin access -------------------------------------------------------------
# Only this account sees the admin analytics panel. Checked by email, not a
# toggleable DB flag, so it can't accidentally be granted to anyone else.
ADMIN_EMAILS = {"muhammedtesleemolatundun@gmail.com"}


def is_admin_email(email):
    return (email or "").strip().lower() in ADMIN_EMAILS


# --- File upload validation ---------------------------------------------------
# A file's extension and the browser's declared Content-Type are both just
# labels the uploader chose — neither proves the bytes are actually a safe
# image. This decodes the real pixel data with Pillow before anything is
# saved to disk or sent to the AI vision model, so a renamed script or a
# corrupt/malicious file gets rejected instead of silently accepted.
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_PROFILE_PICTURE_BYTES = 5 * 1024 * 1024   # 5MB
MAX_SCAN_IMAGE_BYTES = 8 * 1024 * 1024        # 8MB


def validate_image_upload(file_storage, max_bytes):
    """Returns (ok: bool, error_message_or_None, raw_bytes_or_None, mime_or_None).
    Actually decodes the image (Pillow) rather than trusting the filename
    extension or the browser-supplied Content-Type header, both of which an
    attacker fully controls."""
    if not file_storage or not file_storage.filename:
        return False, "No file was attached.", None, None

    data = file_storage.read()
    file_storage.seek(0)
    if not data:
        return False, "That file came through empty.", None, None
    if len(data) > max_bytes:
        return False, f"That image is too large — please use one under {max_bytes // (1024*1024)}MB.", None, None

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.verify()  # raises if the pixel data is corrupt/not actually an image
        detected_mime = Image.MIME.get(img.format, "")
    except Exception:
        return False, "That file doesn't look like a valid image — try a JPG, PNG, WEBP, or GIF.", None, None

    if detected_mime not in ALLOWED_IMAGE_MIME_TYPES:
        return False, "Only JPG, PNG, WEBP, and GIF images are supported.", None, None

    return True, None, data, detected_mime


_IMAGE_EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def save_uploaded_profile_picture(file_storage):
    """Validate + save a profile picture. Returns (relative_path_or_None,
    error_message_or_None)."""
    ok, error, data, mime = validate_image_upload(file_storage, MAX_PROFILE_PICTURE_BYTES)
    if not ok:
        return None, error
    ext = _IMAGE_EXT_BY_MIME.get(mime, "jpg")
    filename = secure_filename(file_storage.filename) or f"upload.{ext}"
    unique_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
    upload_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
    with open(upload_path, "wb") as f:
        f.write(data)
    return os.path.join("pic", unique_name).replace("\\", "/"), None


# --- Brevo (Sendinblue) transactional email ---------------------------------
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL", "muhammedtesleemolatundun@gmail.com")
BREVO_SENDER_NAME = os.environ.get("BREVO_SENDER_NAME", "PRISM")
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

EMAIL_CODE_LENGTH = 6

# --- Groq (AI Buying Intelligence engine) ------------------------------------
# Groq's Chat Completions API is OpenAI-compatible: https://api.groq.com/openai/v1
# Free API key, no card required: https://console.groq.com/keys
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TEXT_MODEL = os.environ.get("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# Single boolean flag the app / dashboard consult to know whether "Prism" is online.
PRISM_API_KEY = GROQ_API_KEY

# --- SerpApi (Google Shopping — real prices from Amazon/Walmart/Best Buy/etc) -
# Phase 1 of real pricing: a shopping-search aggregator instead of scraping
# every retailer ourselves. Free key + sign-up: https://serpapi.com
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
SERPAPI_URL = "https://serpapi.com/search.json"

# Only surface offers from stores a shopper would recognize and trust —
# anything else Google Shopping returns (random third-party sellers) is
# dropped rather than shown as if we vouch for it.
LIVE_PRICE_RETAILERS = ["Amazon", "Walmart", "Best Buy", "Target", "eBay"]

# Real prices move, but not so often that every request needs a fresh paid
# SerpApi call — repeat searches within this window are served from cache.
LIVE_PRICE_CACHE_TTL_SECONDS = 6 * 60 * 60

PRISM_SYSTEM_PROMPT_BASE = (
    "You are Prism, the AI shopping intelligence platform. Speak with calm, specific "
    "confidence — never generic. Ground every answer in the exact product/category mentioned. "
    "For comparisons or recommendations, give a clear verdict, concrete tradeoffs (performance, "
    "battery, camera, value, repairability, long-term cost) and a short 'why' per pick. Reason "
    "from your knowledge of typical specs and pricing bands, and note that figures are "
    "estimates. Format responses in clean markdown: headers, bold, tables, bullet lists.\n\n"
    "VERIFICATION DISCIPLINE: You cannot browse live listings, so never state a spec, price, "
    "release date or availability claim as flat fact — frame it as your best estimate and "
    "explicitly tell the shopper to confirm the live price, condition, and stock on the "
    "marketplace links the app attaches to your picks before paying. If you are unsure "
    "about a specific number, say so plainly rather than inventing false precision. Never "
    "fabricate a marketplace name, URL, or review quote — the app attaches real marketplace "
    "links itself; you only need to name the product clearly and accurately so those lookups "
    "succeed.\n\n"
    "TRUTHFULNESS OVER HEDGING: Shoppers are making real financial decisions with your answers, "
    "so be direct, not vague. Don't hide behind filler words like 'probably', 'might', 'could "
    "be', or 'it's possible that' when you actually have a confident, specific answer — state it "
    "plainly. Reserve hedged language only for the genuinely uncertain parts, and even then say "
    "exactly what you're unsure about (e.g. 'exact price varies by retailer' is useful; 'it's "
    "probably fine' is not). Never generate a filler price, spec, or verdict just to fill a field "
    "— if you truly cannot estimate something, say so in plain words instead of guessing a "
    "plausible-looking but made-up number.\n\n"
    "BUDGET HONESTY: If a shopper's stated budget is unrealistic for what they're asking for "
    "(too small for the category, or for the specific product named), say so clearly and early — "
    "do not pretend a workable option exists at that price. Tell them plainly how much more they "
    "would realistically need, or what real tradeoffs (older model, smaller storage, refurbished, "
    "different category) actually fit their budget. Never stretch, round down, or reframe a "
    "product's real price to make it sound like it fits a budget it doesn't.\n\n"
    "NEUTRALITY & ACCURACY: Do not favor any brand, retailer, or product for any reason other "
    "than the merits relevant to the shopper's stated needs and budget — never because a brand "
    "is more popular, more premium-sounding, or mentioned more often in training data. When two "
    "options are genuinely close, say so instead of forcing a false winner. Always give the "
    "real downsides of your top pick, not just its strengths. If a claim is disputed, uncertain, "
    "or you simply don't know, say that plainly rather than filling the gap with a confident-"
    "sounding guess — a hedged, honest answer is always better than a fluent but wrong one. "
    "Do not state opinions on contested political or social topics as if they were settled facts."
)

# --- Currencies Prism can quote in, and the countries offered in Settings ---
# The shopper picks ONE currency (and country) once, in Settings, and every
# AI answer for that account is pinned to it — this stops the previous
# behaviour where the model would drift between NGN/USD/etc mid-conversation.
CURRENCY_OPTIONS = [
    {"code": "NGN", "symbol": "₦", "label": "Nigerian Naira (₦)"},
    {"code": "USD", "symbol": "$", "label": "US Dollar ($)"},
    {"code": "GBP", "symbol": "£", "label": "British Pound (£)"},
    {"code": "EUR", "symbol": "€", "label": "Euro (€)"},
    {"code": "GHS", "symbol": "₵", "label": "Ghanaian Cedi (₵)"},
    {"code": "KES", "symbol": "KSh", "label": "Kenyan Shilling (KSh)"},
    {"code": "ZAR", "symbol": "R", "label": "South African Rand (R)"},
    {"code": "CAD", "symbol": "$", "label": "Canadian Dollar ($)"},
    {"code": "INR", "symbol": "₹", "label": "Indian Rupee (₹)"},
]
_CURRENCY_SYMBOLS = {c["code"]: c["symbol"] for c in CURRENCY_OPTIONS}
_VALID_CURRENCY_CODES = {c["code"] for c in CURRENCY_OPTIONS}

COUNTRY_CURRENCY_MAP = {
    "Nigeria": "NGN", "Ghana": "GHS", "Kenya": "KES", "South Africa": "ZAR",
    "United States": "USD", "United Kingdom": "GBP", "Canada": "CAD",
    "India": "INR", "Ireland": "EUR", "Germany": "EUR", "France": "EUR",
    "Other": "USD",
}
COUNTRY_OPTIONS = list(COUNTRY_CURRENCY_MAP.keys())

DEFAULT_CURRENCY = "NGN"
DEFAULT_COUNTRY = "Nigeria"


def normalize_currency(code):
    code = (code or "").strip().upper()
    return code if code in _VALID_CURRENCY_CODES else DEFAULT_CURRENCY


def account_currency():
    """The logged-in shopper's fixed account currency (set once in Settings).
    Routes use this instead of trusting a currency passed in the request body,
    so a shopper's quotes never drift between currencies mid-session."""
    try:
        uid = session.get("user_id")
        u = User.query.get(uid) if uid else None
        return u.currency if u and u.currency else DEFAULT_CURRENCY
    except Exception:
        return DEFAULT_CURRENCY


# --- Live FX rates, fetched once and cached -- never guessed by the model ---
# The model has no live internet access, so instead of letting it *guess* an
# exchange rate from stale training data (which is how it invented a wildly
# wrong ratio before, and how it used to drift between currencies mid-answer),
# we fetch real rates here and hand the model a single fixed number it must
# use for the shopper's chosen currency. Cached for a few hours so the number
# stays STABLE across a session instead of subtly changing between requests.
FX_API_URL = "https://open.er-api.com/v6/latest/USD"  # free, keyless, updated ~daily
FX_CACHE_TTL_SECONDS = 6 * 3600
FX_FALLBACK_RATES = {  # only used if the FX API is unreachable
    "NGN": 1550.0, "USD": 1.0, "GBP": 0.78, "EUR": 0.92, "GHS": 15.3,
    "KES": 129.0, "ZAR": 18.1, "CAD": 1.37, "INR": 83.5,
}
_fx_cache = {"rates": None, "fetched_at": 0}


def get_fx_rates():
    """Return the cached (or freshly fetched) {currency: rate_per_usd} dict.
    The same cached snapshot is served to every request within the TTL, so
    the rate a shopper sees stays consistent instead of shifting from one
    call to the next."""
    now = time.time()
    if _fx_cache["rates"] and (now - _fx_cache["fetched_at"] < FX_CACHE_TTL_SECONDS):
        return _fx_cache["rates"]
    try:
        resp = requests.get(FX_API_URL, timeout=6)
        data = resp.json()
        rates = data.get("rates") or {}
        if rates:
            _fx_cache["rates"] = rates
            _fx_cache["fetched_at"] = now
            return rates
    except Exception:
        pass
    # Keep serving a stale cache rather than nothing, if we have one.
    return _fx_cache["rates"] or FX_FALLBACK_RATES


def get_usd_rate_for(currency):
    """1 USD ≈ this many units of `currency`, from the cached live snapshot."""
    currency = normalize_currency(currency)
    rates = get_fx_rates()
    rate = rates.get(currency) or FX_FALLBACK_RATES.get(currency, 1.0)
    return round(float(rate), 4)


def get_usd_to_ngn_rate():
    return get_usd_rate_for("NGN")


def build_system_prompt(memory_notes=None, currency=None):
    """System prompt + the shopper's fixed account currency and its current
    live FX rate, so Prism always quotes in ONE currency instead of drifting
    between them. Optionally folds in a shopper's derived preferences ("AI
    Shopping Memory") so recommendations read as personalized, not generic."""
    if currency is None:
        # Fall back to whatever's on the logged-in account, so every route
        # gets consistent currency behaviour even if it doesn't explicitly
        # pass one in.
        try:
            uid = session.get("user_id")
            u = User.query.get(uid) if uid else None
            currency = u.currency if u else DEFAULT_CURRENCY
        except Exception:
            currency = DEFAULT_CURRENCY
    currency = normalize_currency(currency)
    symbol = _CURRENCY_SYMBOLS.get(currency, "")
    rate = get_usd_rate_for(currency)
    currency_note = (
        f"\n\nSHOPPER'S FIXED CURRENCY: This account's currency is set to {currency} ({symbol}), "
        f"chosen in Settings. Express EVERY price, budget figure, and estimate in {currency} "
        f"only — never switch to another currency partway through a response, even if the "
        f"product is more commonly priced in a different currency internationally. If you need "
        f"to reason from a USD or NGN price band in your own knowledge, silently convert it to "
        f"{currency} before presenting it to the shopper.\n\n"
        f"LIVE FX RATE (fetched recently, treat as authoritative for this account): "
        f"1 USD ≈ {symbol}{rate:,.4f} {currency}. Use this exact rate for any conversion — show "
        f"the arithmetic briefly if it's relevant — and mention that FX rates drift day to day "
        f"so the shopper should sanity-check it against a live converter if it's been a while."
    )
    memory_note = ""
    if memory_notes:
        memory_note = (
            "\n\nSHOPPER MEMORY (derived from this account's past searches, wishlist "
            "and stats — use it to personalize your answer where relevant, but never "
            "state it back as if you're guessing/reading their mind, just quietly "
            "factor it in):\n- " + "\n- ".join(memory_notes)
        )
    return PRISM_SYSTEM_PROMPT_BASE + currency_note + memory_note


_CURRENCY_NUMBER_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def _parse_amount(text):
    """Pull the first plausible numeric amount out of a free-text price
    string like '₦250,000' or '$1,200 - $1,400' -> 250000.0 / 1200.0."""
    if not text:
        return None
    m = _CURRENCY_NUMBER_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


_BRAND_KEYWORDS = [
    "Apple", "Samsung", "Sony", "Logitech", "Dell", "HP", "Lenovo", "Asus",
    "Acer", "MSI", "LG", "Bose", "JBL", "Xiaomi", "Google", "Microsoft",
    "Razer", "Nvidia", "AMD", "Intel", "Nike", "Adidas", "Canon", "Nikon",
]


def build_memory_profile(user_id):
    """Heuristic (no AI call) 'AI Shopping Memory' profile: scans this
    account's stored wishlist/history/stats, PLUS whatever the shopper has
    directly declared in Settings, to surface plain-language preferences —
    top category, typical budget ceiling, favored brands. Declared interests
    are included unconditionally (not just once activity exists), so a brand
    new account that's only filled in "Shopping Interests" still gets
    personalized answers from message one."""
    user = User.query.get(user_id)
    wishlist = WishlistItem.query.filter_by(user_id=user_id).all()
    history = HistoryItem.query.filter_by(user_id=user_id).order_by(HistoryItem.ts.desc()).limit(60).all()
    stats = get_or_create_stats(user_id)
    currency = user.currency if user else DEFAULT_CURRENCY
    symbol = _CURRENCY_SYMBOLS.get(normalize_currency(currency), "")

    notes = []
    if user and user.interests:
        notes.append(f"Has declared these shopping interests in Settings: {user.interests.strip()}")

    top_cats = sorted((stats.categories or {}).items(), key=lambda kv: kv[1], reverse=True)
    if top_cats:
        notes.append(f"Shops most often in: {top_cats[0][0]}" + (f" and {top_cats[1][0]}" if len(top_cats) > 1 else ""))

    amounts = [a for a in (_parse_amount(w.price) for w in wishlist) if a]
    if amounts:
        ceiling = max(amounts)
        notes.append(f"Typical budget ceiling seen on their wishlist: ~{symbol}{ceiling:,.0f}")

    corpus = " ".join([w.name for w in wishlist] + [h.text for h in history]).lower()
    brand_hits = [(b, corpus.count(b.lower())) for b in _BRAND_KEYWORDS]
    brand_hits = [b for b in brand_hits if b[1] > 0]
    brand_hits.sort(key=lambda x: x[1], reverse=True)
    if brand_hits:
        notes.append(f"Has shown a preference for: {', '.join(b[0] for b in brand_hits[:3])}")

    if not notes:
        notes.append("No strong shopping pattern yet — this account is still new.")

    return {
        "notes": notes,
        "top_category": top_cats[0][0] if top_cats else None,
        "budget_ceiling": max(amounts) if amounts else None,
        "brands": [b[0] for b in brand_hits[:5]],
    }




def _contents_to_messages(contents, system_instruction=None):
    """Convert internal-style `contents` ([{role, parts:[{text}|{inline_data}]}])
    into OpenAI/Groq-style `messages` ([{role, content}])."""
    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})

    for item in contents:
        role = item.get("role", "user")
        role = "assistant" if role == "model" else role
        parts = item.get("parts", [])

        # Plain text-only message -> simple string content (cheaper/simpler).
        if all("text" in p for p in parts):
            messages.append({"role": role, "content": "".join(p["text"] for p in parts)})
            continue

        # Mixed text/image message -> OpenAI-style content blocks.
        content_blocks = []
        for p in parts:
            if "text" in p:
                content_blocks.append({"type": "text", "text": p["text"]})
            elif "inline_data" in p:
                mime = p["inline_data"].get("mime_type", "image/jpeg")
                data = p["inline_data"].get("data", "")
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data}"},
                })
        messages.append({"role": role, "content": content_blocks})
    return messages


def _groq_request(payload, stream=False):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured on the server.")
    return requests.post(
        GROQ_CHAT_URL,
        json=payload,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        timeout=60,
        stream=stream,
    )


def lens_generate(contents, system_instruction=None, response_mime_type=None, temperature=0.7, model=None):
    """Single-shot (non-streaming) call powering all of Prism's structured JSON and
    chat replies. Runs on Groq under the hood."""
    messages = _contents_to_messages(contents, system_instruction)
    payload = {"model": model or GROQ_TEXT_MODEL, "messages": messages, "temperature": temperature}
    if response_mime_type == "application/json":
        payload["response_format"] = {"type": "json_object"}

    resp = _groq_request(payload)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(data.get("error", {}).get("message", "Groq request failed."))
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        raise RuntimeError("Groq returned an unexpected response shape.")


def lens_generate_json(contents, system_instruction=None, temperature=0.6, model=None):
    text = lens_generate(contents, system_instruction=system_instruction,
                            response_mime_type="application/json", temperature=temperature, model=model)
    return json.loads(text)


def lens_stream(contents, system_instruction=None, temperature=0.7, model=None):
    messages = _contents_to_messages(contents, system_instruction)
    payload = {"model": model or GROQ_TEXT_MODEL, "messages": messages, "temperature": temperature, "stream": True}

    resp = _groq_request(payload, stream=True)
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {}).get("message", "Groq request failed.")
        except Exception:
            err = "Groq request failed."
        yield f"event: error\ndata: {json.dumps({'error': err})}\n\n"
        return

    # Force UTF-8 decoding of the raw bytes ourselves. `resp.iter_lines(decode_unicode=True)`
    # lets `requests` guess the encoding from the Content-Type header, and when Groq's
    # SSE response doesn't declare a charset, requests falls back to Latin-1 — which
    # mangles any multi-byte UTF-8 character (emoji, curly quotes, accented letters)
    # into garbled symbols like "ð" or "â". Decoding explicitly as UTF-8 fixes that.
    for raw_line in resp.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace")
        if not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if raw == "[DONE]":
            break
        try:
            chunk = json.loads(raw)
            text = chunk["choices"][0]["delta"].get("content", "")
        except (KeyError, IndexError, json.JSONDecodeError):
            continue
        if text:
            yield f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"
    yield "event: done\ndata: {}\n\n"


# ---------------------------------------------------------------------------
# Marketplace links (deterministic — built here, never trusted to the AI,
# so the app never hallucinates a store name or a broken URL)
# ---------------------------------------------------------------------------
def marketplace_links(name):
    q = quote_plus(name or "")
    if not q:
        return []
    return [
        {"name": "Jumia", "icon": "🛍️", "url": f"https://www.jumia.com.ng/catalog/?q={q}"},
        {"name": "Konga", "icon": "🛒", "url": f"https://www.konga.com/search?search={q}"},
        {"name": "Amazon", "icon": "📦", "url": f"https://www.amazon.com/s?k={q}"},
        {"name": "AliExpress", "icon": "🌐", "url": f"https://www.aliexpress.com/wholesale?SearchText={q}"},
        {"name": "eBay", "icon": "🏷️", "url": f"https://www.ebay.com/sch/i.html?_nkw={q}"},
        {"name": "Temu", "icon": "✨", "url": f"https://www.temu.com/search_result.html?search_key={q}"},
    ]


def enrich_picks(payload, key="picks", name_field="name"):
    """Attach real marketplace links to every pick in a list response, without
    ever letting the model itself invent a URL. (The product-photo gallery
    feature has been removed — the underlying image providers weren't
    reliably returning real photos, so the app no longer promises pictures
    it can't consistently deliver. Verified marketplace links remain the
    trustworthy way for a shopper to see and confirm the actual item.)"""
    for p in (payload.get(key) or []):
        nm = (p.get(name_field) or "").strip()
        if nm:
            p["marketplace_links"] = marketplace_links(nm)
    return payload


def enrich_single(payload, name_field="product"):
    """Attach marketplace links for single-product responses (reviews,
    price-history, vision scan)."""
    nm = (payload.get(name_field) or "").strip()
    if nm:
        payload["marketplace_links"] = marketplace_links(nm)
    return payload



# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    profile_picture = db.Column(db.String(500), nullable=True)
    interests = db.Column(db.String(500), nullable=True)
    verification_code = db.Column(db.String(12), nullable=True)
    is_verified = db.Column(db.Boolean, default=False, nullable=False)
    reset_token = db.Column(db.String(128), nullable=True, index=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    country = db.Column(db.String(80), nullable=False, default=DEFAULT_COUNTRY, server_default=DEFAULT_COUNTRY)
    currency = db.Column(db.String(8), nullable=False, default=DEFAULT_CURRENCY, server_default=DEFAULT_CURRENCY)
    failed_login_attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    lockout_until = db.Column(db.DateTime, nullable=True)
    # --- New-device / new-IP login verification ---
    device_verification_code = db.Column(db.String(12), nullable=True)
    device_verification_expiry = db.Column(db.DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Persisted dashboard data — every search, wishlist save, price alert,
# activity entry, achievement and stat used to live only in the browser's
# localStorage (gone the moment cache was cleared / a new device was used).
# These models move all of that into the database, keyed to the account,
# so the dashboard is the same everywhere the shopper logs in.
# ---------------------------------------------------------------------------
class WishlistItem(db.Model):
    id = db.Column(db.String(40), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    price = db.Column(db.String(120), default="—")
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "price": self.price,
                "addedAt": int(self.added_at.timestamp() * 1000)}


class AlertItem(db.Model):
    id = db.Column(db.String(40), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    product = db.Column(db.String(255), nullable=False)
    price = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "product": self.product, "price": self.price,
                "createdAt": int(self.created_at.timestamp() * 1000)}


class HistoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    type = db.Column(db.String(40), nullable=False)
    text = db.Column(db.String(500), nullable=False)
    ts = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {"id": self.id, "type": self.type, "text": self.text,
                "ts": int(self.ts.timestamp() * 1000)}


class Achievement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    unlocked_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("user_id", "name", name="uq_user_achievement"),)


class UserStats(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    saved = db.Column(db.Integer, default=0)
    searches = db.Column(db.Integer, default=0)
    accepted = db.Column(db.Integer, default=0)
    categories_json = db.Column(db.Text, default="{}")

    @property
    def categories(self):
        try:
            return json.loads(self.categories_json or "{}")
        except Exception:
            return {}

    @categories.setter
    def categories(self, value):
        self.categories_json = json.dumps(value or {})


class LoginEvent(db.Model):
    """One row per login attempt (success or failure) — powers 'logins per
    day' and active-user counts in the admin panel, and is what the
    brute-force lockout logic checks against."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    success = db.Column(db.Boolean, nullable=False, default=False)
    ip = db.Column(db.String(64), nullable=True)
    ts = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class FeatureEvent(db.Model):
    """One row per meaningful in-app action (switching to a dashboard view,
    running a Copilot tool, etc.) — powers 'most used feature' and 'where
    people are before they leave' in the admin panel. Deliberately coarse
    (a view/tool name, not full click tracking) — enough to see usage
    patterns without turning this into a surveillance log."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    feature = db.Column(db.String(120), nullable=False, index=True)
    ts = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {"saved": self.saved or 0, "searches": self.searches or 0,
                "accepted": self.accepted or 0, "categories": self.categories}


class LivePriceCache(db.Model):
    """Cached Google-Shopping results for a normalized product query, so
    identical searches across users don't each cost a fresh paid SerpApi
    call. This is a brand-new table, so plain db.create_all() below picks
    it up automatically — no manual ALTER TABLE migration needed."""
    id = db.Column(db.Integer, primary_key=True)
    query_key = db.Column(db.String(300), nullable=False, unique=True, index=True)
    results_json = db.Column(db.Text, nullable=False)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


# ---------------------------------------------------------------------------
# Marketplace — lets any shopper open their own tiny storefront inside
# Prism: a business-card listing in the "Browse" tab that links out to a
# real, shareable mini-site (/store/<slug>) built from a design they pick.
# Buyers can't pay in-app yet (no payment rails), so the whole flow ends at
# "message the seller" — a DM-style inquiry the seller sees in their own
# Marketplace tab.
# ---------------------------------------------------------------------------
SHOP_DESIGNS = {"aurora", "sunset", "mono", "forest", "citrus", "volt"}
DEFAULT_SHOP_DESIGN = "aurora"

# "Template" = the layout/structure of the storefront (grid vs list vs big
# gallery cards). "Design" (above) = the color theme. Picking both is what
# the signup wizard's step 3 (template) and step 4 (design) map to.
SHOP_TEMPLATES = {"grid", "catalog", "boutique", "spotlight"}
DEFAULT_SHOP_TEMPLATE = "grid"

# A product's stock badge, settable from the dashboard without deleting it.
STOCK_STATUSES = {"in_stock", "limited", "sold_out"}
DEFAULT_STOCK_STATUS = "in_stock"


class Shop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    slug = db.Column(db.String(160), unique=True, nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(120), nullable=True)
    description = db.Column(db.Text, nullable=True)
    # Legacy free-text contact (WhatsApp/Instagram/email). No longer required
    # or shown as the primary way to reach a seller — buyers message through
    # the in-app chat instead — but kept as an optional extra line sellers
    # can still fill in if they want to.
    contact = db.Column(db.String(255), nullable=True)
    design = db.Column(db.String(30), nullable=False, default=DEFAULT_SHOP_DESIGN, server_default=DEFAULT_SHOP_DESIGN)
    template = db.Column(db.String(30), nullable=False, default=DEFAULT_SHOP_TEMPLATE, server_default=DEFAULT_SHOP_TEMPLATE)
    cover_image = db.Column(db.String(500), nullable=True)
    logo_image = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self, product_count=0):
        return {
            "slug": self.slug,
            "name": self.name,
            "category": self.category or "",
            "description": self.description or "",
            "contact": self.contact or "",
            "design": self.design if self.design in SHOP_DESIGNS else DEFAULT_SHOP_DESIGN,
            "template": self.template if self.template in SHOP_TEMPLATES else DEFAULT_SHOP_TEMPLATE,
            "coverImageUrl": stored_image_url(self.cover_image),
            "logoImageUrl": stored_image_url(self.logo_image),
            "productCount": product_count,
            "storeUrl": url_for("view_store", slug=self.slug),
        }


class ShopProduct(db.Model):
    id = db.Column(db.String(40), primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shop.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    price = db.Column(db.String(120), nullable=False, default="—")
    description = db.Column(db.Text, nullable=True)
    photos_json = db.Column(db.Text, nullable=False, default="[]")
    stock_status = db.Column(db.String(20), nullable=False, default=DEFAULT_STOCK_STATUS, server_default=DEFAULT_STOCK_STATUS)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    @property
    def photos(self):
        try:
            return json.loads(self.photos_json or "[]")
        except Exception:
            return []

    @photos.setter
    def photos(self, value):
        self.photos_json = json.dumps(value or [])

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "price": self.price,
            "description": self.description or "",
            "photoUrls": [stored_image_url(p) for p in self.photos if stored_image_url(p)],
            "stockStatus": self.stock_status if self.stock_status in STOCK_STATUSES else DEFAULT_STOCK_STATUS,
            "createdAt": int(self.created_at.timestamp() * 1000),
        }


class ShopConversation(db.Model):
    """One running thread between a single buyer and the seller — replaces
    the old one-shot ShopInquiry 'DM'. A buyer never logs in, so they're
    recognized by a random token minted on their first message and kept in
    their browser (localStorage), not by a phone number."""
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shop.id"), nullable=False, index=True)
    product_id = db.Column(db.String(40), db.ForeignKey("shop_product.id"), nullable=True, index=True)
    product_name = db.Column(db.String(200), nullable=True)
    buyer_name = db.Column(db.String(160), nullable=False)
    buyer_token = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_message_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    seller_unread = db.Column(db.Integer, nullable=False, default=0)
    buyer_unread = db.Column(db.Integer, nullable=False, default=0)

    def to_dict(self, last_message=None):
        return {
            "id": self.id,
            "productName": self.product_name or "",
            "buyerName": self.buyer_name,
            "lastMessage": last_message.to_dict() if last_message else None,
            "lastMessageAt": int(self.last_message_at.timestamp() * 1000),
            "sellerUnread": self.seller_unread or 0,
            "buyerUnread": self.buyer_unread or 0,
        }


class ShopMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("shop_conversation.id"), nullable=False, index=True)
    sender = db.Column(db.String(10), nullable=False)  # "buyer" | "seller"
    body = db.Column(db.Text, nullable=True)
    image = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "sender": self.sender,
            "body": self.body or "",
            "imageUrl": stored_image_url(self.image) if self.image else None,
            "createdAt": int(self.created_at.timestamp() * 1000),
        }


def stored_image_url(path):
    """Build a servable /uploads/pic/<file> URL for anything saved via
    save_uploaded_profile_picture-style helpers, or None if missing/blank —
    same "never render a broken image" guard as profile_picture_url."""
    if not path:
        return None
    filename = path[len("pic/"):] if path.startswith("pic/") else path
    full_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if not os.path.isfile(full_path):
        return None
    return url_for("serve_profile_picture", filename=filename)


def slugify_shop_name(name):
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return base or "shop"


def generate_unique_shop_slug(name):
    base = slugify_shop_name(name)
    slug = base
    n = 2
    while Shop.query.filter_by(slug=slug).first():
        slug = f"{base}-{n}"
        n += 1
    return slug


def save_uploaded_shop_image(file_storage, max_bytes=MAX_PROFILE_PICTURE_BYTES):
    """Same validation as a profile picture, just saved under a distinct
    filename prefix so it's obvious in the uploads folder which feature a
    file came from. Returns (relative_path_or_None, error_message_or_None)."""
    ok, error, data, mime = validate_image_upload(file_storage, max_bytes)
    if not ok:
        return None, error
    ext = _IMAGE_EXT_BY_MIME.get(mime, "jpg")
    unique_name = f"shop_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}.{ext}"
    upload_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
    with open(upload_path, "wb") as f:
        f.write(data)
    return os.path.join("pic", unique_name).replace("\\", "/"), None


MAX_PRODUCT_PHOTOS = 5
MAX_PRODUCTS_PER_SHOP = 100

# ---------------------------------------------------------------------------
# "Buy from us first": before Prism sends a shopper to an AI price estimate
# or an external retailer link, check whether one of our own sellers
# (Shop/ShopProduct) already lists the thing they're searching for. If so,
# that listing should win — it's a real, in-app seller, not a guess.
# ---------------------------------------------------------------------------
_SEARCH_STOPWORDS = {
    "a", "an", "the", "for", "to", "of", "in", "on", "with", "and", "or",
    "best", "good", "cheap", "new", "used", "buy", "get", "need", "want",
    "some", "any", "my", "me", "please", "cheapest", "affordable",
}


def _search_tokens(text):
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) >= 3} - _SEARCH_STOPWORDS


def find_internal_matches(query, limit=3, min_score=0.4):
    """Look through every seller's storefront on Prism for a product whose
    name overlaps with what the shopper searched. Returns the best matches
    (highest token-overlap first) as plain dicts ready for the API response,
    or [] if nothing on the platform is a good enough match — a partial,
    noisy match (e.g. one short word in common) is worse than no match, so
    results below min_score are dropped rather than shown."""
    q_tokens = _search_tokens(query)
    if not q_tokens:
        return []
    rows = db.session.query(ShopProduct, Shop).join(Shop, ShopProduct.shop_id == Shop.id).all()
    scored = []
    for product, shop in rows:
        overlap = q_tokens & _search_tokens(product.name)
        if not overlap:
            continue
        score = len(overlap) / len(q_tokens)
        if score >= min_score:
            scored.append((score, product, shop))
    scored.sort(key=lambda row: row[0], reverse=True)

    matches = []
    for _, product, shop in scored[:limit]:
        photos = product.photos
        matches.append({
            "productId": product.id,
            "name": product.name,
            "price": product.price,
            "photoUrl": stored_image_url(photos[0]) if photos else None,
            "shopName": shop.name,
            "shopSlug": shop.slug,
            "storeUrl": url_for("view_store", slug=shop.slug),
        })
    return matches



def _normalize_price_query(product):
    return re.sub(r"\s+", " ", (product or "").strip().lower())


def _fetch_serpapi_shopping(product):
    """Calls SerpApi's Google Shopping engine. Returns a list of
    {store, price_usd, link, thumbnail} dicts, one best (cheapest) offer
    per trusted retailer — never more than one row per store, and only
    from LIVE_PRICE_RETAILERS. `thumbnail` is a real product photo from
    that listing, included at no extra cost since it's already part of
    the Shopping response we're paying for anyway. Raises on any
    request/parsing failure so the caller can fall back to cache or a
    clear error."""
    if not SERPAPI_KEY:
        raise RuntimeError("SERPAPI_KEY is not configured on the server.")
    resp = requests.get(SERPAPI_URL, params={
        "engine": "google_shopping",
        "q": product,
        "api_key": SERPAPI_KEY,
        "gl": "us",
        "hl": "en",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    best_per_store = {}
    for item in (data.get("shopping_results") or []):
        source = (item.get("source") or "").strip()
        matched = next((r for r in LIVE_PRICE_RETAILERS if r.lower() in source.lower()), None)
        price_usd = item.get("extracted_price")
        if not matched or price_usd is None:
            continue
        price_usd = float(price_usd)
        if matched not in best_per_store or price_usd < best_per_store[matched]["price_usd"]:
            best_per_store[matched] = {
                "store": matched,
                "price_usd": price_usd,
                "link": item.get("product_link") or item.get("link"),
                "thumbnail": item.get("thumbnail"),
            }
    return list(best_per_store.values())


def get_live_prices(product, currency):
    """Real prices for `product` from Amazon/Walmart/Best Buy/Target/eBay,
    converted into the account's currency. Returns (results, error) — on
    any failure, falls back to a stale cached copy if one exists rather
    than showing nothing. results is always a plain list (possibly empty),
    never None."""
    key = _normalize_price_query(product)
    if not key:
        return [], "No product given."

    cached = LivePriceCache.query.filter_by(query_key=key).first()
    fresh = cached and (datetime.utcnow() - cached.fetched_at).total_seconds() < LIVE_PRICE_CACHE_TTL_SECONDS

    if fresh:
        raw = json.loads(cached.results_json)
    else:
        try:
            raw = _fetch_serpapi_shopping(product)
        except Exception as exc:
            app.logger.warning(f"Live price lookup failed for '{product}': {exc}")
            if cached:
                raw = json.loads(cached.results_json)  # serve stale rather than nothing
            else:
                return [], "Live prices are temporarily unavailable — try again shortly."
        else:
            if cached:
                cached.results_json = json.dumps(raw)
                cached.fetched_at = datetime.utcnow()
            else:
                db.session.add(LivePriceCache(query_key=key, results_json=json.dumps(raw)))
            db.session.commit()

    currency = normalize_currency(currency)
    symbol = _CURRENCY_SYMBOLS.get(currency, "")
    rate = 1.0 if currency == "USD" else get_usd_rate_for(currency)
    out = [{
        "store": r["store"],
        "price": f"{symbol}{r['price_usd'] * rate:,.2f}",
        "price_value": round(r["price_usd"] * rate, 2),
        "link": r["link"],
        "thumbnail": r.get("thumbnail"),
    } for r in raw]
    out.sort(key=lambda x: x["price_value"])
    return out, None


def get_or_create_stats(user_id):
    stats = UserStats.query.get(user_id)
    if not stats:
        stats = UserStats(user_id=user_id, saved=0, searches=0, accepted=0, categories_json="{}")
        db.session.add(stats)
        db.session.commit()
    return stats


def profile_picture_url(user):
    """Build a servable URL for a user's uploaded profile picture, or None
    if they haven't set one (callers fall back to an initials avatar).
    Also guards against a stale/broken path (file missing on disk) so a
    dead reference never renders as a broken image — it just falls back
    to the initials avatar instead."""
    if not user or not user.profile_picture:
        return None
    filename = user.profile_picture
    if filename.startswith("pic/"):
        filename = filename[len("pic/"):]
    full_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if not os.path.isfile(full_path):
        return None
    return url_for("serve_profile_picture", filename=filename)


with app.app_context():
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    db.create_all()

    # db.create_all() only creates tables that don't exist yet — it never adds
    # a new column to a table that's already there. Since `country`/`currency`/
    # `failed_login_attempts`/`lockout_until` were added to an existing `user`
    # table, patch them in directly for any database created before this
    # change. Each column is added independently, in its own transaction, so
    # one already-existing column can never block the others.
    _new_user_columns = {
        "country": f"VARCHAR(80) DEFAULT '{DEFAULT_COUNTRY}'",
        "currency": f"VARCHAR(8) DEFAULT '{DEFAULT_CURRENCY}'",
        "failed_login_attempts": "INTEGER DEFAULT 0",
        "lockout_until": "TIMESTAMP",
        "device_verification_code": "VARCHAR(12)",
        "device_verification_expiry": "TIMESTAMP",
    }
    from sqlalchemy import text as _sqltext
    is_sqlite = db.engine.dialect.name == "sqlite"
    for _col, _decl in _new_user_columns.items():
        try:
            with db.engine.connect() as _conn:
                if is_sqlite:
                    existing = {row[1] for row in _conn.execute(_sqltext("PRAGMA table_info(user)"))}
                    if _col in existing:
                        continue
                    _conn.execute(_sqltext(f"ALTER TABLE \"user\" ADD COLUMN {_col} {_decl}"))
                else:
                    # IF NOT EXISTS makes this safe to re-run on every deploy,
                    # regardless of whether a previous run already added it.
                    _conn.execute(_sqltext(f"ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS {_col} {_decl}"))
                _conn.commit()
        except Exception as _mig_exc:
            app.logger.warning(f"Skipped migration for user.{_col}: {_mig_exc}")

    # Same story for `shop` and `shop_product` — logo_image/template were
    # added alongside cover_image/design, and stock_status was added after
    # products already existed in the wild.
    _new_shop_columns = {
        "logo_image": "VARCHAR(500)",
        "template": f"VARCHAR(30) DEFAULT '{DEFAULT_SHOP_TEMPLATE}'",
    }
    for _table, _cols in (("shop", _new_shop_columns), ("shop_product", {"stock_status": f"VARCHAR(20) DEFAULT '{DEFAULT_STOCK_STATUS}'"})):
        for _col, _decl in _cols.items():
            try:
                with db.engine.connect() as _conn:
                    if is_sqlite:
                        existing = {row[1] for row in _conn.execute(_sqltext(f"PRAGMA table_info({_table})"))}
                        if _col in existing:
                            continue
                        _conn.execute(_sqltext(f"ALTER TABLE \"{_table}\" ADD COLUMN {_col} {_decl}"))
                    else:
                        _conn.execute(_sqltext(f"ALTER TABLE \"{_table}\" ADD COLUMN IF NOT EXISTS {_col} {_decl}"))
                    _conn.commit()
            except Exception as _mig_exc:
                app.logger.warning(f"Skipped migration for {_table}.{_col}: {_mig_exc}")

    # Add demo account if it doesn't exist
    demo_email = "demo@gmail.com"
    if not User.query.filter_by(email=demo_email).first():
        demo_user = User(
            full_name="DemoUser",
            email=demo_email,
            password_hash=generate_password_hash("demo12345"),
            profile_picture=None,
            interests="Electronics, Tech, Gadgets",
            is_verified=True,
        )
        db.session.add(demo_user)
        db.session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        # Guards against stale session cookies pointing at a user_id that no
        # longer exists (e.g. after the database was reset/recreated) —
        # without this, every view below would 500 on `user.whatever`.
        if not User.query.get(session["user_id"]):
            session.clear()
            flash("Your session has expired. Please log in again.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user:
            session.clear()
            return redirect(url_for("login"))
        if not is_admin_email(user.email):
            # Deliberately vague — don't confirm/deny that an admin panel
            # exists to non-admin accounts poking at the URL.
            flash("That page doesn't exist.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped


def generate_verification_code():
    return f"{random.randint(0, 999999):06d}"


def generate_verification_code():
    return f"{random.randint(0, 999999):06d}"


def send_verification_email(user):
    """Send a verification email through Brevo's transactional email API."""
    code = generate_verification_code()
    user.verification_code = code
    db.session.commit()

    html_content = f"""
    <div style="background:#05070C;padding:48px 24px;font-family:'Inter',Arial,sans-serif;">
      <div style="max-width:480px;margin:0 auto;background:#0A1024;border:1px solid rgba(255,255,255,.09);
                  border-radius:18px;padding:40px;">
        <h1 style="font-family:'Space Grotesk',Arial,sans-serif;color:#F2F5FF;font-size:28px;margin:0 0 4px;">
          PR<span style="color:#4CE0FF;">ISM</span>
        </h1>
        <p style="color:#8B93AE;font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 28px;">
          Verify your email
        </p>
        <p style="color:#F2F5FF;font-size:15px;line-height:1.6;">
          Hi {user.full_name}, use this code to verify your email address and activate your Prism account.
        </p>
        <div style="display:inline-block;margin-top:24px;padding:16px 28px;border-radius:100px;
                    background:linear-gradient(90deg,#4CE0FF,#9D6BFF);color:#05070C;font-weight:700;
                    letter-spacing:0.25em;font-size:24px;">
          {code}
        </div>
        <p style="color:#8B93AE;font-size:12px;margin-top:28px;line-height:1.6;">
          This code expires in 24 hours. If you didn't create a Prism account, you can safely ignore this email.
        </p>
      </div>
    </div>
    """

    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": user.email, "name": user.full_name}],
        "subject": "Verify your Prism account",
        "htmlContent": html_content,
    }
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY or "",
        "content-type": "application/json",
    }

    print(f"\n{'='*60}")
    print(f"📧 ATTEMPTING TO SEND VERIFICATION EMAIL")
    print(f"{'='*60}")
    print(f"To: {user.email}")
    print(f"Name: {user.full_name}")
    print(f"Code: {code}")
    print(f"Sender Email: {BREVO_SENDER_EMAIL}")
    print(f"API Key present: {bool(BREVO_API_KEY)}")
    print(f"API Key length: {len(BREVO_API_KEY) if BREVO_API_KEY else 0}")
    print(f"{'='*60}\n")

    if not BREVO_API_KEY:
        print("❌ BREVO_API_KEY is NOT SET - Email will not be sent")
        print(f"   Check your .env file")
        app.logger.warning(
            "BREVO_API_KEY is not set — skipping real send. Verification code: %s", code
        )
        return False

    try:
        print("🔄 Sending POST request to Brevo API...")
        print(f"   URL: {BREVO_API_URL}")
        
        response = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=10)
        
        print(f"   Status Code: {response.status_code}")
        print(f"   Response Headers: {dict(response.headers)}")
        print(f"   Response Body: {response.text}")
        
        if response.status_code in (200, 201):
            print(f"✅ EMAIL SENT SUCCESSFULLY!")
            return True
        else:
            print(f"❌ EMAIL SEND FAILED - Status {response.status_code}")
            try:
                error_data = response.json()
                print(f"   Error details: {json.dumps(error_data, indent=2)}")
            except:
                pass
            return False
            
    except requests.RequestException as exc:
        print(f"❌ REQUEST EXCEPTION: {exc}")
        app.logger.error("Brevo email send failed: %s", exc)
        return False


def send_reset_email(user, token):
    """Email a signed, time-limited password reset link through Brevo —
    same transactional pipeline used for verification emails."""
    reset_link = url_for("reset_password", token=token, _external=True)

    html_content = f"""
    <div style="background:#05070C;padding:48px 24px;font-family:'Inter',Arial,sans-serif;">
      <div style="max-width:480px;margin:0 auto;background:#0A1024;border:1px solid rgba(255,255,255,.09);
                  border-radius:18px;padding:40px;">
        <h1 style="font-family:'Space Grotesk',Arial,sans-serif;color:#F2F5FF;font-size:28px;margin:0 0 4px;">
          PR<span style="color:#4CE0FF;">ISM</span>
        </h1>
        <p style="color:#8B93AE;font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 28px;">
          Reset your password
        </p>
        <p style="color:#F2F5FF;font-size:15px;line-height:1.6;">
          Hi {user.full_name}, we received a request to reset the password on your Prism account.
          Click the button below to choose a new one. This link expires in 1 hour.
        </p>
        <a href="{reset_link}"
           style="display:inline-block;margin-top:24px;padding:16px 28px;border-radius:100px;
                  background:linear-gradient(90deg,#4CE0FF,#9D6BFF);color:#05070C;font-weight:700;
                  letter-spacing:.08em;text-transform:uppercase;font-size:13px;text-decoration:none;
                  font-family:'JetBrains Mono',monospace;">
          Reset Password
        </a>
        <p style="color:#8B93AE;font-size:12px;margin-top:28px;line-height:1.6;word-break:break-all;">
          Or paste this link into your browser:<br>{reset_link}
        </p>
        <p style="color:#8B93AE;font-size:12px;margin-top:20px;line-height:1.6;">
          If you didn't request this, you can safely ignore this email — your password will not change.
        </p>
      </div>
    </div>
    """

    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": user.email, "name": user.full_name}],
        "subject": "Reset your Prism password",
        "htmlContent": html_content,
    }
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY or "",
        "content-type": "application/json",
    }

    if not BREVO_API_KEY:
        app.logger.warning(
            "BREVO_API_KEY is not set — skipping real send. Reset link: %s", reset_link
        )
        return False

    try:
        response = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            return True
        app.logger.warning(
            "Brevo reset email HTTP %s: %s", response.status_code, response.text[:500]
        )
        return False
    except requests.RequestException as exc:
        app.logger.error("Brevo reset email send failed: %s", exc)
        return False


def send_device_verification_email(user, code, ip):
    """Sent when a login is attempted from an IP address we haven't seen a
    successful login from before for this account. Same Brevo pipeline as
    the other transactional emails."""
    html_content = f"""
    <div style="background:#05070C;padding:48px 24px;font-family:'Inter',Arial,sans-serif;">
      <div style="max-width:480px;margin:0 auto;background:#0A1024;border:1px solid rgba(255,255,255,.09);
                  border-radius:18px;padding:40px;">
        <h1 style="font-family:'Space Grotesk',Arial,sans-serif;color:#F2F5FF;font-size:28px;margin:0 0 4px;">
          PR<span style="color:#4CE0FF;">ISM</span>
        </h1>
        <p style="color:#8B93AE;font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 28px;">
          New sign-in detected
        </p>
        <p style="color:#F2F5FF;font-size:15px;line-height:1.6;">
          Hi {user.full_name}, someone just tried to log in to your Prism account from a device
          or network we don't recognize (IP: {ip or 'unknown'}). Enter the code below to
          confirm it's you and finish signing in.
        </p>
        <div style="display:inline-block;margin-top:24px;padding:16px 28px;border-radius:100px;
                    background:linear-gradient(90deg,#4CE0FF,#9D6BFF);color:#05070C;font-weight:700;
                    letter-spacing:0.25em;font-size:24px;">
          {code}
        </div>
        <p style="color:#8B93AE;font-size:12px;margin-top:28px;line-height:1.6;">
          This code expires in {DEVICE_CODE_EXPIRY_MINUTES} minutes. If this wasn't you, don't share
          this code with anyone — change your password immediately from a trusted device.
        </p>
      </div>
    </div>
    """

    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": user.email, "name": user.full_name}],
        "subject": "Confirm it's you — new Prism sign-in",
        "htmlContent": html_content,
    }
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY or "",
        "content-type": "application/json",
    }

    if not BREVO_API_KEY:
        app.logger.warning(
            "BREVO_API_KEY is not set — skipping real send. Device verification code: %s", code
        )
        return False

    try:
        response = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            return True
        app.logger.warning(
            "Brevo device-verification email HTTP %s: %s", response.status_code, response.text[:500]
        )
        return False
    except requests.RequestException as exc:
        app.logger.error("Brevo device-verification email send failed: %s", exc)
        return False


def is_known_ip_for_user(user_id, ip):
    """True if this account has a prior *successful* login recorded from
    this exact IP address — i.e. this is not a new device/network."""
    if not ip:
        return False
    return db.session.query(
        LoginEvent.query.filter_by(user_id=user_id, success=True, ip=ip).exists()
    ).scalar()


def _describe_user_agent(ua_string):
    """Human-readable device string built straight from the raw User-Agent
    header. We intentionally don't rely on werkzeug's UA parser (its
    accuracy/availability varies by version) — the raw string is always
    good enough for a security notice."""
    ua_string = (ua_string or "").strip()
    if not ua_string:
        return "an unknown device"
    return ua_string if len(ua_string) <= 120 else ua_string[:117] + "..."


def _format_login_time(dt):
    """e.g. 'July 15, 2026 at 3:42 PM UTC'"""
    return dt.strftime("%B %-d, %Y at %-I:%M %p UTC")


def send_login_notification_email(user, ip, ua_string, when):
    """Sent after every completed login so the account owner has a record of
    when/where/what device signed in — separate from the new-device
    verification code email, so it isn't sent twice in the same flow."""
    device_desc = _describe_user_agent(ua_string)
    when_str = _format_login_time(when)

    html_content = f"""
    <div style="background:#05070C;padding:48px 24px;font-family:'Inter',Arial,sans-serif;">
      <div style="max-width:480px;margin:0 auto;background:#0A1024;border:1px solid rgba(255,255,255,.09);
                  border-radius:18px;padding:40px;">
        <h1 style="font-family:'Space Grotesk',Arial,sans-serif;color:#F2F5FF;font-size:28px;margin:0 0 4px;">
          PR<span style="color:#4CE0FF;">ISM</span>
        </h1>
        <p style="color:#8B93AE;font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 28px;">
          New login to your account
        </p>
        <p style="color:#F2F5FF;font-size:15px;line-height:1.6;">
          Hi {user.full_name}, your Prism account was just signed in to from
          <b>{device_desc}</b> at <b>{when_str}</b> (IP: {ip or 'unknown'}).
        </p>
        <p style="color:#8B93AE;font-size:13px;margin-top:20px;line-height:1.6;">
          If this was you, no action is needed. If you don't recognize this
          activity, change your password right away and contact support.
        </p>
      </div>
    </div>
    """

    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": user.email, "name": user.full_name}],
        "subject": "New login to your Prism account",
        "htmlContent": html_content,
    }
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY or "",
        "content-type": "application/json",
    }

    if not BREVO_API_KEY:
        app.logger.warning(
            "BREVO_API_KEY is not set — skipping real send. Login notification for %s from %s at %s",
            user.email, ip, when_str,
        )
        return False

    try:
        response = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            return True
        app.logger.warning(
            "Brevo login-notification email HTTP %s: %s", response.status_code, response.text[:500]
        )
        return False
    except requests.RequestException as exc:
        app.logger.error("Brevo login-notification email send failed: %s", exc)
        return False


CONTACT_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_contact_message_email(name, email, message):
    """Forward a public contact-form submission (landing page / legal pages)
    to the Prism inbox through Brevo. Reply-To is set to the visitor's own
    address so replying from the inbox goes straight back to them."""
    safe_message = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    safe_name = (name or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html_content = f"""
    <div style="background:#05070C;padding:48px 24px;font-family:'Inter',Arial,sans-serif;">
      <div style="max-width:520px;margin:0 auto;background:#0A1024;border:1px solid rgba(255,255,255,.09);
                  border-radius:18px;padding:40px;">
        <h1 style="font-family:'Space Grotesk',Arial,sans-serif;color:#F2F5FF;font-size:26px;margin:0 0 4px;">
          PR<span style="color:#4CE0FF;">ISM</span>
        </h1>
        <p style="color:#8B93AE;font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 28px;">
          New contact message
        </p>
        <p style="color:#F2F5FF;font-size:14px;line-height:1.6;margin:0 0 4px;">
          <strong>{safe_name}</strong> &lt;{email}&gt;
        </p>
        <div style="margin-top:18px;padding:18px 20px;background:rgba(255,255,255,.04);
                    border:1px solid rgba(255,255,255,.09);border-radius:12px;color:#F2F5FF;
                    font-size:14px;line-height:1.7;">
          {safe_message}
        </div>
        <p style="color:#8B93AE;font-size:12px;margin-top:24px;line-height:1.6;">
          Reply directly to this email to respond to {safe_name}.
        </p>
      </div>
    </div>
    """

    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": next(iter(ADMIN_EMAILS))}],
        "replyTo": {"email": email, "name": name or email},
        "subject": f"Prism contact form — {name or email}",
        "htmlContent": html_content,
    }
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY or "",
        "content-type": "application/json",
    }

    if not BREVO_API_KEY:
        app.logger.warning("BREVO_API_KEY is not set — skipping contact email from %s", email)
        return False

    try:
        response = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            return True
        app.logger.warning("Brevo contact email HTTP %s: %s", response.status_code, response.text[:500])
        return False
    except requests.RequestException as exc:
        app.logger.error("Brevo contact email send failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Serve the Prism cinematic landing page."""
    return render_template("index.html")


LEGAL_LAST_UPDATED = "July 14, 2026"


@app.route("/terms")
def terms():
    return render_template("terms.html", legal_updated=LEGAL_LAST_UPDATED)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", legal_updated=LEGAL_LAST_UPDATED)


@app.route("/api/contact", methods=["POST"])
@limiter.limit("5 per hour")
def api_contact():
    """Public contact form — embedded on the landing page, Terms, and
    Privacy pages. Forwards the message to the Prism inbox via Brevo."""
    body = request.get_json(force=True, silent=True) or request.form or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    message = (body.get("message") or "").strip()

    if not name or not email or not message:
        return jsonify({"error": "Fill in your name, email, and message."}), 400
    if len(name) > 120 or len(email) > 180:
        return jsonify({"error": "That name or email looks too long."}), 400
    if not CONTACT_EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(message) < 5:
        return jsonify({"error": "Your message is a bit too short."}), 400
    if len(message) > 4000:
        return jsonify({"error": "Keep your message under 4000 characters."}), 400

    sent = send_contact_message_email(name, email, message)
    if not sent:
        return jsonify({
            "error": "We couldn't send that right now — please try again shortly, "
                     "or email us directly at muhammedtesleemolatundun@gmail.com."
        }), 502
    return jsonify({"success": True})


@app.route("/signup", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def signup():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        interests = request.form.get("interest", "").strip()
        agreed_terms = request.form.get("agree_terms") == "on"

        error = None
        if not full_name or not email or not password:
            error = "Fill in every field to continue."
        elif password != confirm_password:
            error = "Passwords don't match."
        elif len(password) < 8:
            error = "Use at least 8 characters for your password."
        elif not agreed_terms:
            error = "You must agree to the Terms of Service and Privacy Policy to create an account."
        elif User.query.filter_by(email=email).first():
            error = "An account with this email already exists."

        if error:
            flash(error, "error")
            return render_template("signup.html", full_name=full_name, email=email, interest=interests)

        uploaded_file = request.files.get("profile_picture")
        saved_path = None
        if uploaded_file and uploaded_file.filename:
            saved_path, upload_error = save_uploaded_profile_picture(uploaded_file)
            if upload_error:
                flash(upload_error, "error")
                return render_template("signup.html", full_name=full_name, email=email, interest=interests)

        user = User(
            full_name=full_name,
            email=email,
            password_hash=generate_password_hash(password),
            profile_picture=saved_path,
            interests=interests or None,
        )
        db.session.add(user)
        db.session.commit()

        send_verification_email(user)
        return redirect(url_for("verify_sent", email=email))

    return render_template("signup.html")


@app.route("/verify-sent")
def verify_sent():
    email = request.args.get("email", "")
    return render_template("verify_sent.html", email=email)


@app.route("/verify-email", methods=["GET", "POST"])
@limiter.limit("15 per hour")
def verify_email():
    email = request.args.get("email", "") or request.form.get("email", "")
    email = email.strip().lower()
    
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        
        if not email:
            flash("Email address is required.", "error")
            return render_template("verify_result.html", status="invalid", email=email)
        
        user = User.query.filter_by(email=email).first()

        if not user:
            flash("We couldn't find that account.", "error")
            return render_template("verify_result.html", status="invalid", email=email)

        if user.is_verified:
            return render_template("verify_result.html", status="success", email=email)

        if not code:
            flash("Please enter the verification code.", "error")
            return render_template("verify_result.html", status="invalid", email=email)

        if user.verification_code and user.verification_code == code:
            user.is_verified = True
            user.verification_code = None
            db.session.commit()
            return render_template("verify_result.html", status="success", email=email)

        flash(f"That code is incorrect. Please try again.", "error")
        return render_template("verify_result.html", status="invalid", email=email)

    if email:
        return render_template("verify_result.html", status="pending", email=email)
    return render_template("verify_result.html", status="pending")


@app.route("/resend-verification", methods=["POST"])
@limiter.limit("6 per hour")
def resend_verification():
    email = request.form.get("email", "").strip().lower()
    user = User.query.filter_by(email=email).first()
    # Always show the same message, whether or not the account exists,
    # so the form can't be used to probe which emails are registered.
    if user and not user.is_verified:
        send_verification_email(user)
    flash("If that account exists, a new verification code is on its way.", "info")
    return redirect(url_for("verify_email", email=email))


MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

# --- New-device / new-IP login verification ---------------------------------
# If a login attempt succeeds (right email + password) but comes from an IP
# we've never seen a successful login from for this account, we don't finish
# logging them in straight away — we email a one-time code to their address
# on file and require it before the session is created.
DEVICE_CODE_EXPIRY_MINUTES = 15


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("15 per minute")
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        client_ip = get_remote_address()

        if user and user.lockout_until and user.lockout_until > datetime.utcnow():
            wait_mins = max(1, int((user.lockout_until - datetime.utcnow()).total_seconds() // 60) + 1)
            flash(f"Too many failed attempts. Try again in about {wait_mins} minute(s), or reset your password.", "error")
            db.session.add(LoginEvent(user_id=user.id, email=email, success=False, ip=client_ip))
            db.session.commit()
            return render_template("login.html", email=email)

        if not user or not check_password_hash(user.password_hash, password):
            db.session.add(LoginEvent(user_id=user.id if user else None, email=email, success=False, ip=client_ip))
            if user:
                user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
                if user.failed_login_attempts >= MAX_LOGIN_ATTEMPTS:
                    user.lockout_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            db.session.commit()
            # Same message whether the email doesn't exist or the password is
            # wrong, so this can't be used to enumerate registered accounts.
            flash("Incorrect email or password.", "error")
            return render_template("login.html", email=email)

        if not user.is_verified:
            flash("Verify your email before logging in.", "error")
            return redirect(url_for("verify_sent", email=email))

        user.failed_login_attempts = 0
        user.lockout_until = None

        # New device / new IP address for this account -> hold off on
        # completing the login until they confirm a code sent to their email.
        if not is_known_ip_for_user(user.id, client_ip):
            code = generate_verification_code()
            user.device_verification_code = code
            user.device_verification_expiry = datetime.utcnow() + timedelta(minutes=DEVICE_CODE_EXPIRY_MINUTES)
            # Logged as a non-success attempt for now — it only becomes a
            # "known" successful IP once the code is confirmed below.
            db.session.add(LoginEvent(user_id=user.id, email=email, success=False, ip=client_ip))
            db.session.commit()
            send_device_verification_email(user, code, client_ip)

            session.clear()
            session["pending_2fa_user_id"] = user.id
            flash("We don't recognize this device. Enter the code we emailed you to finish logging in.", "info")
            return redirect(url_for("verify_device"))

        db.session.add(LoginEvent(user_id=user.id, email=email, success=True, ip=client_ip))
        db.session.commit()
        send_login_notification_email(
            user, client_ip, request.headers.get("User-Agent", ""), datetime.utcnow()
        )

        session.clear()
        session["user_id"] = user.id
        session["user_name"] = user.full_name
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/verify-device", methods=["GET", "POST"])
@limiter.limit("15 per hour")
def verify_device():
    """Second step of login when the attempt came from an IP address we
    haven't recorded a successful login from before for this account."""
    pending_user_id = session.get("pending_2fa_user_id")
    user = User.query.get(pending_user_id) if pending_user_id else None
    if not user:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        client_ip = get_remote_address()

        if not code:
            flash("Please enter the verification code.", "error")
            return render_template("verify_device.html", email=user.email)

        expired = (
            not user.device_verification_expiry
            or user.device_verification_expiry < datetime.utcnow()
        )
        if expired or not user.device_verification_code or user.device_verification_code != code:
            flash("That code is incorrect or has expired.", "error")
            return render_template("verify_device.html", email=user.email)

        # Correct code -> clear it and complete the login. Recording this
        # attempt as a *successful* LoginEvent for this IP means future
        # logins from the same network won't need to go through this again.
        user.device_verification_code = None
        user.device_verification_expiry = None
        user.failed_login_attempts = 0
        user.lockout_until = None
        db.session.add(LoginEvent(user_id=user.id, email=user.email, success=True, ip=client_ip))
        db.session.commit()
        send_login_notification_email(
            user, client_ip, request.headers.get("User-Agent", ""), datetime.utcnow()
        )

        session.clear()
        session["user_id"] = user.id
        session["user_name"] = user.full_name
        return redirect(url_for("dashboard"))

    return render_template("verify_device.html", email=user.email)


@app.route("/resend-device-code", methods=["POST"])
@limiter.limit("6 per hour")
def resend_device_code():
    pending_user_id = session.get("pending_2fa_user_id")
    user = User.query.get(pending_user_id) if pending_user_id else None
    if user:
        code = generate_verification_code()
        user.device_verification_code = code
        user.device_verification_expiry = datetime.utcnow() + timedelta(minutes=DEVICE_CODE_EXPIRY_MINUTES)
        db.session.commit()
        send_device_verification_email(user, code, get_remote_address())
    flash("If that login attempt is still pending, a new code is on its way.", "info")
    return redirect(url_for("verify_device"))


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("6 per hour")
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()

        # Always issue the same response whether or not the account exists,
        # so this form can't be used to probe which emails are registered.
        if user:
            token = secrets.token_urlsafe(32)
            user.reset_token = token
            user.reset_token_expiry = datetime.utcnow() + timedelta(hours=1)
            db.session.commit()
            send_reset_email(user, token)

        flash("If an account exists for that email, a reset link is on its way.", "info")
        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def reset_password(token):
    user = User.query.filter_by(reset_token=token).first()
    token_valid = bool(
        user and user.reset_token_expiry and user.reset_token_expiry > datetime.utcnow()
    )

    if not token_valid:
        flash("That reset link is invalid or has expired. Request a new one below.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        error = None
        if not password or len(password) < 8:
            error = "Use at least 8 characters for your new password."
        elif password != confirm_password:
            error = "Passwords don't match."

        if error:
            flash(error, "error")
            return render_template("reset_password.html", token=token)

        user.password_hash = generate_password_hash(password)
        user.reset_token = None
        user.reset_token_expiry = None
        user.failed_login_attempts = 0
        user.lockout_until = None
        db.session.commit()
        send_password_changed_email(user)

        flash("Password updated. Log in with your new password.", "info")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


def send_password_changed_email(user):
    """Security notification — sent whenever this account's password changes,
    so the real owner finds out immediately if someone else reset it."""
    html_content = f"""
    <div style="background:#05070C;padding:48px 24px;font-family:'Inter',Arial,sans-serif;">
      <div style="max-width:480px;margin:0 auto;background:#0A1024;border:1px solid rgba(255,255,255,.09);
                  border-radius:18px;padding:40px;">
        <h1 style="font-family:'Space Grotesk',Arial,sans-serif;color:#F2F5FF;font-size:28px;margin:0 0 4px;">
          PR<span style="color:#4CE0FF;">ISM</span>
        </h1>
        <p style="color:#8B93AE;font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 28px;">
          Security notice
        </p>
        <p style="color:#F2F5FF;font-size:15px;line-height:1.6;">
          Hi {user.full_name}, the password on your Prism account ({user.email}) was just changed.
        </p>
        <p style="color:#8B93AE;font-size:13px;margin-top:20px;line-height:1.6;">
          If this was you, no action is needed. If you didn't make this change, someone else may have
          access to your account — reset your password again immediately and consider changing the
          password on any accounts that share it.
        </p>
      </div>
    </div>
    """
    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": user.email, "name": user.full_name}],
        "subject": "Your Prism password was changed",
        "htmlContent": html_content,
    }
    headers = {"accept": "application/json", "api-key": BREVO_API_KEY or "", "content-type": "application/json"}
    if not BREVO_API_KEY:
        app.logger.warning("BREVO_API_KEY is not set — skipping password-changed notification.")
        return False
    try:
        response = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=10)
        return response.status_code in (200, 201)
    except requests.RequestException as exc:
        app.logger.error("Password-changed notification failed: %s", exc)
        return False


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_panel():
    return render_template("admin.html")


def _bucket_by_day(timestamps, days=14):
    """{'YYYY-MM-DD': count} for the last `days` days, oldest first, zero-
    filled so the chart doesn't skip days with no activity."""
    today = datetime.utcnow().date()
    buckets = {(today - timedelta(days=i)).isoformat(): 0 for i in range(days - 1, -1, -1)}
    for ts in timestamps:
        if not ts:
            continue
        key = ts.date().isoformat()
        if key in buckets:
            buckets[key] += 1
    return buckets


def _detect_anomaly_days(bucket, min_count=5, z=2.0):
    """Flag days whose count is both meaningfully above the range's average
    AND clears an absolute floor (so 2 failed logins on a quiet day for a
    brand-new app doesn't get dramatically flagged as an 'anomaly')."""
    values = list(bucket.values())
    if len(values) < 3 or not any(values):
        return []
    mean = statistics.mean(values)
    stdev = statistics.pstdev(values) or 1.0
    return [day for day, v in bucket.items() if v >= min_count and v > mean + z * stdev]


@app.route("/api/admin/analytics")
@admin_required
def api_admin_analytics():
    days = min(max(int(request.args.get("days", 14) or 14), 1), 90)
    since = datetime.utcnow() - timedelta(days=days)

    total_users = User.query.count()
    verified_users = User.query.filter_by(is_verified=True).count()

    signups = User.query.filter(User.created_at >= since).with_entities(User.created_at).all()
    signups_by_day = _bucket_by_day([s[0] for s in signups], days)

    logins = LoginEvent.query.filter(LoginEvent.ts >= since).all()
    logins_by_day = _bucket_by_day([l.ts for l in logins if l.success], days)
    failed_logins_by_day = _bucket_by_day([l.ts for l in logins if not l.success], days)
    total_logins = sum(1 for l in logins if l.success)
    total_failed_logins = sum(1 for l in logins if not l.success)
    currently_locked_out = User.query.filter(User.lockout_until != None, User.lockout_until > datetime.utcnow()).count()  # noqa: E711
    failed_login_anomalies = _detect_anomaly_days(failed_logins_by_day)

    events = FeatureEvent.query.filter(FeatureEvent.ts >= since).all()
    feature_counts, view_counts, leaving_counts = {}, {}, {}
    active_user_ids = set()
    for e in events:
        active_user_ids.add(e.user_id)
        if e.feature.startswith("view:"):
            key = e.feature[len("view:"):]
            view_counts[key] = view_counts.get(key, 0) + 1
        elif e.feature.startswith("leaving:"):
            key = e.feature[len("leaving:"):]
            leaving_counts[key] = leaving_counts.get(key, 0) + 1
        else:
            feature_counts[e.feature] = feature_counts.get(e.feature, 0) + 1
    active_user_ids |= {l.user_id for l in logins if l.success and l.user_id}

    def top(d, n=10):
        return [{"name": k, "count": v} for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n]]

    recent_signups = (
        User.query.order_by(User.created_at.desc()).limit(10)
        .with_entities(User.id, User.full_name, User.email, User.created_at, User.country).all()
    )

    return jsonify({
        "range_days": days,
        "total_users": total_users,
        "verified_users": verified_users,
        "active_users_in_range": len(active_user_ids),
        "total_logins_in_range": total_logins,
        "total_failed_logins_in_range": total_failed_logins,
        "currently_locked_out": currently_locked_out,
        "signups_by_day": signups_by_day,
        "logins_by_day": logins_by_day,
        "failed_logins_by_day": failed_logins_by_day,
        "failed_login_anomaly_days": failed_login_anomalies,
        "most_used_features": top(feature_counts, 10),
        "most_visited_views": top(view_counts, 12),
        "leaving_from": top(leaving_counts, 10),
        "recent_signups": [
            {"id": uid, "name": n, "email": em, "created_at": ca.isoformat() if ca else None, "country": c}
            for uid, n, em, ca, c in recent_signups
        ],
    })


@app.route("/api/admin/user/<int:user_id>")
@admin_required
def api_admin_user_detail(user_id):
    """Per-user drill-down: profile basics, activity counts, and recent
    events, for clicking into a row in the admin signups table."""
    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "No user with that ID."}), 404

    stats = get_or_create_stats(user_id)
    wishlist_count = WishlistItem.query.filter_by(user_id=user_id).count()
    history_count = HistoryItem.query.filter_by(user_id=user_id).count()
    login_events = LoginEvent.query.filter_by(user_id=user_id).order_by(LoginEvent.ts.desc()).limit(20).all()
    feature_events = FeatureEvent.query.filter_by(user_id=user_id).order_by(FeatureEvent.ts.desc()).limit(20).all()
    last_login = next((l.ts for l in login_events if l.success), None)

    return jsonify({
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "country": user.country,
        "currency": user.currency,
        "is_verified": user.is_verified,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login": last_login.isoformat() if last_login else None,
        "currently_locked_out": bool(user.lockout_until and user.lockout_until > datetime.utcnow()),
        "failed_login_attempts": user.failed_login_attempts or 0,
        "searches": stats.searches or 0,
        "accepted_recommendations": stats.accepted or 0,
        "saved_amount": stats.saved or 0,
        "top_categories": sorted((stats.categories or {}).items(), key=lambda kv: kv[1], reverse=True)[:5],
        "wishlist_count": wishlist_count,
        "history_count": history_count,
        "recent_logins": [{"ts": l.ts.isoformat() if l.ts else None, "success": l.success, "ip": l.ip} for l in login_events],
        "recent_features": [{"ts": e.ts.isoformat() if e.ts else None, "feature": e.feature} for e in feature_events],
    })


@app.route("/api/admin/export")
@admin_required
def api_admin_export():
    """CSV export for offline analysis. ?type=signups|logins|features, and
    an optional ?days= for the logins/features exports (signups always
    exports everyone, since a signup list isn't a rolling window)."""
    export_type = (request.args.get("type") or "signups").strip()
    days = min(max(int(request.args.get("days", 30) or 30), 1), 365)
    since = datetime.utcnow() - timedelta(days=days)

    buf = io.StringIO()
    writer = csv.writer(buf)

    if export_type == "signups":
        writer.writerow(["id", "full_name", "email", "country", "currency", "is_verified", "created_at"])
        for u in User.query.order_by(User.created_at.desc()).all():
            writer.writerow([u.id, u.full_name, u.email, u.country, u.currency, u.is_verified,
                              u.created_at.isoformat() if u.created_at else ""])
    elif export_type == "logins":
        writer.writerow(["id", "user_id", "email", "success", "ip", "ts"])
        for l in LoginEvent.query.filter(LoginEvent.ts >= since).order_by(LoginEvent.ts.desc()).all():
            writer.writerow([l.id, l.user_id, l.email, l.success, l.ip, l.ts.isoformat() if l.ts else ""])
    elif export_type == "features":
        writer.writerow(["id", "user_id", "feature", "ts"])
        for e in FeatureEvent.query.filter(FeatureEvent.ts >= since).order_by(FeatureEvent.ts.desc()).all():
            writer.writerow([e.id, e.user_id, e.feature, e.ts.isoformat() if e.ts else ""])
    else:
        return jsonify({"error": "Unknown export type. Use signups, logins, or features."}), 400

    filename = f"prism_{export_type}_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/uploads/pic/<path:filename>")
def serve_profile_picture(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/dashboard")
@login_required
def dashboard():
    user = User.query.get(session["user_id"])
    hour = datetime.utcnow().hour
    if hour < 12:
        greeting = "Good Morning"
    elif hour < 17:
        greeting = "Good Afternoon"
    else:
        greeting = "Good Evening"
    first_name = (user.full_name or "there").split(" ")[0]
    return render_template(
        "dashboard.html",
        user=user,
        greeting=greeting,
        first_name=first_name,
        lens_configured=bool(PRISM_API_KEY),
        profile_picture_url=profile_picture_url(user),
        currency_options=CURRENCY_OPTIONS,
        country_options=COUNTRY_OPTIONS,
    )


@app.route("/api/profile/update", methods=["POST"])
@login_required
def api_profile_update():
    """Let a shopper edit their name, shopping interests, profile photo, and
    account currency/country straight from Settings — no page reload needed."""
    user = User.query.get(session["user_id"])
    if not user:
        return jsonify({"error": "Session expired — please log in again."}), 401

    full_name = (request.form.get("full_name") or "").strip()
    interests = (request.form.get("interests") or "").strip()
    country = (request.form.get("country") or "").strip()
    currency = (request.form.get("currency") or "").strip()

    if full_name:
        user.full_name = full_name
        session["user_name"] = full_name
    user.interests = interests or None
    if country and country in COUNTRY_CURRENCY_MAP:
        user.country = country
    if currency:
        user.currency = normalize_currency(currency)

    uploaded_file = request.files.get("profile_picture")
    if uploaded_file and uploaded_file.filename:
        saved_path, upload_error = save_uploaded_profile_picture(uploaded_file)
        if upload_error:
            return jsonify({"error": upload_error}), 400
        user.profile_picture = saved_path

    db.session.commit()

    return jsonify({
        "full_name": user.full_name,
        "email": user.email,
        "interests": user.interests or "",
        "country": user.country,
        "currency": user.currency,
        "first_name": (user.full_name or "there").split(" ")[0],
        "profile_picture_url": profile_picture_url(user),
    })



# ---------------------------------------------------------------------------
# Dashboard data API — wishlist, price alerts, activity history, stats and
# achievements, all persisted to the database and scoped to the logged-in
# account. The dashboard fetches this once on load ("bootstrap") and then
# calls the small CRUD endpoints below as the shopper interacts with it.
# ---------------------------------------------------------------------------
@app.route("/api/data/bootstrap")
@login_required
def api_data_bootstrap():
    uid = session["user_id"]
    wishlist = WishlistItem.query.filter_by(user_id=uid).order_by(WishlistItem.added_at.desc()).all()
    alerts = AlertItem.query.filter_by(user_id=uid).order_by(AlertItem.created_at.desc()).all()
    history = HistoryItem.query.filter_by(user_id=uid).order_by(HistoryItem.ts.desc()).limit(60).all()
    achievements = Achievement.query.filter_by(user_id=uid).order_by(Achievement.unlocked_at.asc()).all()
    stats = get_or_create_stats(uid)
    return jsonify({
        "wishlist": [w.to_dict() for w in wishlist],
        "alerts": [a.to_dict() for a in alerts],
        "history": [h.to_dict() for h in history],
        "achievements": [a.name for a in achievements],
        "stats": stats.to_dict(),
    })


@app.route("/api/wishlist", methods=["POST"])
@login_required
def api_wishlist_add():
    uid = session["user_id"]
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    price = (body.get("price") or "—").strip() or "—"
    if not name:
        return jsonify({"error": "A product name is required."}), 400
    if WishlistItem.query.filter_by(user_id=uid, name=name).first():
        return jsonify({"error": f"{name} is already on your wishlist.", "duplicate": True}), 409
    item = WishlistItem(id=f"w_{uid}_{int(time.time()*1000)}_{secrets.token_hex(3)}",
                         user_id=uid, name=name, price=price)
    db.session.add(item)
    db.session.commit()
    return jsonify(item.to_dict())


@app.route("/api/wishlist/<item_id>", methods=["DELETE"])
@login_required
def api_wishlist_delete(item_id):
    uid = session["user_id"]
    item = WishlistItem.query.filter_by(id=item_id, user_id=uid).first()
    if item:
        db.session.delete(item)
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/alerts", methods=["POST"])
@login_required
def api_alerts_add():
    uid = session["user_id"]
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    price = (body.get("price") or "").strip()
    if not product or not price:
        return jsonify({"error": "A product name and target price are required."}), 400
    item = AlertItem(id=f"a_{uid}_{int(time.time()*1000)}_{secrets.token_hex(3)}",
                      user_id=uid, product=product, price=price)
    db.session.add(item)
    db.session.commit()
    return jsonify(item.to_dict())


@app.route("/api/alerts/<item_id>", methods=["DELETE"])
@login_required
def api_alerts_delete(item_id):
    uid = session["user_id"]
    item = AlertItem.query.filter_by(id=item_id, user_id=uid).first()
    if item:
        db.session.delete(item)
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/history", methods=["POST"])
@login_required
def api_history_add():
    uid = session["user_id"]
    body = request.get_json(force=True, silent=True) or {}
    type_ = (body.get("type") or "activity").strip()[:40]
    text = (body.get("text") or "").strip()[:500]
    if not text:
        return jsonify({"error": "Nothing to log."}), 400
    item = HistoryItem(user_id=uid, type=type_, text=text)
    db.session.add(item)
    db.session.commit()
    # Keep only the most recent 60 entries per user so the table doesn't
    # grow unbounded — mirrors the old client-side `.slice(0, 60)` cap.
    stale = (HistoryItem.query.filter_by(user_id=uid)
             .order_by(HistoryItem.ts.desc()).offset(60).all())
    for s in stale:
        db.session.delete(s)
    if stale:
        db.session.commit()
    return jsonify(item.to_dict())


@app.route("/api/history", methods=["DELETE"])
@login_required
def api_history_clear():
    uid = session["user_id"]
    HistoryItem.query.filter_by(user_id=uid).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/stats/bump", methods=["POST"])
@login_required
def api_stats_bump():
    uid = session["user_id"]
    body = request.get_json(force=True, silent=True) or {}
    field = (body.get("field") or "").strip()
    amount = body.get("amount", 1)
    if field not in ("saved", "searches", "accepted"):
        return jsonify({"error": "Unknown stat field."}), 400
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 1
    stats = get_or_create_stats(uid)
    setattr(stats, field, (getattr(stats, field) or 0) + amount)
    db.session.commit()
    return jsonify(stats.to_dict())


@app.route("/api/stats/category", methods=["POST"])
@login_required
def api_stats_category():
    uid = session["user_id"]
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "A category name is required."}), 400
    stats = get_or_create_stats(uid)
    cats = stats.categories
    cats[name] = (cats.get(name) or 0) + 1
    stats.categories = cats
    db.session.commit()
    return jsonify(stats.to_dict())


@app.route("/api/track", methods=["POST"])
@csrf.exempt  # low-risk, append-only analytics ping — also sent via
              # navigator.sendBeacon() on logout, which can't attach a CSRF header
@limiter.limit("120 per minute")
def api_track_feature():
    """Log a single coarse feature-usage event (e.g. 'view:search',
    'tool:buy-or-wait', 'leaving:assistant') for the admin analytics panel.
    Requires a logged-in session, but never blocks or errors loudly — this
    must stay invisible to the person actually using the app."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False}), 200
    body = request.get_json(force=True, silent=True) or {}
    feature = (body.get("feature") or "").strip()[:120]
    if feature:
        db.session.add(FeatureEvent(user_id=uid, feature=feature))
        db.session.commit()
    return jsonify({"ok": True})


# --- Real trending signal: Google Trends, no API key required ---------------
# pytrends is an *unofficial* client that scrapes trends.google.com the same
# way a browser would — there's no official free Trends API, so this is the
# closest thing to "real" data without a paid key. Be aware of what that
# actually costs you:
#   - It's not sanctioned by Google and can break silently if they change
#     their frontend; treat failures as expected, not exceptional.
#   - Hitting it every 60 seconds per keyword WILL get the server's IP
#     rate-limited or temporarily blocked. So we cache each keyword for
#     TRENDS_CACHE_TTL and let the *minute-level rotation* (which keywords
#     get shown, from which category) come from our own pool + the account's
#     db-derived category weighting — only the score/direction per keyword
#     is backed by a live Trends lookup, refreshed on a slower, safer cadence.
#   - If pytrends isn't installed, or Google blocks/rate-limits us, or a
#     term simply has no Trends data, we fall back to the deterministic
#     simulated signal — and each item is tagged "live": true/false so the
#     frontend (or you) can tell which is which rather than it being silently
#     presented as real.
try:
    from pytrends.request import TrendReq
    _pytrends_client = TrendReq(hl="en-US", tz=0)
except Exception:
    _pytrends_client = None

TRENDS_CACHE_TTL = 20 * 60  # 20 minutes — see note above on why not every minute
_trends_cache = {}


def _fetch_google_trend(keyword):
    """Return (direction, pct, is_live) for a keyword using real Google
    Trends interest-over-time data, or (None, None, False) if unavailable."""
    now = time.time()
    cached = _trends_cache.get(keyword)
    if cached and (now - cached["fetched_at"] < TRENDS_CACHE_TTL):
        return cached["direction"], cached["pct"], True

    if _pytrends_client is None:
        return None, None, False

    try:
        _pytrends_client.build_payload([keyword], timeframe="now 7-d")
        df = _pytrends_client.interest_over_time()
        if df is None or df.empty or keyword not in df:
            return None, None, False
        series = df[keyword].astype(float)
        if len(series) < 2:
            return None, None, False
        latest, prior = series.iloc[-1], series.iloc[-2]
        base = prior if prior > 0 else 1.0
        pct = round(abs(latest - prior) / base * 100, 1)
        direction = "▲" if latest >= prior else "▼"
        _trends_cache[keyword] = {"direction": direction, "pct": pct, "fetched_at": now}
        return direction, pct, True
    except Exception as e:
        # Rate-limited, blocked, network hiccup, or a frontend change on
        # Google's end — any of these are expected occasionally, not bugs.
        app.logger.info("Google Trends lookup unavailable for %r: %s", keyword, e)
        return None, None, False


# --- "Trending Right Now" ----------------------------------------------------
# A per-category pool of plausible trending items, used both as the candidate
# list (which items even get shown) and as a fallback signal (%/direction)
# for any item Google Trends doesn't return live data for. What's ALWAYS real:
# which categories get shown, and in what order, is driven by *this account's*
# own stats.categories in the db (bumped every time they search/scan/save in
# that category) — so a shopper who's mostly looked at phones sees
# phone-adjacent trends first.
TRENDING_POOL = {
    "electronics":  ["iPhone 17 Pro", "Samsung Galaxy S25", "RTX 5080 laptops", "Noise-cancelling earbuds", "Foldable phones"],
    "phones":       ["iPhone 17 Pro", "Samsung Galaxy S25", "Google Pixel 10", "Foldable phones", "Budget 5G phones"],
    "laptops":      ["RTX 5080 laptops", "Slim ultrabooks", "MacBook Air M4", "Gaming laptops under ₦1.5m", "Chromebooks"],
    "vehicles":     ["Compact EVs", "Used SUVs", "Hybrid sedans", "Tokunbo Corollas", "EV charging accessories"],
    "fashion":      ["Minimalist sneakers", "Oversized denim jackets", "Ankara statement pieces", "Retro running shoes"],
    "homes":        ["Two-bedroom flats (Lekki)", "Studio apartments", "Smart home starter kits", "Mortgage rate trends"],
    "travel":       ["Off-peak flight deals", "Weekend getaway packages", "Travel insurance bundles", "Carry-on luggage"],
    "insurance":    ["Comprehensive auto cover", "Health HMO plans", "Gadget insurance", "Travel insurance bundles"],
    "gaming":       ["PS5 Pro bundles", "Handheld PC gaming", "Mechanical keyboards", "Ray-tracing GPUs"],
    "groceries":    ["Bulk rice prices", "Cooking oil price watch", "Imported pasta brands", "Baby formula prices"],
    "audio":        ["Noise-cancelling earbuds", "Bluetooth speakers", "Studio headphones"],
    "general":      ["iPhone 17 Pro", "RTX 5080 laptops", "Compact EVs", "Noise-cancelling earbuds", "Smart home starter kits"],
}



_INTEREST_CATEGORY_RULES = [
    ("laptops", ["laptop", "notebook", "macbook", "ultrabook", "chromebook"]),
    ("phones", ["phone", "iphone", "samsung", "pixel", "smartphone", "android"]),
    ("vehicles", ["car", "vehicle", "suv", "ev", "sedan", "corolla", "truck"]),
    ("homes", ["apartment", "flat", "house", "home", "mortgage", "rent", "real estate"]),
    ("travel", ["flight", "travel", "hotel", "luggage", "trip", "vacation"]),
    ("insurance", ["insurance", "cover", "hmo", "policy"]),
    ("gaming", ["game", "gaming", "console", "ps5", "xbox", "playstation"]),
    ("fashion", ["sneaker", "shoe", "jacket", "dress", "fashion", "denim", "watch", "style"]),
    ("groceries", ["rice", "grocery", "groceries", "food", "pasta", "formula"]),
    ("audio", ["earbud", "headphone", "speaker", "airpods", "audio"]),
    ("electronics", ["camera", "tv", "gadget", "electronic", "gpu", "rtx", "monitor", "tech"]),
]


def _category_for_text(text):
    """Map a free-text phrase (a declared shopping interest, a search query,
    etc.) onto one of the TRENDING_POOL category keys, or None if nothing
    matches. Mirrors guessCategory() in dashboard.js so profile interests
    and live searches feed the same personalization signal."""
    t = (text or "").lower()
    for cat, words in _INTEREST_CATEGORY_RULES:
        if any(w in t for w in words):
            return cat
    return None


def _trending_change_for(item, minute_seed):
    """Fallback deterministic (not random-each-refresh) +/- % so the same
    item shows the same trend within a given minute even when Google Trends
    has no data for it — a lightweight stand-in, clearly marked as such."""
    h = int(hashlib.sha256(f"{item}:{minute_seed}".encode()).hexdigest(), 16)
    pct = (h % 3400) / 100.0  # 0.00 – 33.99
    direction = "▲" if (h // 3400) % 5 != 0 else "▼"  # mostly up, sometimes down
    return direction, round(max(pct, 0.5), 1)


def _trend_signal_for(item, minute_seed):
    """Real Google Trends data if we can get it (cached ~20 min at a time),
    otherwise the deterministic simulated fallback. Returns (direction, pct, is_live)."""
    direction, pct, is_live = _fetch_google_trend(item)
    if is_live:
        return direction, pct, True
    direction, pct = _trending_change_for(item, minute_seed)
    return direction, pct, False


@app.route("/api/trending")
@login_required
def api_trending():
    """Trending list: rotates every 60 seconds, and is weighted toward the
    signed-in user's own recorded category interest (UserStats.categories)."""
    uid = session["user_id"]
    user = User.query.get(uid)
    stats = get_or_create_stats(uid)
    cats = {k.lower(): v for k, v in (stats.categories or {}).items()}

    # Fold the shopper's *declared* interests (from Profile/Settings) in as a
    # baseline weight, so trending reflects what they said they care about
    # even before they've searched anything — not just derived click/search
    # history. Search-driven weight (bumped every time they actually search,
    # scan, or save something) still stacks on top and can overtake this.
    if user and user.interests:
        for part in user.interests.split(","):
            cat = _category_for_text(part.strip())
            if cat:
                cats[cat] = cats.get(cat, 0) + 2

    # Rank the user's own categories by how often they've engaged with them
    # (declared interest + actual activity combined).
    ranked_user_cats = [c for c, _ in sorted(cats.items(), key=lambda kv: kv[1], reverse=True)]

    # Build a candidate pool: user's top categories first (personalized),
    # then a general pool so the list is never empty for a new account.
    pool_order = [c for c in ranked_user_cats if c in TRENDING_POOL] or []
    for fallback in ("electronics", "general"):
        if fallback not in pool_order:
            pool_order.append(fallback)

    minute_seed = int(time.time() // 60)  # changes exactly once per minute
    items, seen = [], set()
    for cat in pool_order:
        pool = TRENDING_POOL.get(cat, [])
        if not pool:
            continue
        # Deterministic-but-rotating pick within this minute using the seed,
        # so which items surface from a category shifts minute to minute.
        offset = (minute_seed + hash(cat)) % len(pool)
        rotated = pool[offset:] + pool[:offset]
        for name in rotated:
            if name in seen:
                continue
            seen.add(name)
            direction, pct, is_live = _trend_signal_for(name, minute_seed)
            items.append({"name": name, "direction": direction, "pct": pct, "category": cat, "live": is_live})
            break  # one per category per pass keeps the list varied
        if len(items) >= 4:
            break

    # Top up to 4 items from the general pool if personalized categories ran dry.
    if len(items) < 4:
        for name in TRENDING_POOL["general"]:
            if len(items) >= 4:
                break
            if name in seen:
                continue
            seen.add(name)
            direction, pct, is_live = _trend_signal_for(name, minute_seed)
            items.append({"name": name, "direction": direction, "pct": pct, "category": "general", "live": is_live})

    return jsonify({
        "items": items[:4],
        "personalized": bool(pool_order and pool_order[0] not in ("electronics", "general")),
        "refreshes_in_seconds": 60 - (int(time.time()) % 60),
        "note": "pct/direction is real Google Trends data where available (see each item's 'live' flag), otherwise a clearly-marked simulated fallback.",
    })


@app.route("/api/achievements", methods=["POST"])
@login_required
def api_achievements_unlock():
    uid = session["user_id"]
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "An achievement name is required."}), 400
    if Achievement.query.filter_by(user_id=uid, name=name).first():
        return jsonify({"unlocked": False, "name": name})
    db.session.add(Achievement(user_id=uid, name=name))
    db.session.commit()
    return jsonify({"unlocked": True, "name": name})


@app.route("/api/data/clear", methods=["DELETE"])
@login_required
def api_data_clear():
    """Wipe all persisted dashboard data for this account (wishlist,
    alerts, history, achievements, stats) — the DB-backed replacement for
    the old 'clear local storage' settings action."""
    uid = session["user_id"]
    WishlistItem.query.filter_by(user_id=uid).delete()
    AlertItem.query.filter_by(user_id=uid).delete()
    HistoryItem.query.filter_by(user_id=uid).delete()
    Achievement.query.filter_by(user_id=uid).delete()
    UserStats.query.filter_by(user_id=uid).delete()
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# AI Buying Intelligence API (Groq-powered, "Prism" branded)
# ---------------------------------------------------------------------------
def _lens_error_response(exc):
    app.logger.error("Prism engine error: %s", exc)
    return jsonify({"error": str(exc)}), 502


@app.route("/api/ai/chat/stream", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_chat_stream():
    """Streaming AI Shopping Assistant — Server-Sent Events. Accepts an
    optional inline image (base64) so a shopper can drop a photo straight
    into the conversation instead of only using the separate Scanner."""
    body = request.get_json(force=True, silent=True) or {}
    history = body.get("history", [])  # [{role: 'user'|'model', text: '...'}]
    message = (body.get("message") or "").strip()
    image_b64 = body.get("image")
    image_mime = (body.get("image_mime_type") or "image/jpeg").strip()

    if not message and not image_b64:
        return jsonify({"error": "Message is required."}), 400

    contents = [{"role": h.get("role"), "parts": [{"text": h.get("text", "")}]} for h in history]

    parts = []
    if message:
        parts.append({"text": message})
    if image_b64:
        parts.append({"inline_data": {"mime_type": image_mime, "data": image_b64}})
        if not message:
            parts.insert(0, {"text": "Identify what's in this photo and give me buying advice on it."})
    contents.append({"role": "user", "parts": parts})

    # Photos require the vision-capable model; text-only chat keeps using
    # the (cheaper/faster) text model.
    chat_model = GROQ_VISION_MODEL if image_b64 else None

    if not PRISM_API_KEY:
        def _no_key():
            yield f"data: {json.dumps({'text': '⚠️ Prism\u2019s AI engine is not configured on the server right now. Add a GROQ_API_KEY to your environment to activate live AI answers.'})}\n\n"
            yield "event: done\ndata: {}\n\n"
        return Response(stream_with_context(_no_key()), mimetype="text/event-stream")

    memory_notes = build_memory_profile(session["user_id"])["notes"]

    return Response(
        stream_with_context(lens_stream(contents, system_instruction=build_system_prompt(memory_notes), model=chat_model)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/fx/usd-ngn")
@login_required
def api_fx_usd_ngn():
    """Live USD -> account-currency rate, fetched (and cached) from a free FX
    API — used by the dashboard to show a trustworthy rate instead of one the
    AI guesses, and kept fixed to the shopper's chosen Settings currency."""
    currency = account_currency()
    return jsonify({
        "rate": get_usd_rate_for(currency),
        "currency": currency,
        "symbol": _CURRENCY_SYMBOLS.get(currency, ""),
        "pair": f"USD/{currency}",
    })


@app.route("/api/ai/search", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_search():
    """Natural-language product search -> structured verdict + candidate picks."""
    body = request.get_json(force=True, silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "A search query is required."}), 400

    currency = account_currency()
    symbol = _CURRENCY_SYMBOLS.get(currency, "")

    prompt = f"""A shopper searched: "{query}"

Respond with ONLY JSON matching this exact shape, no markdown fences:
{{
  "interpretation": "one sentence describing what you understood the shopper wants",
  "verdict": "2-3 sentence buying verdict / advice — be direct, not hedged, about what you actually know",
  "picks": [
    {{"name": "product name", "price_estimate": "e.g. {symbol}450,000 - {symbol}520,000", "tag": "Best Overall | Best Value | Best Long-Term | Premium Pick", "why": "1-2 sentence reasoning", "pros": ["..","..)"], "cons": ["..",".."]}}
  ]
}}
Provide exactly 3 picks, realistic and specific to the query (real-world plausible models/specs).
Every price_estimate MUST be in {currency} ({symbol}) — do not use any other currency."""

    internal_matches = find_internal_matches(query)

    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(build_memory_profile(session["user_id"])["notes"], currency=currency),
        )
        data = enrich_picks(data)
        data["internal_matches"] = internal_matches
        if internal_matches:
            # A real seller on Prism already has this — that beats an AI
            # price estimate or a link off-platform, so say so up front.
            data["verdict"] = (
                f"Good news — a Prism seller already has this. Buy it directly from "
                f"{internal_matches[0]['shopName']} on Prism instead of guessing from "
                f"estimates or going elsewhere. " + data.get("verdict", "")
            ).strip()
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/live-prices", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_live_prices():
    """Phase 1 of real pricing: actual current prices for a named product
    from Amazon/Walmart/Best Buy/Target/eBay via a Google Shopping
    aggregator (SerpApi) — not an AI estimate. Called on-demand for one
    product at a time (e.g. a 'Check live prices' button on a specific
    pick) rather than automatically for every AI response, since each
    fresh lookup costs a real API call."""
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    if not product:
        return jsonify({"error": "Which product should Prism check live prices for?"}), 400

    currency = account_currency()
    results, error = get_live_prices(product, currency)
    if error and not results:
        return jsonify({"error": error}), 502

    return jsonify({"product": product, "currency": currency, "prices": results})


@app.route("/api/ai/recommend", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_recommend():
    """Budget/need based recommendation engine."""
    body = request.get_json(force=True, silent=True) or {}
    need = (body.get("need") or "").strip()
    budget = (body.get("budget") or "").strip()
    currency = account_currency()  # fixed per-account currency, not per-request
    if not need:
        return jsonify({"error": "Tell Prism what you're shopping for."}), 400

    prompt = f"""Shopper need: "{need}"
Budget: {budget or 'not specified'} {currency}

If this budget is unrealistic for the need described, say so plainly in "summary" instead of
forcing 3 picks that don't actually fit — explain what it would realistically take, or what
real tradeoff (older/refurbished/smaller/different category) actually fits this budget.

Return ONLY JSON, no markdown fences:
{{
  "summary": "2 sentence framing of the recommendation — direct and honest, including any budget mismatch",
  "picks": [
    {{"rank": 1, "name": "..", "price": "..", "tag": "Best Overall", "performance": "short note", "battery": "short note", "value": "short note", "long_term": "short note", "why": "2 sentence reasoning"}},
    {{"rank": 2, "name": "..", "price": "..", "tag": "Best Value", "performance": "..", "battery": "..", "value": "..", "long_term": "..", "why": ".."}},
    {{"rank": 3, "name": "..", "price": "..", "tag": "Best Long-Term", "performance": "..", "battery": "..", "value": "..", "long_term": "..", "why": ".."}}
  ]
}}
Every "price" MUST be in {currency} — do not use any other currency."""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(build_memory_profile(session["user_id"])["notes"], currency=currency),
        )
        data = enrich_picks(data)
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/compare", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_compare():
    body = request.get_json(force=True, silent=True) or {}
    products = [p.strip() for p in body.get("products", []) if p and p.strip()]
    if len(products) < 2:
        return jsonify({"error": "Give Prism at least two products to compare."}), 400

    prompt = f"""Compare these products for a shopper: {', '.join(products)}

Return ONLY JSON, no markdown fences:
{{
  "products": ["name1", "name2", ...],
  "rows": [
    {{"spec": "Price", "values": ["..", "..", ".."]}},
    {{"spec": "Performance", "values": ["..", "..", ".."]}},
    {{"spec": "Battery", "values": ["..", "..", ".."]}},
    {{"spec": "Camera / Display", "values": ["..", "..", ".."]}},
    {{"spec": "Build & Repairability", "values": ["..", "..", ".."]}},
    {{"spec": "Resale / Long-term value", "values": ["..", "..", ".."]}}
  ],
  "winner": "name of the best overall pick",
  "verdict": "2-3 sentence explanation of the winner and key tradeoffs"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
        )
        names = data.get("products") or products
        data["product_links"] = {nm: marketplace_links(nm) for nm in names}
        data["product_images_query"] = {nm: nm for nm in names}
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/reviews", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_reviews():
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    if not product:
        return jsonify({"error": "Which product should Prism summarize reviews for?"}), 400

    prompt = f"""Summarize the general review sentiment for: "{product}"

Return ONLY JSON, no markdown fences:
{{
  "product": "{product}",
  "confidence_score": 0-100 integer,
  "verdict": "Buy | Wait | Consider Alternatives",
  "loves": ["short phrase", "short phrase", "short phrase"],
  "dislikes": ["short phrase", "short phrase", "short phrase"],
  "common_issues": ["short phrase", "short phrase"],
  "summary": "2-3 sentence overall verdict on whether to buy"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
        )
        data = enrich_single(data, "product")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/scam-check", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_scam_check():
    body = request.get_json(force=True, silent=True) or {}
    url_or_listing = (body.get("url") or "").strip()
    if not url_or_listing:
        return jsonify({"error": "Paste a listing URL or description to check."}), 400

    prompt = f"""A shopper wants a scam-risk assessment of this listing/URL: "{url_or_listing}"

You cannot actually browse it, so reason from domain patterns, naming conventions, typical
red flags in URLs/listings (misspelled brand domains, unusual TLDs, too-good pricing framing,
no HTTPS conventions implied, generic marketplace patterns, etc). Be transparent this is a
heuristic read, not a live scan.

Return ONLY JSON, no markdown fences:
{{
  "risk_score": 0-100 integer (higher = riskier),
  "risk_label": "Low Risk | Moderate Risk | High Risk",
  "red_flags": ["short flag", "short flag", "short flag"],
  "green_flags": ["short reassuring point", "short reassuring point"],
  "domain_notes": "1-2 sentence note on the domain/URL pattern",
  "pricing_notes": "1-2 sentence note on pricing plausibility if mentioned",
  "safety_tips": ["short actionable tip", "short actionable tip", "short actionable tip"],
  "summary": "2 sentence overall guidance"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
            temperature=0.4,
        )
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/vision", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_vision():
    """AI Camera / barcode scanner — Prism Vision analysis of an uploaded image."""
    uploaded = request.files.get("image")
    mode = (request.form.get("mode") or "product").strip()  # 'product' | 'barcode'
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Attach an image to scan."}), 400

    ok, error, image_bytes, mime_type = validate_image_upload(uploaded, MAX_SCAN_IMAGE_BYTES)
    if not ok:
        return jsonify({"error": error}), 400

    b64 = base64.b64encode(image_bytes).decode("utf-8")

    if mode == "barcode":
        instruction = (
            "Look closely for any barcode/UPC/QR code in this image. Read its digits if legible. "
            "Then identify the product it most likely belongs to."
        )
    else:
        instruction = "Identify the product shown in this image in detail."

    currency = account_currency()
    symbol = _CURRENCY_SYMBOLS.get(currency, "")

    prompt = f"""{instruction}

STRICT HONESTY RULES — follow these exactly, this scan is used for real purchase decisions:
1. If the image does not clearly show an identifiable, purchasable product (e.g. it's blurry,
   a random scene, a person, text/document, or anything you can't confidently tie to a real
   product category), say that plainly in "summary", set "confidence" to "low", and leave
   "product_name" as a general description rather than inventing a specific model.
2. Never invent a spec, brand, or feature you cannot actually see or reasonably infer from what
   IS visible — an empty specs list is correct if you have nothing solid to report.
3. "estimated_price" must be a realistic {currency} price band for the item as you've identified
   it — do not give a random or generic number disconnected from the actual product/category, and
   do not pad the range so wide it becomes meaningless. If you truly can't estimate a price (e.g.
   the product is unidentifiable), return an empty string rather than a guess.
4. Only report barcode digits you can actually read clearly — if none are legible, return an
   empty string rather than guessing digits.
5. Match "confidence" honestly to how sure you actually are — most real-world photos (angle,
   lighting, cropping) warrant "medium" at best; reserve "high" for genuinely clear, unambiguous
   shots of a well-known product.

Return ONLY JSON, no markdown fences:
{{
  "product_name": "your best identification, or a general description if you're not sure of the exact model",
  "brand": "brand name if visible/inferable, else empty string",
  "category": "..",
  "estimated_price": "e.g. {symbol}250,000 - {symbol}300,000 (in {currency}), or empty string if you cannot estimate",
  "barcode_digits": "digits if clearly legible, else empty string",
  "confidence": "high | medium | low — how sure you are this identification is correct",
  "specs": ["only specs you can actually see or confidently infer — omit if none"],
  "summary": "2-3 sentence description of what's in the image and its condition/notable traits; mention plainly if the photo made identification hard or impossible",
  "recommendations": ["short buying tip", "short buying tip"],
  "alternatives": ["alternative product name", "alternative product name"]
}}"""

    contents = [{
        "role": "user",
        "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime_type, "data": b64}},
        ],
    }]
    try:
        data = lens_generate_json(contents, system_instruction=build_system_prompt(currency=currency),
                                     model=GROQ_VISION_MODEL, temperature=0.3)
        data = enrich_single(data, "product_name")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/price-history", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_price_history():
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    if not product:
        return jsonify({"error": "Which product's price trend should Prism explain?"}), 400

    prompt = f"""Give a plausible 12-month relative price-index trend (100 = today's price, so
past months are typically >100 if prices have been falling, or model whatever realistic pattern
fits the product category) for: "{product}"

Return ONLY JSON, no markdown fences:
{{
  "product": "{product}",
  "trend": [{{"month": "Jan", "index": 100}}, ... 12 entries ending at "this month"],
  "best_time_to_buy": "short recommendation",
  "expected_movement": "1-2 sentence prediction for the next 8-12 weeks",
  "seasonal_notes": "1-2 sentence note on seasonal patterns for this category"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
        )
        data = enrich_single(data, "product")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/memory/profile")
@login_required
def api_memory_profile():
    """AI Shopping Memory — a heuristic (no AI call) read of this account's
    stored wishlist/history/stats, surfaced as plain-language preferences."""
    return jsonify(build_memory_profile(session["user_id"]))


@app.route("/api/ai/negotiate", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_negotiate():
    """AI Negotiator — reads a listing's asking price, estimates fair market
    value, and drafts an opening negotiation message the shopper can send."""
    body = request.get_json(force=True, silent=True) or {}
    listing = (body.get("listing") or "").strip()
    if not listing:
        return jsonify({"error": "Paste the listing (price + description) to negotiate."}), 400

    prompt = f"""A shopper is about to negotiate on this listing:
\"\"\"{listing}\"\"\"

Estimate the seller's asking price, a realistic market value range, a smart opening
offer, and draft a short, polite negotiation message the shopper could send as-is.

Return ONLY JSON, no markdown fences:
{{
  "seller_price": "e.g. \\u20a6950,000",
  "market_low": "e.g. \\u20a6780,000",
  "market_high": "e.g. \\u20a6840,000",
  "recommended_offer": "e.g. \\u20a6790,000",
  "negotiation_chance": 0-100 integer,
  "reasoning": "1-2 sentence explanation of the offer strategy",
  "message": "a friendly, ready-to-send negotiation message, under 60 words"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(), temperature=0.5,
        )
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/fake-listing", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_fake_listing():
    """Fake Listing / Authenticity Detector — heuristic read of a pasted
    listing for common counterfeit/scam-listing red flags."""
    body = request.get_json(force=True, silent=True) or {}
    listing = (body.get("listing") or "").strip()
    if not listing:
        return jsonify({"error": "Paste the listing text/description to check."}), 400

    prompt = f"""Assess how authentic/genuine this listing looks (not a live scan — reason
from the text itself: pricing plausibility, description patterns, wording that reads
copy-pasted from a manufacturer page, urgency language, seller-account cues if mentioned):
\"\"\"{listing}\"\"\"

Return ONLY JSON, no markdown fences:
{{
  "authenticity_score": 0-100 integer,
  "risk_label": "Low | Medium | High",
  "checks": [
    {{"status": "good", "note": "short observation"}},
    {{"status": "warn", "note": "short observation"}},
    {{"status": "good", "note": "short observation"}}
  ],
  "summary": "1-2 sentence overall read"
}}
Include 3-6 checks total, mixing "good" and "warn" statuses based on what's actually
plausible from the text — don't invent seller-account-age or reverse-image-search
findings you have no basis for; only flag what the text itself supports."""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(), temperature=0.4,
        )
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/shopping-agent", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_shopping_agent():
    """AI Shopping Agent — simulates checking multiple marketplaces and
    returns the single best deal found, framed as an autonomous search."""
    body = request.get_json(force=True, silent=True) or {}
    need = (body.get("need") or "").strip()
    budget = (body.get("budget") or "").strip()
    currency = account_currency()  # fixed per-account currency, not per-request
    if not need:
        return jsonify({"error": "Tell Prism what to go find."}), 400

    stores = ["Jumia", "Konga", "Amazon", "AliExpress", "eBay", "Temu"]
    prompt = f"""Shopper wants: "{need}"
Budget: {budget or 'not specified'} {currency}

Simulate checking these marketplaces: {', '.join(stores)}. Return your single best
overall deal recommendation (product + store + price), plus a fair market price to
compare it against so savings can be shown.

Return ONLY JSON, no markdown fences:
{{
  "product": "exact product name/model",
  "best_store": "one of: {', '.join(stores)}",
  "best_price": "e.g. \\u20a61,520,000",
  "market_price": "e.g. \\u20a61,660,000",
  "savings": "e.g. \\u20a6140,000",
  "delivery_estimate": "e.g. 10-14 days",
  "warranty": "short note on typical warranty for this store/product",
  "why_this_store": "1-2 sentence reasoning"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(build_memory_profile(session["user_id"])["notes"]),
        )
        data["stores_checked"] = stores
        data = enrich_single(data, "product")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/budget-planner", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_budget_planner():
    """AI Budget Planner — splits a lump budget across a shopping goal's
    typical component list, leaving a realistic remainder."""
    body = request.get_json(force=True, silent=True) or {}
    budget = (body.get("budget") or "").strip()
    currency = account_currency()  # fixed per-account currency, not per-request
    goal = (body.get("goal") or "").strip()
    if not budget or not goal:
        return jsonify({"error": "Tell Prism the goal and the total budget."}), 400

    prompt = f"""Shopper's goal: "{goal}"
Total budget: {budget} {currency}

Break this budget into the realistic list of items typically needed for this goal,
each with an estimated cost, so the total (items + remaining) equals the full budget.

Return ONLY JSON, no markdown fences:
{{
  "goal": "{goal}",
  "total_budget": "{budget} {currency}",
  "items": [
    {{"name": "..", "price": "e.g. \\u20a6700,000"}},
    {{"name": "..", "price": ".."}}
  ],
  "remaining": "e.g. \\u20a675,000",
  "notes": "1-2 sentence tip on where to spend more/less"
}}
Keep the item list to 4-7 realistic items for this goal."""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
        )
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/buy-or-wait", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_buy_or_wait():
    """Buy or Wait Predictor — current price vs. fair market value, a
    predicted 30-day price movement, and a clear buy-now-or-wait call
    with an estimated savings figure if waiting pays off."""
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    if not product:
        return jsonify({"error": "Which product should Prism predict on?"}), 400

    prompt = f"""Should a shopper buy this now or wait: "{product}"

Reason from typical product-cycle patterns (recency of release, category price-decay
curves, known refresh cycles, seasonal sales windows) — you have no live pricing feed,
so frame every number here as a considered estimate, not a fact.

Return ONLY JSON, no markdown fences:
{{
  "product": "{product}",
  "current_price": "e.g. \\u20a6850,000 (a realistic current retail estimate)",
  "fair_market_value": "e.g. \\u20a6790,000 (what it should reasonably cost)",
  "predicted_30d_movement_pct": a signed number like -8 or 3 (negative = price expected to fall),
  "verdict": "BUY | WAIT",
  "recommendation": "short actionable line, e.g. 'Wait 2-3 weeks' or 'Buy now'",
  "confidence": 0-100 integer,
  "reason": "2-3 sentence explanation grounded in product-cycle/pricing logic",
  "estimated_savings": "e.g. \\u20a668,000, or 'Minimal' if verdict is BUY"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(), temperature=0.5,
        )
        data = enrich_single(data, "product")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/scam-messages", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_scam_messages():
    """Scam Message Detector — reads pasted chat/WhatsApp messages from a
    seller for manipulation and scam red flags."""
    body = request.get_json(force=True, silent=True) or {}
    messages = (body.get("messages") or "").strip()
    if not messages:
        return jsonify({"error": "Paste the seller's messages to check."}), 400

    prompt = f"""Assess these seller messages for scam risk (reasoning from tone, urgency,
payment-before-inspection requests, refusal to video call, and similar patterns —
only flag what the text actually shows):
\"\"\"{messages}\"\"\"

Return ONLY JSON, no markdown fences:
{{
  "scam_probability": 0-100 integer,
  "reasons": ["short flag", "short flag", "short flag"],
  "recommendation": "1-2 sentence action the shopper should take"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(), temperature=0.3,
        )
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/lifespan", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_lifespan():
    """Product Life Expectancy — typical lifespan, repairability, and
    expected resale value down the line for a named product."""
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    if not product:
        return jsonify({"error": "Which product should Prism estimate lifespan for?"}), 400

    prompt = f"""Estimate the realistic long-term ownership profile of: "{product}"

Return ONLY JSON, no markdown fences:
{{
  "product": "{product}",
  "average_lifespan": "e.g. 7 years",
  "battery_cycles": "e.g. 1000 cycles, or 'N/A' if not battery-powered",
  "repairability": "e.g. 8.5/10",
  "software_support": "e.g. Updates expected until ~2034",
  "resale_after_3y": "e.g. \\u20a6650,000, framed as an estimate"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
        )
        data = enrich_single(data, "product")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/deal-hunter", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_deal_hunter():
    """AI Deal Hunter — a small daily digest of plausible deals, tailored
    toward the shopper's stated interests / shopping memory when available."""
    user = User.query.get(session["user_id"])
    interests = (user.interests if user else "") or "general electronics"
    memory = build_memory_profile(session["user_id"])

    prompt = f"""Generate a short "today's deals" digest for a shopper interested in:
{interests}
{"Known preferences: " + "; ".join(memory["notes"]) if memory["notes"] else ""}

Frame these as realistic, plausible current deals for this category (clearly estimates,
not scraped live listings).

Return ONLY JSON, no markdown fences:
{{
  "deals": [
    {{"name": "..", "detail": "e.g. '21% OFF' or '\\u20a680,000 cheaper than usual'", "urgency": "e.g. 'Flash sale ends in 2 hours', or empty string"}},
    {{"name": "..", "detail": "..", "urgency": ""}},
    {{"name": "..", "detail": "..", "urgency": ""}}
  ]
}}
Exactly 3 deals."""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(), temperature=0.8,
        )
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/resale-predictor", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_resale_predictor():
    """AI Resale Predictor — projects a product's value at 1/2/3 years out
    from its purchase price."""
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    buy_price = (body.get("buy_price") or "").strip()
    currency = account_currency()  # fixed per-account currency, not per-request
    if not product:
        return jsonify({"error": "Which product should Prism predict resale value for?"}), 400

    prompt = f"""Project resale value over time for: "{product}"
{"Purchase price: " + buy_price + " " + currency if buy_price else "Assume a realistic current retail price."}

Return ONLY JSON, no markdown fences:
{{
  "product": "{product}",
  "buy_price": "e.g. \\u20a61,800,000",
  "value_1y": "e.g. \\u20a61,520,000",
  "value_2y": "e.g. \\u20a61,310,000",
  "value_3y": "e.g. \\u20a61,050,000",
  "depreciation_label": "Low | Moderate | High",
  "notes": "1-2 sentence explanation of the depreciation curve for this category"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
        )
        data = enrich_single(data, "product")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/compatibility", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_compatibility():
    """AI Compatibility Checker — checks a part against a freeform
    description of the shopper's existing PC build."""
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    system_desc = (body.get("system") or "").strip()
    if not product or not system_desc:
        return jsonify({"error": "Give Prism the part and a description of your current PC."}), 400

    prompt = f"""A shopper wants to add this part: "{product}"
Their current system: "{system_desc}"

Check compatibility against the parts they mentioned (CPU/PSU/case/motherboard/RAM,
whichever they gave you) and estimate any upgrade cost needed.

Return ONLY JSON, no markdown fences:
{{
  "part": "{product}",
  "checks": [
    {{"component": "PSU", "status": "compatible | needs_upgrade | unknown", "note": "short note"}},
    {{"component": "Case", "status": "compatible | needs_upgrade | unknown", "note": "short note"}},
    {{"component": "Motherboard", "status": "compatible | needs_upgrade | unknown", "note": "short note"}}
  ],
  "estimated_upgrade_cost": "e.g. \\u20a665,000, or 'None needed'",
  "summary": "1-2 sentence overall verdict"
}}
Only include components the shopper actually mentioned or that are clearly relevant;
mark status "unknown" rather than guessing if info is missing."""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(), temperature=0.4,
        )
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/timeline", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_timeline():
    """AI Product Timeline — a short release-to-now history for a product,
    rendered as a vertical timeline in the UI."""
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    if not product:
        return jsonify({"error": "Which product should Prism map a timeline for?"}), 400

    prompt = f"""Build a short timeline of notable moments for: "{product}"
(release, notable price changes, known issues/fixes, and a verdict on the best time
to buy relative to now).

Return ONLY JSON, no markdown fences:
{{
  "product": "{product}",
  "events": [
    {{"year": "2023", "label": "Released"}},
    {{"year": "2024", "label": "Price dropped ~12%"}},
    {{"year": "2025", "label": "..."}},
    {{"year": "2026", "label": "Best time to buy"}}
  ]
}}
3-6 events, chronological, last one should be a present-day verdict."""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
        )
        data = enrich_single(data, "product")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/community", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_community():
    """AI Buyer Community — a plausible aggregate-owner-sentiment snapshot
    for a product (clearly framed as an estimate, not scraped reviews)."""
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    if not product:
        return jsonify({"error": "Which product should Prism summarize owner sentiment for?"}), 400

    prompt = f"""Estimate typical owner sentiment for: "{product}", framed as if summarizing
a community of past buyers (this is a plausible estimate from general review sentiment
patterns you know about this category/product, not a live scrape — be transparent
about that if asked, but the numbers here should just read as a clean snapshot).

Return ONLY JSON, no markdown fences:
{{
  "product": "{product}",
  "recommend_pct": 0-100 integer,
  "most_common_complaint": "short phrase",
  "most_loved_feature": "short phrase",
  "summary": "1-2 sentence overall read"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
        )
        data = enrich_single(data, "product")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/score", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_score():
    """AI Shopping Score — a multi-axis scorecard (value/performance/
    repairability/future-proofing) for a named product."""
    body = request.get_json(force=True, silent=True) or {}
    product = (body.get("product") or "").strip()
    if not product:
        return jsonify({"error": "Which product should Prism score?"}), 400

    prompt = f"""Score this product across key buying dimensions: "{product}"

Return ONLY JSON, no markdown fences:
{{
  "product": "{product}",
  "value": 0.0-10.0,
  "performance": 0.0-10.0,
  "repairability": 0.0-10.0,
  "future_proof": 0.0-10.0,
  "overall": 0.0-10.0,
  "summary": "1 sentence justifying the overall score"
}}"""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(),
        )
        data = enrich_single(data, "product")
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


@app.route("/api/ai/copilot", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_ai_copilot():
    """AI Shopping Copilot — turns a life situation ("moving into my first
    apartment") into a full categorized shopping plan with a buy order."""
    body = request.get_json(force=True, silent=True) or {}
    situation = (body.get("situation") or "").strip()
    budget = (body.get("budget") or "").strip()
    currency = account_currency()  # fixed per-account currency, not per-request
    if not situation:
        return jsonify({"error": "Describe the situation you're shopping for."}), 400

    prompt = f"""A shopper describes their situation: "{situation}"
{"Budget: " + budget + " " + currency if budget else "No specific budget given — plan sensibly."}

Build a full categorized shopping plan: group items into logical categories (e.g. rooms,
or phases), rate each item's priority, estimate a total cost, and suggest a sensible
buy order across a few weeks/phases so the shopper doesn't buy everything at once.

Return ONLY JSON, no markdown fences:
{{
  "categories": [
    {{"name": "Living Room", "items": [{{"name": "TV", "priority": 1-5}}, {{"name": "Sofa", "priority": 1-5}}]}},
    {{"name": "Kitchen", "items": [{{"name": "Microwave", "priority": 1-5}}]}}
  ],
  "estimated_cost": "e.g. \\u20a62,850,000",
  "buy_order": [
    {{"phase": "Week 1", "items": ["Mattress", "Microwave"]}},
    {{"phase": "Week 2", "items": ["Sofa", "TV"]}}
  ]
}}
3-5 categories, 2-4 items each; buy order should cover all items across 2-4 phases,
essentials first."""
    try:
        data = lens_generate_json(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=build_system_prompt(build_memory_profile(session["user_id"])["notes"]),
            temperature=0.6,
        )
        return jsonify(data)
    except Exception as exc:
        return _lens_error_response(exc)


# ---------------------------------------------------------------------------
# Marketplace API — sellers manage their storefront from the dashboard;
# buyers browse from the dashboard too, but the storefront itself
# (/store/<slug>) is a public, shareable page so a seller can send the link
# to anyone, logged in or not.
# ---------------------------------------------------------------------------
def _shop_for_current_user():
    return Shop.query.filter_by(user_id=session["user_id"]).first()


@app.route("/api/shop/mine")
@login_required
def api_shop_mine():
    shop = _shop_for_current_user()
    if not shop:
        return jsonify({"shop": None, "products": [], "conversations": []})
    products = ShopProduct.query.filter_by(shop_id=shop.id).order_by(ShopProduct.created_at.desc()).all()
    convos = ShopConversation.query.filter_by(shop_id=shop.id).order_by(ShopConversation.last_message_at.desc()).limit(100).all()
    convo_list = []
    for c in convos:
        last = ShopMessage.query.filter_by(conversation_id=c.id).order_by(ShopMessage.created_at.desc()).first()
        convo_list.append(c.to_dict(last))
    return jsonify({
        "shop": shop.to_dict(product_count=len(products)),
        "products": [p.to_dict() for p in products],
        "conversations": convo_list,
    })


@app.route("/api/shop/signup", methods=["POST"])
@limiter.limit("10 per minute")
@login_required
def api_shop_signup():
    """Create or update the logged-in user's storefront. One shop per
    account — resubmitting this form (e.g. from 'Edit store') updates the
    existing shop in place rather than creating a second one."""
    uid = session["user_id"]
    name = (request.form.get("name") or "").strip()
    category = (request.form.get("category") or "").strip()
    description = (request.form.get("description") or "").strip()
    contact = (request.form.get("contact") or "").strip()
    design = (request.form.get("design") or DEFAULT_SHOP_DESIGN).strip()
    if design not in SHOP_DESIGNS:
        design = DEFAULT_SHOP_DESIGN
    template = (request.form.get("template") or DEFAULT_SHOP_TEMPLATE).strip()
    if template not in SHOP_TEMPLATES:
        template = DEFAULT_SHOP_TEMPLATE

    if not name:
        return jsonify({"error": "Give your store a name."}), 400
    if len(name) > 160:
        return jsonify({"error": "That store name is too long."}), 400

    shop = _shop_for_current_user()
    is_new = shop is None
    if is_new:
        shop = Shop(user_id=uid, slug=generate_unique_shop_slug(name), name=name)
        db.session.add(shop)

    shop.name = name
    shop.category = category or None
    shop.description = description or None
    shop.contact = contact or None
    shop.design = design
    shop.template = template

    cover_file = request.files.get("cover_image")
    if cover_file and cover_file.filename:
        saved_path, upload_error = save_uploaded_shop_image(cover_file)
        if upload_error:
            return jsonify({"error": upload_error}), 400
        shop.cover_image = saved_path

    logo_file = request.files.get("logo_image")
    if logo_file and logo_file.filename:
        saved_path, upload_error = save_uploaded_shop_image(logo_file)
        if upload_error:
            return jsonify({"error": upload_error}), 400
        shop.logo_image = saved_path

    db.session.commit()
    return jsonify({"shop": shop.to_dict(product_count=ShopProduct.query.filter_by(shop_id=shop.id).count()), "created": is_new})


@app.route("/api/shop/products", methods=["POST"])
@limiter.limit("30 per minute")
@login_required
def api_shop_product_add():
    shop = _shop_for_current_user()
    if not shop:
        return jsonify({"error": "Open your store before adding products."}), 400

    existing_count = ShopProduct.query.filter_by(shop_id=shop.id).count()
    if existing_count >= MAX_PRODUCTS_PER_SHOP:
        return jsonify({"error": f"You've reached the {MAX_PRODUCTS_PER_SHOP}-product limit for one store."}), 400

    name = (request.form.get("name") or "").strip()
    price = (request.form.get("price") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not name or not price:
        return jsonify({"error": "Give the product a name and a price."}), 400

    photo_files = [f for f in request.files.getlist("photos") if f and f.filename][:MAX_PRODUCT_PHOTOS]
    saved_paths = []
    for f in photo_files:
        saved_path, upload_error = save_uploaded_shop_image(f)
        if upload_error:
            return jsonify({"error": upload_error}), 400
        saved_paths.append(saved_path)

    product = ShopProduct(
        id=f"sp_{shop.id}_{int(time.time()*1000)}_{secrets.token_hex(3)}",
        shop_id=shop.id, name=name, price=price, description=description or None,
    )
    product.photos = saved_paths
    db.session.add(product)
    db.session.commit()
    return jsonify(product.to_dict())


@app.route("/api/shop/products/<product_id>", methods=["PATCH"])
@login_required
def api_shop_product_update(product_id):
    """Used from the dashboard's per-product menu to flag 'Limited stock' /
    'Sold out' / back to 'In stock' without deleting and re-adding the item."""
    shop = _shop_for_current_user()
    if not shop:
        return jsonify({"error": "No store found."}), 404
    product = ShopProduct.query.filter_by(id=product_id, shop_id=shop.id).first()
    if not product:
        return jsonify({"error": "Product not found."}), 404

    body = request.get_json(force=True, silent=True) or {}
    if "stock_status" in body:
        status = (body.get("stock_status") or "").strip()
        if status not in STOCK_STATUSES:
            return jsonify({"error": "That's not a valid stock status."}), 400
        product.stock_status = status
    if "name" in body and (body.get("name") or "").strip():
        product.name = body["name"].strip()[:200]
    if "price" in body and (body.get("price") or "").strip():
        product.price = body["price"].strip()[:120]
    if "description" in body:
        product.description = (body.get("description") or "").strip() or None

    db.session.commit()
    return jsonify(product.to_dict())


@app.route("/api/shop/products/<product_id>", methods=["DELETE"])
@login_required
def api_shop_product_delete(product_id):
    shop = _shop_for_current_user()
    if not shop:
        return jsonify({"error": "No store found."}), 404
    product = ShopProduct.query.filter_by(id=product_id, shop_id=shop.id).first()
    if product:
        db.session.delete(product)
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/shop/browse")
@login_required
def api_shop_browse():
    """Business-card listings for the Browse tab. Excludes the viewer's own
    store (they manage that from the Sell tab instead) and shops with zero
    products (nothing to see yet)."""
    q = (request.args.get("q") or "").strip().lower()
    uid = session["user_id"]
    shops = Shop.query.order_by(Shop.created_at.desc()).all()
    cards = []
    for shop in shops:
        if shop.user_id == uid:
            continue
        count = ShopProduct.query.filter_by(shop_id=shop.id).count()
        if count == 0:
            continue
        if q and q not in (shop.name or "").lower() and q not in (shop.category or "").lower() and q not in (shop.description or "").lower():
            continue
        cards.append(shop.to_dict(product_count=count))
    return jsonify({"shops": cards})


@app.route("/store/<slug>")
def view_store(slug):
    """The seller's mini-site — public, no login required, so the link
    works for anyone it's shared with. Buyers can't check out here (no
    payment yet); they browse the seller's photos/prices and send a DM."""
    shop = Shop.query.filter_by(slug=slug).first()
    if not shop:
        return render_template("store_not_found.html"), 404
    products = ShopProduct.query.filter_by(shop_id=shop.id).order_by(ShopProduct.created_at.desc()).all()
    return render_template(
        "store.html",
        shop=shop.to_dict(product_count=len(products)),
        products=[p.to_dict() for p in products],
        csrf_token_value=generate_csrf(),
    )


def _save_chat_image_if_present():
    """Shared by every chat-send route. Returns (path_or_None, error_or_None)."""
    image_file = request.files.get("image")
    if not image_file or not image_file.filename:
        return None, None
    return save_uploaded_shop_image(image_file, max_bytes=MAX_SCAN_IMAGE_BYTES)


# --- Buyer side: no login required, identity is a random token kept in the
# buyer's browser (localStorage) instead of a phone number ------------------
@app.route("/api/shop/<slug>/chat/start", methods=["POST"])
@limiter.limit("15 per minute")
def api_shop_chat_start(slug):
    """First message of a new conversation. Mints a buyer_token the client
    stores locally and sends back on every future visit to keep seeing the
    same thread — no account, no phone number, no WhatsApp handoff needed."""
    shop = Shop.query.filter_by(slug=slug).first()
    if not shop:
        return jsonify({"error": "That store doesn't exist."}), 404

    name = (request.form.get("name") or "").strip()
    message = (request.form.get("message") or "").strip()
    product_id = (request.form.get("product_id") or "").strip() or None
    product_name = (request.form.get("product_name") or "").strip() or None

    if not name:
        return jsonify({"error": "Add your name so the seller knows who's messaging."}), 400
    if len(name) > 160:
        return jsonify({"error": "That name is a bit long."}), 400
    if len(message) > 2000:
        return jsonify({"error": "That message is a bit long — keep it under 2000 characters."}), 400

    image_path, upload_error = _save_chat_image_if_present()
    if upload_error:
        return jsonify({"error": upload_error}), 400
    if not message and not image_path:
        return jsonify({"error": "Write a message or attach a photo."}), 400

    convo = ShopConversation(
        shop_id=shop.id, product_id=product_id, product_name=product_name,
        buyer_name=name, buyer_token=secrets.token_hex(20), seller_unread=1,
    )
    db.session.add(convo)
    db.session.flush()
    msg = ShopMessage(conversation_id=convo.id, sender="buyer", body=message or None, image=image_path)
    db.session.add(msg)
    db.session.commit()
    return jsonify({"buyerToken": convo.buyer_token, "conversation": convo.to_dict(msg), "messages": [msg.to_dict()]})


@app.route("/api/shop/<slug>/chat/<buyer_token>/unread")
def api_shop_chat_unread_peek(slug, buyer_token):
    """Lightweight, non-destructive check used to badge the chat launcher
    while the panel is closed — unlike the endpoint below, this does NOT
    clear buyer_unread, so it's safe to poll in the background."""
    shop = Shop.query.filter_by(slug=slug).first()
    if not shop:
        return jsonify({"error": "That store doesn't exist."}), 404
    convo = ShopConversation.query.filter_by(shop_id=shop.id, buyer_token=buyer_token).first()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404
    return jsonify({"buyerUnread": convo.buyer_unread or 0})


@app.route("/api/shop/<slug>/chat/<buyer_token>")
def api_shop_chat_thread(slug, buyer_token):
    """A buyer re-opening the store page polls this with their saved token
    to load message history and any reply the seller sent."""
    shop = Shop.query.filter_by(slug=slug).first()
    if not shop:
        return jsonify({"error": "That store doesn't exist."}), 404
    convo = ShopConversation.query.filter_by(shop_id=shop.id, buyer_token=buyer_token).first()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404
    convo.buyer_unread = 0
    db.session.commit()
    messages = ShopMessage.query.filter_by(conversation_id=convo.id).order_by(ShopMessage.created_at.asc()).all()
    return jsonify({"conversation": convo.to_dict(), "messages": [m.to_dict() for m in messages]})


@app.route("/api/shop/<slug>/chat/<buyer_token>/messages", methods=["POST"])
@limiter.limit("30 per minute")
def api_shop_chat_buyer_send(slug, buyer_token):
    shop = Shop.query.filter_by(slug=slug).first()
    if not shop:
        return jsonify({"error": "That store doesn't exist."}), 404
    convo = ShopConversation.query.filter_by(shop_id=shop.id, buyer_token=buyer_token).first()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404

    message = (request.form.get("message") or "").strip()
    if len(message) > 2000:
        return jsonify({"error": "That message is a bit long — keep it under 2000 characters."}), 400
    image_path, upload_error = _save_chat_image_if_present()
    if upload_error:
        return jsonify({"error": upload_error}), 400
    if not message and not image_path:
        return jsonify({"error": "Write a message or attach a photo."}), 400

    msg = ShopMessage(conversation_id=convo.id, sender="buyer", body=message or None, image=image_path)
    db.session.add(msg)
    convo.last_message_at = datetime.utcnow()
    convo.seller_unread = (convo.seller_unread or 0) + 1
    db.session.commit()
    return jsonify(msg.to_dict())


# --- Seller side: authenticated, lives in the dashboard's Marketplace tab --
@app.route("/api/shop/conversations")
@login_required
def api_shop_conversations():
    shop = _shop_for_current_user()
    if not shop:
        return jsonify({"conversations": []})
    convos = ShopConversation.query.filter_by(shop_id=shop.id).order_by(ShopConversation.last_message_at.desc()).all()
    out = []
    for c in convos:
        last = ShopMessage.query.filter_by(conversation_id=c.id).order_by(ShopMessage.created_at.desc()).first()
        out.append(c.to_dict(last))
    return jsonify({"conversations": out})


@app.route("/api/shop/conversations/<int:convo_id>/messages")
@login_required
def api_shop_conversation_messages(convo_id):
    shop = _shop_for_current_user()
    convo = ShopConversation.query.filter_by(id=convo_id, shop_id=shop.id if shop else -1).first()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404
    convo.seller_unread = 0
    db.session.commit()
    messages = ShopMessage.query.filter_by(conversation_id=convo.id).order_by(ShopMessage.created_at.asc()).all()
    return jsonify({"conversation": convo.to_dict(), "messages": [m.to_dict() for m in messages]})


@app.route("/api/shop/conversations/<int:convo_id>/messages", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def api_shop_conversation_reply(convo_id):
    shop = _shop_for_current_user()
    convo = ShopConversation.query.filter_by(id=convo_id, shop_id=shop.id if shop else -1).first()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404

    message = (request.form.get("message") or "").strip()
    if len(message) > 2000:
        return jsonify({"error": "That message is a bit long — keep it under 2000 characters."}), 400
    image_path, upload_error = _save_chat_image_if_present()
    if upload_error:
        return jsonify({"error": upload_error}), 400
    if not message and not image_path:
        return jsonify({"error": "Write a message or attach a photo."}), 400

    msg = ShopMessage(conversation_id=convo.id, sender="seller", body=message or None, image=image_path)
    db.session.add(msg)
    convo.last_message_at = datetime.utcnow()
    convo.buyer_unread = (convo.buyer_unread or 0) + 1
    db.session.commit()
    return jsonify(msg.to_dict())


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_mode)