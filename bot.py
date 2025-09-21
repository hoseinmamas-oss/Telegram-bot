# bot.py
import os
import asyncio
import tempfile
import subprocess
from pathlib import Path
import logging
import httpx
import base64
import json
import time
from datetime import datetime, timedelta
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# try import yt_dlp
try:
    import yt_dlp
except Exception:
    yt_dlp = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tgbot")

# ---------- تنظیمات از محیط (هیچ توکنی هاردکد نکن)
TELEGRAM_BOT_TOKEN = os.getenv("7506339947:AAG-2OyscYMvVXrUzwYsw0-anA1VYqTdPDU")
YOUTUBE_API_KEY = os.getenv("AIzaSyBEXaveIVj5w7dFDiDP-J1rGp7ES77LZP8")       # برای metadata YouTube
SPOTIFY_CLIENT_ID = os.getenv("ebc4362782aa4bebbbbfe6ff0a0cdbea")
SPOTIFY_CLIENT_SECRET = os.getenv("ac08c1705e7442748091bba9024cd6f7")
AUDD_API_TOKEN = os.getenv("Cba430843da4f18b5ca0642acd58cc45")         # برای شناسایی آهنگ (اختیاری)
TMDB_API_KEY = os.getenv("438ca39296232891f7ded4b3acd540d4")             # برای جستجوی فیلم/سریال
GOOGLE_VISION_API_KEY = os.getenv("AIzaSyCx5kykYKzzxfpG0CAOrz1MpJv7Nqilg1E")  # optional

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required in env variables")

# temp dir
TMP = Path(tempfile.gettempdir()) / "tgbot_files"
TMP.mkdir(parents=True, exist_ok=True)

# consent store file (persistent)
CONSENT_FILE = TMP / "consent.json"
DOWNLOAD_LOG = TMP / "downloads.log"
# load consents
if CONSENT_FILE.exists():
    try:
        with open(CONSENT_FILE, "r", encoding="utf-8") as f:
            CONSENTS = json.load(f)
    except Exception:
        CONSENTS = {}
else:
    CONSENTS = {}

# in-memory cooldown (seconds)
USER_COOLDOWN = {}
COOLDOWN_SECONDS = 30  # هر کاربر حداقل 30 ثانیه بین دانلودها

# ---------------- platform detection (simple)
import re
YOUTUBE_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_\-]+)")
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)")
TIKTOK_RE = re.compile(r"tiktok\.com|vm\.tiktok\.com")
INSTAGRAM_RE = re.compile(r"instagram\.com|instagr\.am")
PINTEREST_RE = re.compile(r"pinterest\.com")

def detect_platform(url: str) -> str:
    if YOUTUBE_RE.search(url): return "youtube"
    if SPOTIFY_RE.search(url): return "spotify"
    if TIKTOK_RE.search(url): return "tiktok"
    if INSTAGRAM_RE.search(url): return "instagram"
    if PINTEREST_RE.search(url): return "pinterest"
    return "unknown"

def save_consents():
    try:
        with open(CONSENT_FILE, "w", encoding="utf-8") as f:
            json.dump(CONSENTS, f)
    except Exception as e:
        logger.exception("Failed to save consents: %s", e)

def log_download(user_id: int, url: str, info: dict):
    try:
        with open(DOWNLOAD_LOG, "a", encoding="utf-8") as f:
            entry = {
                "time": datetime.utcnow().isoformat() + "Z",
                "user_id": user_id,
                "url": url,
                "info": info
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

# ---------------- YouTube metadata
# (same as before) ...
async def youtube_metadata(video_id: str):
    if not YOUTUBE_API_KEY:
        return None
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"id": video_id, "part":"snippet,contentDetails","key":YOUTUBE_API_KEY}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params, timeout=15)
        if r.status_code!=200:
            return None
        j = r.json()
        items = j.get("items") or []
        if not items:
            return None
        s = items[0]["snippet"]
        return {
            "title": s.get("title"),
            "channel": s.get("channelTitle"),
            "desc": s.get("description"),
            "thumbnail": s.get("thumbnails", {}).get("high", {}).get("url")
        }

# ---------------- Spotify functions (same as your code)
async def spotify_token():
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    token_url = "https://accounts.spotify.com/api/token"
    auth = (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
    async with httpx.AsyncClient() as client:
        r = await client.post(token_url, data={"grant_type":"client_credentials"}, auth=auth, timeout=10)
        if r.status_code!=200:
            return None
        return r.json().get("access_token")

async def spotify_search_track(q: str, limit=5):
    token = await spotify_token()
    if not token:
        return []
    url = "https://api.spotify.com/v1/search"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": q, "type":"track", "limit": limit}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers, params=params, timeout=10)
        if r.status_code!=200:
            return []
        j = r.json()
        tracks = []
        for t in j.get("tracks", {}).get("items", []):
            tracks.append({
                "name": t["name"],
                "artists": ", ".join([a["name"] for a in t["artists"]]),
                "preview_url": t.get("preview_url"),
                "external_url": t["external_urls"]["spotify"],
                "album_cover": t["album"]["images"][0]["url"] if t["album"]["images"] else None
            })
        return tracks

# ---------------- TMDb search (for images -> title)
async def tmdb_search(query: str):
    if not TMDB_API_KEY:
        return []
    url = "https://api.themoviedb.org/3/search/multi"
    params = {"api_key": TMDB_API_KEY, "query": query}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params, timeout=10)
        if r.status_code!=200:
            return []
        j = r.json()
        results = []
        for it in j.get("results", [])[:6]:
            results.append({
                "id": it.get("id"),
                "title": (it.get("title') or it.get('name') if False else (it.get('title') or it.get('name'))),
                "media_type": it.get("media_type"),
                "overview": it.get("overview"),
                "poster": f"https://image.tmdb.org/t/p/w500{it.get('poster_path')}" if it.get("poster_path") else None
            })
        return results

# -------------- Google Vision (webDetection + labels)
async def google_vision_detect(image_bytes: bytes):
    if not GOOGLE_VISION_API_KEY:
        return []
    url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "requests":[
            {"image":{"content": img_b64},
             "features":[{"type":"WEB_DETECTION","maxResults":5},{"type":"LABEL_DETECTION","maxResults":5}]
            }
        ]
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, timeout=20)
        if r.status_code!=200:
            return []
        j = r.json()
        resp = j.get("responses", [{}])[0]
        labels = [c.get("description") for c in resp.get("labelAnnotations", [])]
        web = [g.get("label") for g in resp.get("webDetection", {}).get("bestGuessLabels", [])]
        return list(dict.fromkeys(labels + web))

# ---------------- ffmpeg convert (file path -> mp3 path)
def convert_to_mp3(src_path: Path, out_path: Path) -> bool:
    # requires ffmpeg installed on system
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src_path),
        "-vn", "-acodec", "libmp3lame", "-ab", "192k",
        str(out_path)
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out_path.exists()
    except Exception as e:
        logger.exception("ffmpeg failed: %s", e)
        return False

# ---------------- AudD recognition
def audd_recognize(file_path: Path):
    if not AUDD_API_TOKEN:
        return None
    url = "https://api.audd.io/"
    with open(file_path, "rb") as fh:
        files = {"file": fh}
        data = {"api_token": AUDD_API_TOKEN, "return":"spotify"}
        r = httpx.post(url, data=data, files=files, timeout=30)
    if r.status_code != 200:
        return None
    j = r.json()
    if j.get("status") == "success" and j.get("result"):
        return j["result"]
    return None

# ---------------- yt-dlp download helper (blocking) ----------------
def ytdlp_download_blocking(url: str, outdir: Path):
    """
    Returns a dict with keys: success(bool), filepath(str|None), info(dict|None), error(str|None)
    """
    if yt_dlp is None:
        return {"success": False, "error": "yt_dlp not installed"}
    outdir.mkdir(parents=True, exist_ok=True)
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # prepare filename
            filename = ydl.prepare_filename(info)
            return {"success": True, "filepath": filename, "info": info}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ---------------- Handlers
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! من رباتِ تست هستم.\n\n• اگر مالک محتوا هستی یا مجوز داری، اول /confirm_owner رو بزن.\n• سپس /download <url> بفرست تا دانلود و تبدیل انجام شود (در صورت امکان و اگر فایل با سایز مجاز باشد ارسال می‌شود).\n\nتوجه: استفادهٔ ناصحیح ممکن است نقض کپی‌رایت یا شرایط پلتفرم باشد — مسئولیت با شماست."
    )

# /confirm_owner -> ثبت رضایت کاربر برای 24 ساعت
async def confirm_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    expiry = (datetime.utcnow() + timedelta(hours=24)).isoformat() + "Z"
    CONSENTS[user_id] = {"granted_at": datetime.utcnow().isoformat() + "Z", "expires_at": expiry}
    save_consents()
    await update.message.reply_text(
        "✅ تایید شما ثبت شد. شما ۲۴ ساعت اجازهٔ استفاده برای دانلودهایی که مالک یا مجوزشان را دارید دارید.\n"
        "حالا می‌توانید از دستور /download <url> استفاده کنید."
    )

def has_consent(user_id: int) -> bool:
    entry = CONSENTS.get(str(user_id))
    if not entry:
        return False
    try:
        exp = datetime.fromisoformat(entry["expires_at"].replace("Z",""))
        return datetime.utcnow() <= exp
    except Exception:
        return False

# /download <url>
async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = time.time()
    # cooldown
    last = USER_COOLDOWN.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        await update.message.reply_text(f"⏳ لطفاً {int(COOLDOWN_SECONDS - (now-last))} ثانیه دیگر تلاش کنید (محدودیت).")
        return
    USER_COOLDOWN[user_id] = now

    if not has_consent(user_id):
        await update.message.reply_text(
            "❗ شما هنوز تایید مالکیت را ثبت نکرده‌اید. لطفاً ابتدا /confirm_owner را بزنید."
        )
        return

    if not context.args:
        await update.message.reply_text("فرمول: /download <url>")
        return
    url = context.args[0].strip()
    platform = detect_platform(url)
    await update.message.reply_text(f"⏳ در حال دانلود (با yt-dlp) — لطفا صبر کن. پلتفرم تشخیص‌داده‌شده: {platform}")

    outdir = TMP / f"dl_{user_id}_{int(time.time())}"
    outdir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, ytdlp_download_blocking, url, outdir)

    if not result.get("success"):
        await update.message.reply_text(f"❌ دانلود ناموفق بود: {result.get('error')}")
        # cleanup
        try:
            for p in outdir.iterdir():
                p.unlink()
            outdir.rmdir()
        except Exception:
            pass
        return

    filepath = result.get("filepath")
    info = result.get("info") or {}
    log_download(user_id, url, {"filepath": filepath, "info": {"title": info.get("title")}})
    # send video if small enough
    try:
        size = os.path.getsize(filepath)
    except Exception:
        size = 0

    MAX_SEND_SIZE = 45 * 1024 * 1024  # 45 MB
    try:
        if size > 0 and size <= MAX_SEND_SIZE:
            # send video
            await update.message.reply_video(video=open(filepath, "rb"))
        else:
            await update.message.reply_text("ویدیو بزرگ است یا مشکل ارسال ویدیو وجود دارد — فقط فایل صوتی را ارسال می‌کنم.")
    except Exception as e:
        await update.message.reply_text(f"خطا در ارسال ویدیو: {e}")

    # تبدیل به mp3
    mp3_out = outdir / f"{Path(filepath).stem}.mp3"
    converted = await loop.run_in_executor(None, convert_to_mp3, Path(filepath), mp3_out)

    if converted and mp3_out.exists():
        # ارسال صوتی (به عنوان audio)
        try:
            await update.message.reply_audio(audio=open(mp3_out, "rb"))
        except Exception as e:
            await update.message.reply_text(f"خطا در ارسال فایل صوتی: {e}")
        # شناسایی AudD (اختیاری)
        if AUDD_API_TOKEN:
            await update.message.reply_text("🔎 در حال شناسایی آهنگ با AudD...")
            try:
                res = await loop.run_in_executor(None, audd_recognize, mp3_out)
                if res:
                    title = res.get("title","نامشخص")
                    artist = res.get("artist","نامشخص")
                    reply = f"🎵 تشخیص داده شد:\nآهنگ: {title}\nخواننده: {artist}"
                    spotify = res.get("spotify")
                    if spotify and spotify.get("external_urls", {}).get("spotify"):
                        reply += f"\n🔗 Spotify: {spotify['external_urls']['spotify']}"
                    await update.message.reply_text(reply)
                else:
                    await update.message.reply_text("نتیجه‌ای در AudD پیدا نشد.")
            except Exception as e:
                await update.message.reply_text(f"خطا در تماس با AudD: {e}")
    else:
        await update.message.reply_text("تبدیل به mp3 انجام نشد یا فایل mp3 ایجاد نشد.")

    # cleanup
    try:
        # remove all files in outdir
        for p in outdir.iterdir():
            try:
                p.unlink()
            except Exception:
                pass
        try:
            outdir.rmdir()
        except Exception:
            pass
    except Exception:
        pass

# ---------------- other handlers (image, text, audio handlers) ----------------
# reuse your existing handlers: text_handler, doc_audio_handler, image_handler, search_command
# (You can keep the versions you already have in your file. For brevity, re-use them if present.)

# If you have the definitions in this file already, they will be used.
# Otherwise, define minimal fallbacks:

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    platform = detect_platform(txt)
    if platform == "youtube":
        m = YOUTUBE_RE.search(txt)
        if m:
            vid = m.group(1)
            meta = await youtube_metadata(vid)
            if meta:
                msg = f"🎬 <b>{meta['title']}</b>\nChannel: {meta['channel']}\n\n{(meta['desc'] or '')[:300]}...\n\n🔗 https://www.youtube.com/watch?v={vid}"
                if meta.get("thumbnail"):
                    await update.message.reply_photo(meta["thumbnail"], caption=msg, parse_mode="HTML")
                else:
                    await update.message.reply_text(msg, parse_mode="HTML")
                return
    elif platform == "spotify":
        m = SPOTIFY_RE.search(txt)
        if m:
            kind, sid = m.group(1), m.group(2)
            if kind == "track":
                tracks = await spotify_search_track(sid, limit=1)
                if tracks:
                    t = tracks[0]
                    caption = f"🎵 <b>{t['name']}</b>\nArtist(s): {t['artists']}\n\n🔗 {t['external_url']}"
                    if t.get("album_cover"):
                        await update.message.reply_photo(t["album_cover"], caption=caption, parse_mode="HTML")
                    else:
                        await update.message.reply_text(caption, parse_mode="HTML")
                    return
    elif platform in ("tiktok","instagram","pinterest"):
        await update.message.reply_text(
            f"پلتفرم تشخیص داده شد: {platform}. اگر مالک محتوا هستید می‌توانید /confirm_owner را بزنید و سپس /download <url> را ارسال کنید."
        )
        return
    else:
        await update.message.reply_text("لینک شناسایی نشد. می‌تونی از /search <song name> برای جستجوی آهنگ استفاده کنی یا فایل صوتی آپلود کن.")

async def doc_audio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # همان کد شما برای فایل‌های آپلودی (بدون yt-dlp)
    msg = await update.message.reply_text("⏳ دریافت فایل و پردازش...")
    file_obj = None
    ext = "bin"
    if update.message.voice:
        file_obj = await update.message.voice.get_file()
        ext = "ogg"
    elif update.message.audio:
        file_obj = await update.message.audio.get_file()
        fn = update.message.audio.file_name or f"audio_{update.message.message_id}.mp3"
        ext = Path(fn).suffix.lstrip(".") or "mp3"
    elif update.message.document:
        file_obj = await update.message.document.get_file()
        fn = update.message.document.file_name or f"doc_{update.message.message_id}.bin"
        ext = Path(fn).suffix.lstrip(".") or "bin"
    else:
        await update.message.reply_text("فایل پشتیبانی نشده.")
        return

    local_in = TMP / f"{update.message.message_id}_in.{ext}"
    await file_obj.download_to_drive(str(local_in))

    # تبدیل به mp3 (اختیاری، برای AudD بهتره)
    local_mp3 = TMP / f"{update.message.message_id}.mp3"
    converted = convert_to_mp3(local_in, local_mp3)
    target_for_audd = local_mp3 if converted else local_in

    # شناسایی با AudD (در صورت تنظیم توکن)
    if AUDD_API_TOKEN:
        await update.message.reply_text("🔎 در حال شناسایی آهنگ با AudD...")
        try:
            res = await asyncio.get_event_loop().run_in_executor(None, audd_recognize, target_for_audd)
            if res:
                title = res.get("title","نامشخص")
                artist = res.get("artist","نامشخص")
                reply = f"🎵 تشخیص داده شد:\nآهنگ: {title}\nخواننده: {artist}"
                spotify = res.get("spotify")
                if spotify and spotify.get("external_urls", {}).get("spotify"):
                    reply += f"\n🔗 Spotify: {spotify['external_urls']['spotify']}"
                await update.message.reply_text(reply)
            else:
                await update.message.reply_text("نتیجه‌ای در AudD پیدا نشد.")
        except Exception as e:
            await update.message.reply_text(f"خطا در تماس با AudD: {e}")
    else:
        await update.message.reply_text("AUDD_API_TOKEN تنظیم نشده — شناسایی غیرفعال است.")

    # پاک‌سازی فایل‌ها
    try:
        local_in.unlink()
    except: pass
    try:
        local_mp3.unlink()
    except: pass
    await msg.delete()

async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ تصویر دانلود و بررسی می‌شود...")
    photo = update.message.photo[-1]
    f = await photo.get_file()
    local = TMP / f"{update.message.message_id}_img.jpg"
    await f.download_to_drive(str(local))
    with open(local, "rb") as fh:
        img_bytes = fh.read()
    labels = await google_vision_detect(img_bytes) if GOOGLE_VISION_API_KEY else []
    candidates = labels[:4] if labels else []
    results = []
    # search tmdb for each candidate
    for c in candidates:
        r = await tmdb_search(c)
        if r:
            results.extend(r)
    if results:
        text = "<b>ممکنه این تصویر مربوط باشه به:</b>\n\n"
        for it in results[:5]:
            title = it.get("title") or "نامشخص"
            text += f"• <b>{title}</b> ({it.get('media_type')})\n{(it.get('overview') or '')[:160]}...\n"
            if it.get("poster"):
                text += f"{it.get('poster')}\n"
            text += "\n"
        await update.message.reply_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text("نتوانستم تشخیصی بدهم. اگر اسم یا سرنخی داری بنویس تا جستجو کنم.")
    try:
        local.unlink()
    except: pass
    await msg.delete()

# /search command (same as before)
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args or [])
    if not q:
        await update.message.reply_text("فرمول: /search <song name> — برای جستجوی آهنگ در Spotify")
        return
    await update.message.reply_text(f"در حال جستجوی «{q}» در Spotify...")
    tracks = await spotify_search_track(q, limit=5)
    if not tracks:
        await update.message.reply_text("نتیجه‌ای پیدا نشد یا Spotify API تنظیم نشده.")
        return
    for t in tracks:
        txt = f"🎵 <b>{t['name']}</b>\nArtist(s): {t['artists']}\n🔗 {t['external_url']}\n"
        if t.get("preview_url"):
            txt += f"Preview: {t['preview_url']}\n"
        if t.get("album_cover"):
            await update.message.reply_photo(t["album_cover"], caption=txt, parse_mode="HTML")
        else:
            await update.message.reply_text(txt, parse_mode="HTML")

# ---------------- main
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("confirm_owner", confirm_owner))
    app.add_handler(CommandHandler("download", download_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, image_handler))
    app.add_handler(MessageHandler((filters.VOICE | filters.AUDIO | filters.Document.ALL) & ~filters.COMMAND, doc_audio_handler))
    logger.info("Bot starting (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
