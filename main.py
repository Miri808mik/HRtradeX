import os
import re
import json
import time
import random
import threading
import subprocess
import sys
import asyncio
from contextlib import suppress
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque
from urllib.parse import parse_qs, urlparse

import aiohttp
from highrise import BaseBot, SessionMetadata, User, Position, AnchorPosition, Error, Item


class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, format, *args):
        pass


def run_fake_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


HIGHRS_LINK_RE = re.compile(r"(https?://(?:www\.)?high\.rs/\S+)")


def extract_emote_id(raw_value: str):
    value = raw_value.strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if "high.rs" not in parsed.netloc:
            return None
        query = parse_qs(parsed.query)
        emote_id = query.get("id", [None])[0]
        return emote_id.strip() if emote_id else None
    if value.startswith(("high.rs/", "www.high.rs/")):
        return extract_emote_id("https://" + value)
    if " " in value:
        return None
    return value


def extract_item_id(raw_value: str):
    """
    آیدی آیتم رو از یه لینک high.rs/item?id=... استخراج می‌کنه، یا اگه خودِ آیدی خام بود همونو برمی‌گردونه.
    مثال لینک: https://high.rs/item?id=sock-n_starteritems2020whitekneelength&type=clothing
    """
    value = raw_value.strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if "high.rs" not in parsed.netloc:
            return None
        query = parse_qs(parsed.query)
        item_id = query.get("id", [None])[0]
        return item_id.strip() if item_id else None
    if value.startswith(("high.rs/", "www.high.rs/")):
        return extract_item_id("https://" + value)
    if " " in value:
        return None
    return value


def _is_positive_float(value: str) -> bool:
    try:
        return float(value) > 0
    except ValueError:
        return False


def extract_username_from_link(raw_value: str):
    """از لینکی مثل high.rs/user?name=SyntaxError.py یوزرنیم رو درمیاره، یا اگه خام بود همونو برمی‌گردونه."""
    value = raw_value.strip().lstrip("@")
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if "high.rs" not in parsed.netloc:
            return None
        query = parse_qs(parsed.query)
        name = query.get("name", [None])[0]
        return name.strip() if name else None
    if value.startswith(("high.rs/", "www.high.rs/")):
        return extract_username_from_link("https://" + value)
    if " " in value:
        return None
    return value


def find_dance_in_text(text: str):
    """دنبال لینک high.rs یا آیدی خام (مثل dance-xxx) هر جای متن می‌گرده، حتی وسط یه جمله."""
    match = HIGHRS_LINK_RE.search(text)
    if match:
        return extract_emote_id(match.group(1))
    # آیدی خام بدون فاصله که با dance- یا emote- یا idle- شروع بشه
    bare_match = re.search(r"\b(dance-[\w-]+|emote-[\w-]+|idle-[\w-]+)\b", text)
    if bare_match:
        return bare_match.group(1)
    return None


# تشخیص نیت «بیا اینجا/کنارم/پیشم» با کلمه‌کلیدی، نه با AI (سریع‌تر و بدون هزینه)
COME_HERE_RE = re.compile(r"بیا(ی|ید)?\b")


def is_come_here_request(text: str) -> bool:
    return bool(COME_HERE_RE.search(text))


DANCE_REPLIES = [
    "بیا اینم دنست 💃",
    "اینم رقصی که خواستی ✨",
    "بفرما، اجرا شد 🕺",
]

EXTRA_OWNER_USERNAMES = {"syntaxerror.py"}
DAILY_AI_LIMIT = 20

# --- مناطق VIP ---
# هروقت !ثبت VIP گفتی، بات مختصات همون‌جا رو توی چت میگه.
# همون مختصات رو (x, y, z) این‌جا به‌صورت یه خط جدید اضافه کن، همین‌جا، جای دیگه‌ای لازم نیست.
# مثال: {"x": 12.5, "y": 0.0, "z": 8.0, "radius": 3.0}
VIP_ZONES = [
    # {"x": 0.0, "y": 0.0, "z": 0.0, "radius": 3.0},
]

# --- تشخیص دعوای واقعی (فقط وقتی کسی #گزارش بزنه، نه همیشه - برای صرفه‌جویی توکن) ---
REPORT_WINDOW_SECONDS = 3 * 60       # ۳ دقیقه زیرنظر می‌گیره
MUTE_SECONDS = 2 * 60 * 60           # ۲ ساعت (واحد action_length رو مطمئن نیستیم، احتمالاً ثانیه‌ست)

# --- رفتار وقتی بیکاره ---
IDLE_CHECK_INTERVAL = (120, 240)    # هر ۲ تا ۴ دقیقه یه‌بار چک کن کسی تنها هست یا نه
LONELY_RESPONSE_TIMEOUT = 150       # ۲.۵ دقیقه صبر کن ببین جواب میده یا نه

# --- اولویت بین دو نفر ---
ACTIVE_CONVO_WINDOW = 120           # اگه ظرف ۲ دقیقه‌ی اخیر باهاش حرف زده بودیم، یعنی "مشغولیم"
NUDGE_TIMEOUT = 15                  # چقدر صبر کنیم ببینیم نفر اول اعتراض می‌کنه یا نه

# برای فهمیدن «کی نزدیک کیه»: شعاع (واحد مختصات Highrise) و بازه‌ی زمانی که پیام‌ها معتبرن
# این عدد حدسیه، چون Highrise مقیاس دقیق فاصله رو مستند نکرده - نیاز به تست و تنظیم داره
NEARBY_RADIUS = 5.0
CONTEXT_WINDOW_SECONDS = 180  # ۳ دقیقه
MAX_CONTEXT_MESSAGES = 15

# آدرس عمومی سرویس روی Render، برای بیدار نگه‌داشتنش
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://hrtradex.onrender.com")

# آدرس API حافظه‌ی بلندمدت (همون فایل‌های PHP روی هاست)
DB_API_URL = os.environ.get("DB_API_URL", "").rstrip("/")
DB_API_SECRET = os.environ.get("DB_API_SECRET", "")
DATASET_BATCH_SIZE = 100

# --- سیستم مدیریت دنس (جدول + لوپ) ---
DANCES_FILE = "dances.json"
LOOP_EARLY_SECONDS = 0.25  # این عدد رو خودت کم‌وزیاد کن (مثلاً 0.1 یا 0.2) بسته به سرعت اینترنت بات

# مقادیر پیش‌فرض؛ فقط اگه فایل dances.json پیدا نشد استفاده میشن
# این لیست از یه دیتاست واقعی (نه حدسی) استخراج شده - ۳۸۲ ایموت/دنس با duration دقیق
# اگه دیدی یکیش هنوز اشتباهه، با !دنس <emote_id> <ثانیه> دستی اصلاحش کن
DEFAULT_DANCES = {
    "emote-shy2": 4.8,
    "idle-loop-aerobics": 8.5,
    "emote-laughing2": 5.1,
    "emoji-angry": 6.4,
    "dance-anime3": 10.5,
    "dance-anime": 7.8,
    "idle-loop-annoyed": 17.1,
    "emote-apart": 4.8,
    "emote-armcannon": 7.3,
    "emoji-arrogance": 6.9,
    "emote-astronaut": 14.4,
    "emote-salute": 3.0,
    "emote-attention": 4.4,
    "idle_layingdown": 24.6,
    "emote-baseball": 7.3,
    "dance-blackpink": 6.4,
    "emote-disappear": 6.2,
    "emote-boo": 4.5,
    "emote-bow": 2.8,
    "emote-boxer": 6.6,
    "dance-breakdance": 17.6,
    "profile-breakscreen": 10.9,
    "idle-loop-sad": 6.1,
    "emote-bunnyhop": 12.4,
    "emote-cartwheel": 5.0,
    "idle-dance-casual": 8.8,
    "emote-swing-celebrate": 1.6,
    "emote-swing-celebrate-idle": 1.6,
    "emoji-celebrate": 3.0,
    "emote-celebrationstep": 3.0,
    "emote-charging": 7.6,
    "idle-loop-happy": 18.8,
    "emoji-clapping": 2.2,
    "emote-fail2": 6.5,
    "idle-cold": 16.7,
    "emote-cold": 3.7,
    "emote-death2": 4.9,
    "emote-confused": 8.1,
    "emote-coolguy": 4.0,
    "idle-floorsleeping": 13.9,
    "emote-creepycute": 7.3,
    "dance-creepypuppet": 5.9,
    "emoji-crying": 3.7,
    "emoji-cursing": 2.4,
    "emote-curtsy": 2.2,
    "emote-cute": 3.3,
    "emote-cutesalute": 2.4,
    "emote-cutey": 2.7,
    "emote-dab": 2.7,
    "dance-zombie": 12.9,
    "emote-dinner": 12.3,
    "emote-disco": 5.4,
    "emoji-dizzy": 4.1,
    "dance-tiktok13": 7.7,
    "emote-dramatic": 7.7,
    "emote-fail3": 6.7,
    "dance-duckwalk": 11.7,
    "emote-elbowbump": 3.8,
    "emote-electrified": 4.3,
    "emote-embarrassed": 7.4,
    "emote-energyball": 7.2,
    "idle-enthusiastic": 15.2,
    "emote-exasperated": 2.4,
    "emoji-eyeroll": 3.0,
    "emote-exasperatedb": 2.7,
    "emote-fading": 12.6,
    "emote-fainting": 18.4,
    "emote-deathdrop": 3.8,
    "idle-floating": 26.1,
    "emote-looping": 8.6,
    "emote-fail1": 5.6,
    "emote-fashionista": 5.1,
    "idle-dance-headbobbing": 25.4,
    "emote-hot": 4.4,
    "idle-fighter": 17.2,
    "emoji-hadoken": 2.7,
    "emote-fireworks": 11.4,
    "fishing-cast": 1.5,
    "fishing-idle": 16.0,
    "fishing-pull": 1.0,
    "fishing-pull-small": 1.0,
    "emoji-flex": 2.1,
    "emote-flirt": 6.6,
    "emote-float": 8.1,
    "dance-floss": 21.3,
    "dance-fruity": 17.6,
    "emote-frog": 14.1,
    "emote-frustrated": 5.6,
    "emoji-gagging": 5.7,
    "emote-gangnam": 7.3,
    "emoji-ghost": 3.5,
    "emote-ghost-idle": 19.6,
    "emote-gift": 4.5,
    "emoji-give-up": 5.4,
    "emote-gooey": 4.8,
    "emote-gravity": 8.5,
    "emote-greedy": 4.2,
    "idle-guitar": 13.5,
    "dance-handsup": 22.3,
    "emote-handstand": 4.0,
    "emote-handwalk": 6.0,
    "emote-happy": 3.5,
    "emote-harlemshake": 13.6,
    "emote-headblowup": 11.3,
    "idle-headless": 43.9,
    "emote-hearteyes": 4.0,
    "emote-heartfingers": 4.0,
    "emote-heartshape": 6.2,
    "emote-hello": 2.5,
    "emote-hero": 5.0,
    "idle-hero": 21.9,
    "dance-hiphop": 26.3,
    "dance-hipshake": 12.0,
    "emote-hopscotch": 4.2,
    "emote-howl": 10.0,
    "idle-howl": 10.0,
    "emote-hug": 3.5,
    "emote-hugyourself": 5.0,
    "emote-hyped": 7.0,
    "dance-icecream": 6.3,
    "emote-iceskating": 7.0,
    "idle-space": 36.2,
    "idle-angry": 25.4,
    "emote-jetpack": 16.8,
    "dance-jinglebell": 10.6,
    "emote-judochop": 2.4,
    "emote-juggling": 4.4,
    "emote-jumpb": 3.6,
    "dance-martial-artist": 13.3,
    "dance-kawai": 9.7,
    "emote-kawaiigogo": 10.0,
    "dance-kid": 9.6,
    "emote-kiss": 2.4,
    "sit-idle-sleep": 17.3,
    "emote-laughing": 2.5,
    "emote-launch": 8.4,
    "emote-levelup": 6.1,
    "emoji-halo": 5.8,
    "emote-lust": 4.7,
    "emoji-lying": 6.3,
    "dance-macarena": 11.7,
    "emote-maniac": 4.5,
    "emoji-mind-blown": 2.4,
    "mining-fail": 2.8,
    "mining-mine": 3.0,
    "mining-success": 2.5,
    "emote-model": 5.9,
    "emote-monster_fail": 4.6,
    "emote-gordonshuffle": 8.1,
    "emote-pose4": 4.2,
    "emoji-naughty": 4.3,
    "idle-nervous": 22.6,
    "emote-nightfever": 5.5,
    "emote-ninjarun": 4.8,
    "emote-no": 2.3,
    "emote-oops": 6.4,
    "emote-opera": 4.6,
    "dance-orangejustice": 6.5,
    "emote-outfit": 12.2,
    "emote-outfit2": 10.5,
    "emote-panic": 2.9,
    "emote-celebrate": 3.9,
    "emote-kissing-passionate": 9.2,
    "emote-peace": 5.8,
    "emote-peekaboo": 3.6,
    "dance-pinguin": 10.8,
    "dance-pennywise": 0.7,
    "emoji-there": 2.1,
    "dance-tiktok12": 13.8,
    "idle-lookup": 22.3,
    "emote-pose1": 2.3,
    "emote-pose10": 3.5,
    "emote-pose3": 4.5,
    "emote-pose5": 4.2,
    "emote-pose7": 4.0,
    "emote-pose8": 4.8,
    "emote-pose9": 4.0,
    "emote-pose11": 3.6,
    "emote-pose12": 4.1,
    "idle-posh": 21.9,
    "idle-sad": 24.4,
    "emoji-pray": 4.5,
    "emote-proposing": 4.3,
    "emoji-punch": 1.8,
    "emote-punkguitar": 8.7,
    "emote-puppet": 16.3,
    "emote-pose2": 4.5,
    "dance-employee": 6.5,
    "dance-aerobics": 8.8,
    "emote-rainbow": 2.8,
    "emote-receive-happy": 4.3,
    "emote-receive-disappointed": 5.3,
    "idle_layingdown2": 21.5,
    "idle-floorsleeping2": 17.3,
    "emote-repose": 1.1,
    "sit-idle-cute": 16.85,
    "emote-death": 6.6,
    "emote-rifle-throw": 6.6,
    "dance-singleladies": 21.2,
    "emote-robot": 7.6,
    "dance-robotic": 17.8,
    "dance-metal": 15.1,
    "emote-rofl": 6.3,
    "emote-roll": 3.6,
    "emote-ropepull": 8.8,
    "idle_tough": 26.7,
    "run-vertical": 1.0,
    "emote-runhop": 6.5,
    "dance-russian": 9.6,
    "emote-sad": 5.4,
    "emoji-scared": 3.0,
    "idle-wild": 26.6,
    "emote-secrethandshake": 3.9,
    "dance-sexy": 12.3,
    "emote-sheephop": 2.4,
    "emote-shocked": 4.0,
    "dance-shoppingcart": 3.7,
    "emote-shrink": 8.7,
    "dance-shuffle": 7.6,
    "emoji-shush": 1.8,
    "emote-shy": 3.9,
    "idle-loop-shy": 16.5,
    "emoji-sick": 5.1,
    "idle_singing": 9.8,
    "sit-open": 26.0,
    "sit-relaxed": 29.6,
    "sit-chair": 1.7,
    "idle-loop-sitfloor": 21.7,
    "emote-slap": 2.7,
    "idle-sleep": 22.6,
    "emote-sleigh": 11.1,
    "emoji-smirking": 4.8,
    "emote-kissing-bound": 5.8,
    "dance-smoothwalk": 6.7,
    "emote-snake": 4.7,
    "emoji-sneeze": 3.0,
    "emote-snowangel": 5.5,
    "emote-snowball": 4.6,
    "emote-splitsdrop": 4.5,
    "emote-stargaze": 1.1,
    "emote-stargazer": 6.5,
    "emoji-poop": 4.8,
    "emote-sumo": 10.9,
    "emote-superpunch": 3.8,
    "emote-superrun": 6.3,
    "emote-kicking": 4.9,
    "emote-superpose": 5.3,
    "emote-surf": 17.0,
    "emote-pose6": 5.0,
    "emote-kissing": 5.0,
    "emote-swing-net": 2.6,
    "emote-swing-net-2": 2.5,
    "emote-swing-net-3": 2.0,
    "emote-swing-sneak-Idle": 2.5,
    "emote-swordfight": 5.3,
    "emote-tapdance": 11.1,
    "idle-loop-tapdance": 6.3,
    "emote-telekinesis": 9.6,
    "emote-teleporting": 11.5,
    "emote-theatrical": 8.6,
    "emote-thief": 4.6,
    "emote-think": 3.7,
    "emoji-thumbsup": 2.3,
    "emote-suckthumb": 4.2,
    "idle-dance-tiktok7": 13.0,
    "dance-tiktok10": 7.5,
    "dance-tiktok2": 10.0,
    "idle-dance-tiktok4": 15.1,
    "dance-tiktok8": 10.2,
    "dance-tiktok9": 11.5,
    "dance-tiktok1": 11.0,
    "dance-tiktok15": 13.6,
    "dance-tiktok16": 8.8,
    "dance-tiktok3": 8.9,
    "dance-tiktok5": 11.3,
    "dance-tiktok6": 10.4,
    "dance-tiktok7": 12.6,
    "emote-timejump": 3.5,
    "emote-tired": 4.4,
    "idle-loop-tired": 22.0,
    "idle-toilet": 31.9,
    "dance-touch": 11.3,
    "emote-trampoline": 15.0,
    "emote-twitched": 7.5,
    "idle-uwu": 24.8,
    "dance-voguehands": 9.2,
    "walk-vertical": 1.0,
    "emote-wave": 2.5,
    "emote-wavey": 10.7,
    "dance-weird": 21.1,
    "emote-wings": 13.1,
    "emote-pose13": 5.0,
    "dance-tiktok11": 9.2,
    "dance-wrong": 11.9,
    "emote-yes": 1.9,
    "dance-spiritual": 15.8,
    "idle_zombie": 28.8,
    "emote-zombierun": 8.7,
    "idle-dance-swinging": 13.2,
    "emote-headball": 10.1,
    "emote-graceful": 3.8,
    "emote-frollicking": 3.7,
    "emote-lagughing": 1.1,
    "dance-wild": 20.0,
    "emote-threadexchange-posing": 24.0,
    "emote-outfit3": 15.0,
    "emote-guitar": 10.0,
    "sit-cute": 15.0,
    "sit-ground-crossed": 20.0,
    "sit-ground-relaxed": 20.0,
    "emote-bloomify-pose1": 6.5,
    "emote-bloomify-pose2": 7.5,
    "emote-bloomify-pose3": 4.0,
    "emote-rainstruck-success": 5.5,
    "emote-rainstruck-fail": 6.5,
    "emote-pose-stand1": 4.0,
    "emote-pose-stand2": 5.0,
    "emote-pose-sit": 6.0,
    "emote-yoga-treePose": 5.0,
    "emote-yoga-warrior2": 5.0,
    "emote-yoga-warrior3": 5.5,
    "emote-punkandlaces-pose1": 3.5,
    "emote-punkandlaces-pose2": 3.5,
    "emote-punkandlaces-pose3": 3.5,
    "emote-sugarbite-pose1": 10.5,
    "emote-sugarbite-pose2": 10.6,
    "emote-sugarbite-pose3": 10.5,
    "emote-pose-goth1": 10.0,
    "emote-pose-goth2": 10.0,
    "emote-pose-goth3": 10.0,
    "sit-idle-springSun": 10.0,
    "emote-threadexchange-floating": 16.3,
    "emote-offdutyangels-comehere": 5.8,
    "emote-offdutyangels-backoff": 5.8,
    "emote-jewelrise-vibing": 12.3,
    "emote-idle-daydreaming": 11.4,
    "dance-cheerleader": 11.5,
    "dance-tiktok14": 10.5,
    "idle-phone": 13.0,
    "idle-phone-camera": 11.0,
    "idle-laying-phone-talking": 22.0,
    "dance-ballet": 7.8,
    "dance-true-heart": 11.0,
    "dance-mine": 11.0,
    "dance-popularvibe": 11.0,
    "dance-swagbounce": 11.0,
    "dance-woah": 8.0,
    "sit-idle-phone-text": 15.0,
    "idle-laying-phone-texting": 22.0,
    "dance-freshprince": 11.0,
    "dance-twerk": 11.0,
    "emote-knocking-screen": 5.0,
    "idle-phone-talking": 13.0,
    "dance-wait": 9.0,
    "idle-crouched": 15.0,
    "emote-phone": 15.0,
    "emote-rifle": 5.0,
    "emote-thought": 5.0,
    "hcc-jetpack": 8.0,
    "emote-spiderman": 10.0,
    "emote-threadexchange-star": 16.0,
    "emote-confused2": 7.1,
    "emote-tune-accept": 4.0,
    "dance-running-man": 10.2,
    "emote-yogaSurprise": 16.0,
    "emote-sicklycute-sing-fast": 11.0,
    "emote-sicklycute-sing-slow": 10.0,
    "emote-kawaiipose": 7.0,
    "emote-collab-celebrate-right": 5.88,
    "sit-idle-laidBack": 20.0,
    "emote-blowkisses": 20.0,
    "dance-griddy": 20.0,
    "idle-dance-tiktok6": 20.0,
    "emote-alice-shrink": 15.0,
    "emote-meditate-idle": 20.0,
    "emote-collab-photo-right": 20.0,
    "emote-collab-photo-left": 20.0,
    "emote-afk-idle": 16.6,
    "emote-inside-out": 16.6,
    "emote-adoringfans": 12.18,
    "emote-sixseven": 2.81,
    "emote-sixseve-noimg": 2.81,
}


def load_dances() -> dict:
    try:
        with open(DANCES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"load_dances: using defaults ({e})")
        save_dances(DEFAULT_DANCES)
        return DEFAULT_DANCES.copy()


def save_dances(dances: dict) -> None:
    try:
        with open(DANCES_FILE, "w", encoding="utf-8") as f:
            json.dump(dances, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"save_dances error: {e}")


# --- فایل ماندگار ادمین‌ها ---
ADMINS_FILE = "admins.json"


def load_admins() -> set:
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"load_admins: starting empty ({e})")
        save_admins(set())
        return set()


def save_admins(admin_ids: set) -> None:
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(admin_ids), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"save_admins error: {e}")


class Bot(BaseBot):
    def __init__(self):
        super().__init__()
        self.ai_url = "https://api.gapgpt.app/v1/chat/completions"
        self.ai_model = os.environ.get("AI_MODEL", "gpt-4.1")
        self.histories = defaultdict(lambda: deque(maxlen=24))  # ۱۲ ردوبدل، یادش می‌مونه یه‌کم بیشتر
        self.owner_id = None
        self.own_user_id = None
        self.daily_usage = {}
        self._keepalive_task = None

        # قفل حرکت: وقتی True باشه بات هیچ‌جا نمی‌ره
        self.movement_locked = False
        # کاربری که الان بات داره براش میره (برای جلوگیری از تداخل چند درخواست هم‌زمان)
        self.busy_with_username = None

        # لاگ کل چت (حتی پیام‌های بدون !) برای فهمیدن زمینه‌ی گفتگو بعداً
        # هر آیتم: (timestamp, user_id, username, text)
        self.chat_log = deque(maxlen=300)

        # بافر پیام‌ها برای ساخت دیتاست؛ هر ۱۰۰ تا که جمع شد، یه‌جا پردازش و ذخیره میشن
        self.dataset_buffer = []

        # @خاموش / @روشن
        self.is_shutdown = False

        # حالت گزارش: وقتی #گزارش گفته میشه، تا این timestamp چت رو زیر نظر می‌گیره
        self.report_monitoring_until = None

        # ادمین‌هایی که با !ادمین اضافه شدن (توی فایل admins.json ذخیره میشن)
        self.admin_ids = load_admins()

        # جدول دنس‌ها (emote_id -> duration) + وضعیت لوپِ هر کاربر
        self.dances = load_dances()
        self.user_dance_states = {}  # user_id -> (stop_event, task)

        # گشت‌زدن وقتی بیکاره
        self._idle_wander_task = None
        self.pending_lonely_user_id = None
        self.recently_greeted_ids = deque(maxlen=10)

        # مکالمه‌ی فعال با AI: (user_id, username, آخرین timestamp)
        self.current_ai_partner = None

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot started")
        self.owner_id = session_metadata.room_info.owner_id
        self.own_user_id = session_metadata.user_id
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        if self._idle_wander_task is None:
            self._idle_wander_task = asyncio.create_task(self._idle_wander_loop())

    async def _idle_wander_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            wait_seconds = random.randint(*IDLE_CHECK_INTERVAL)
            await asyncio.sleep(wait_seconds)

            if self.is_shutdown or self.movement_locked or self.busy_with_username:
                continue
            if self.pending_lonely_user_id is not None:
                continue  # هنوز منتظر جواب یه نفریم

            try:
                result = await self.highrise.get_room_users()
            except Exception as e:
                print(f"idle wander get_room_users error: {e}")
                continue
            if isinstance(result, Error):
                continue

            positions = [
                (u, pos) for u, pos in result.content
                if isinstance(pos, Position) and u.id != self.own_user_id
            ]
            if len(positions) < 1:
                continue

            # برای هر کاربر، نزدیک‌ترین فاصله تا بقیه رو حساب کن؛ تنهاترین یعنی بیشترین "نزدیک‌ترین فاصله"
            def min_dist(target_u, target_pos, others):
                dists = [
                    ((op.x - target_pos.x) ** 2 + (op.z - target_pos.z) ** 2) ** 0.5
                    for ou, op in others if ou.id != target_u.id
                ]
                return min(dists) if dists else float("inf")

            candidates = []
            for target_u, target_pos in positions:
                if target_u.id in self.recently_greeted_ids:
                    continue
                d = min_dist(target_u, target_pos, positions)
                if d >= NEARBY_RADIUS:
                    candidates.append((d, target_u))

            if not candidates:
                continue

            candidates.sort(key=lambda item: -item[0])
            lonely_user = candidates[0][1]

            self.pending_lonely_user_id = lonely_user.id
            self.recently_greeted_ids.append(lonely_user.id)

            opener = random.choice([
                "چرا تنها وایستادی؟ 😄",
                "هووی، تنهایی؟ بیا حرف بزنیم",
                "چیه غمگینی؟ 😅",
            ])
            await self.go_greet_user(lonely_user, opener)

            # منتظر جواب بمون؛ اگه ظرف این مدت جواب نداد، بی‌خیال شو
            await asyncio.sleep(LONELY_RESPONSE_TIMEOUT)
            if self.pending_lonely_user_id == lonely_user.id:
                self.pending_lonely_user_id = None

    async def on_user_join(self, user: User, position) -> None:
        await self.highrise.chat(f"سلام {user.username} 👋")

    async def on_user_leave(self, user: User) -> None:
        await self._stop_user_dance(user.id)

    async def on_whisper(self, user: User, message: str) -> None:
        if self.is_owner(user):
            # مالک/ادمین: دقیقاً مثل چت عمومی پردازش میشه (دستورها اجرا میشن)
            # نکته: جواب‌ها همچنان توی خودِ روم (چت عمومی) نمایش داده میشن،
            # چون همه‌ی دستورها از self.highrise.chat استفاده می‌کنن، نه whisper.
            await self.on_chat(user, message)
            return

        # کاربر عادی: فقط راهنما، بدون اجرای هیچ دستوری
        try:
            await self.highrise.send_whisper(
                user.id,
                "سلام! من فقط یه بات معمولیم 🙂 برای صحبت باهام توی خودِ روم بنویس، "
                "مثلاً: !سلام یا هر سوالی با ! جلوش.",
            )
        except Exception as e:
            print(f"on_whisper reply error: {e}")

    def is_true_owner(self, user: User) -> bool:
        """فقط مالک واقعی، نه ادمین‌ها - برای دستوراتی مثل !ادمین که نباید دست ادمین‌ها باشه."""
        if self.owner_id and user.id == self.owner_id:
            return True
        return user.username.lower() in EXTRA_OWNER_USERNAMES

    def is_owner(self, user: User) -> bool:
        """مالک واقعی + ادمین‌هایی که با !ادمین اضافه شدن، هر دو دسترسی یکسان دارن."""
        if self.is_true_owner(user):
            return True
        return user.id in self.admin_ids

    async def add_admin_by_link(self, requester: User, link_or_username: str) -> None:
        username = extract_username_from_link(link_or_username)
        if not username:
            await self.highrise.chat(f"@{requester.username} لینک/یوزرنیم معتبر نیست.")
            return

        target_id = None
        found_username = username

        # اول توی خودِ روم بگرد (مطمئن‌تره، چون از داده‌ی زنده‌ی خودِ روم میاد)
        try:
            room_result = await self.highrise.get_room_users()
            if not isinstance(room_result, Error):
                for u, _pos in room_result.content:
                    if u.username.lower() == username.lower():
                        target_id = u.id
                        found_username = u.username
                        break
        except Exception as e:
            print(f"get_room_users (admin lookup) error: {e}")

        # اگه توی روم نبود، بریم سراغ جستجوی عمومی webapi (چندتا حالت حروف رو امتحان می‌کنیم)
        if target_id is None:
            candidates = [username, username.lower(), username.upper(), username.capitalize()]
            seen = set()
            for candidate in candidates:
                if candidate in seen:
                    continue
                seen.add(candidate)
                try:
                    result = await self.webapi.get_users(username=candidate)
                except Exception as e:
                    print(f"get_users error ({candidate}): {e}")
                    continue
                if result.users:
                    target_id = result.users[0].user_id
                    found_username = result.users[0].username or username
                    break

        if target_id is None:
            await self.highrise.chat(
                f"@{requester.username} پیداش نکردم. اگه الان توی روم حاضره، اسمشو دقیق (بدون فاصله‌ی اضافه) بفرست."
            )
            return

        self.admin_ids.add(target_id)
        save_admins(self.admin_ids)
        await self.highrise.chat(f"@{requester.username} {found_username} ادمین شد ✅ (برای همیشه ذخیره شد)")

    # ---------- حالت گزارش: با #گزارش فعال میشه، چند دقیقه چت رو زیرنظر می‌گیره ----------
    async def start_report_monitoring(self, requester: User) -> None:
        if self.report_monitoring_until and time.time() < self.report_monitoring_until:
            await self.highrise.chat(f"@{requester.username} الان دارم بررسی می‌کنم، صبر کن ⏳")
            return

        window_start = time.time()
        self.report_monitoring_until = window_start + REPORT_WINDOW_SECONDS
        await self.highrise.chat(
            f"@{requester.username} چشم، {REPORT_WINDOW_SECONDS // 60} دقیقه چت رو زیرنظر می‌گیرم 👀"
        )

        await asyncio.sleep(REPORT_WINDOW_SECONDS)

        window_messages = [
            (ts, uid, uname, msg) for ts, uid, uname, msg in self.chat_log if ts >= window_start
        ]
        self.report_monitoring_until = None

        if not window_messages:
            return

        conversation = "\n".join(f"{uname}: {msg}" for _, _, uname, msg in window_messages)
        aggressors = await self.classify_report_window(conversation)
        if not aggressors:
            return

        # آخرین username → user_id رو از همون پنجره‌ی زمانی پیدا کن
        username_to_id = {}
        for _, uid, uname, _ in window_messages:
            username_to_id[uname.lower()] = uid

        for name in aggressors:
            uid = username_to_id.get(name.strip().lower())
            if not uid:
                continue
            try:
                await self.highrise.moderate_room(uid, "mute", MUTE_SECONDS)
                hours = MUTE_SECONDS // 3600
                for _, u_id2, uname2, _ in window_messages:
                    if u_id2 == uid:
                        target_user = User(id=uid, username=uname2)
                        break
                await self.go_greet_user(
                    target_user, f"به‌خاطر دعوا/توهین توی چت، به مدت {hours} ساعت از صحبت‌کردن محدود شدی 🚫"
                )
            except Exception as e:
                print(f"report-mode moderate_room error: {e}")

    async def classify_report_window(self, conversation: str) -> list[str]:
        """با یه فراخوانیِ AI، کل بازه رو یه‌جا تحلیل می‌کنه، نه پیام‌به‌پیام (صرفه‌جویی توکن)."""
        api_key = os.environ.get("AI_API_KEY", "").strip()
        if not api_key:
            return []

        messages = [
            {
                "role": "system",
                "content": (
                    "این یه بخشی از چتِ یه روم Highrise ـه. بررسی کن آیا یه دعوای واقعی و جدی "
                    "(نه شوخی دوستانه) بین کاربرها اتفاق افتاده یا نه. "
                    "اگه دعوای جدی بود، فقط یوزرنیم‌های کسایی که واقعاً توهین/دعوا کردن رو با کاما جدا کن. "
                    "اگه دعوای جدی‌ای نبود، فقط بنویس: NONE. توضیح اضافه نده."
                ),
            },
            {"role": "user", "content": conversation[-4000:]},
        ]
        payload = {"model": self.ai_model, "messages": messages, "temperature": 0.0, "max_tokens": 60}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.ai_url, headers=headers, json=payload) as resp:
                    data = await resp.json(content_type=None)
                    reply = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    if not reply or reply.upper() == "NONE":
                        return []
                    return [name for name in reply.split(",") if name.strip()]
        except Exception as e:
            print(f"classify_report_window error: {e}")
            return []

    # ---------- بیدار نگه‌داشتن Render (پلن رایگان) ----------
    async def _keepalive_loop(self) -> None:
        # منتظر می‌مونیم بات کامل بالا بیاد
        await asyncio.sleep(30)
        while True:
            # فاصله‌ی تصادفی بین ۶ تا ۱۲ دقیقه، تا همیشه یه عدد ثابت نباشه
            wait_seconds = random.randint(6 * 60, 12 * 60)
            await asyncio.sleep(wait_seconds)

            if self.is_shutdown:
                # وقتی @خاموش گفته شده، دیگه پینگ نمی‌فرستیم تا Render خودش بعد از
                # حدود ۱۵ دقیقه بی‌کاری، سرویس رو واقعاً بخوابونه
                print("Keepalive skipped (bot is shut down)")
                continue

            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(PUBLIC_URL) as resp:
                        print(f"Keepalive ping: status={resp.status}")
            except Exception as e:
                print(f"Keepalive ping failed: {e}")

    # ---------- اومدن کنار یه کاربر (برای @بیا و !سلام هر دو) ----------
    # ---------- مدیریت آیتم/آواتار بات ----------
    async def cmd_buy_item(self, requester: User, item_id: str) -> None:
        try:
            result = await self.highrise.buy_item(item_id)
        except Exception as e:
            print(f"buy_item error: {e}")
            await self.highrise.chat(f"@{requester.username} خرید انجام نشد (خطای غیرمنتظره).")
            return

        if isinstance(result, Error):
            await self.highrise.chat(f"@{requester.username} خرید ناموفق: {result.message}")
            return
        if result == "insufficient_funds":
            await self.highrise.chat(f"@{requester.username} گلد بات کافی نیست 💸")
            return
        await self.highrise.chat(f"@{requester.username} خریداری شد ✅ حالا با !بپوش {item_id} می‌تونی بپوشونیش")

    # دسته‌هایی که پیشوندشون دو تیکه‌ای‌ه (با یه خط‌تیره وسطشون)، نه یه تیکه
    KNOWN_TWO_PART_CATEGORIES = {
        "hair-front", "hair-back", "hair_front", "hair_back",
        "eye-color", "eye_color",
    }

    # نگاشت کلمه‌ی فارسی به پیشوند احتمالیِ دسته (حدسیه، چون Highrise لیست رسمی دسته‌ها رو نداده)
    CATEGORY_ALIASES = {
        "کلاه": "hat",
        "عینک": "glasses",
        "کفش": "shoe",
    }

    COLOR_CATEGORY_ALIASES = {
        "چشم": "eye",
        "مو": "hair",
        "پوست": "body",  # آیدی واقعیش چیزی مثل body-flesh هست، نه skin
        "ابرو": "eyebrow",
    }

    def _get_item_category(self, item_id: str) -> str:
        """
        پیشوند دسته‌ی آیتم رو از روی آیدیش تشخیص میده (مثلاً 'shirt' از 'shirt-abc123').
        برای دسته‌های دو‌تیکه‌ای شناخته‌شده (مثل hair-front) دو تیکه‌ی اول رو نگه می‌داره.
        این یه تشخیصِ حدسیه بر اساس الگوهای رایج؛ اگه یه دسته‌ی جدید و ناشناخته دیدی که
        درست تشخیص داده نشد، به KNOWN_TWO_PART_CATEGORIES اضافه‌ش کن.
        """
        parts = item_id.split("-")
        if len(parts) >= 2:
            two_part = f"{parts[0]}-{parts[1]}".lower()
            if two_part in self.KNOWN_TWO_PART_CATEGORIES:
                return two_part
        return parts[0]

    async def cmd_wear_item(self, requester: User, item_id: str) -> None:
        outfit_result = await self.highrise.get_my_outfit()
        if isinstance(outfit_result, Error):
            await self.highrise.chat(f"@{requester.username} نتونستم لباس فعلی رو بخونم.")
            return

        current_items = list(outfit_result.outfit)

        # اگه از قبل دقیقاً همین آیتم پوشیده، دوباره اضافه نکن
        if any(it.id == item_id for it in current_items):
            await self.highrise.chat(f"@{requester.username} این آیتم از قبل پوشیدمش.")
            return

        category = self._get_item_category(item_id)

        # هر آیتم دیگه‌ای که توی همین دسته‌ست رو از لباس فعلی حذف کن
        # (چون هر دسته - مثل shirt، pants، eye - فقط یه آیتم هم‌زمان مجازه)
        new_outfit = [
            it for it in current_items
            if self._get_item_category(it.id) != category
        ]

        new_item = Item(type="clothing", amount=1, id=item_id, active_palette=0)
        new_outfit.append(new_item)

        try:
            result = await self.highrise.set_outfit(new_outfit)
        except Exception as e:
            print(f"set_outfit error: {e}")
            await self.highrise.chat(f"@{requester.username} پوشیدن انجام نشد (خطای غیرمنتظره).")
            return

        if isinstance(result, Error):
            await self.highrise.chat(
                f"@{requester.username} پوشیدن ناموفق: {result.message} "
                f"(ممکنه آیدی آیتم اشتباه باشه، یا نیاز به خرید داشته باشه - "
                f"آیتم‌های رایگان با rarity=none نیازی به خرید ندارن، پس اگه پیام هنوز میاد "
                f"با !بخر {item_id} امتحان کن)"
            )
            return
        await self.highrise.chat(f"@{requester.username} پوشیدم ✅")

    async def cmd_unwear_item(self, requester: User, item_id: str) -> None:
        outfit_result = await self.highrise.get_my_outfit()
        if isinstance(outfit_result, Error):
            await self.highrise.chat(f"@{requester.username} نتونستم لباس فعلی رو بخونم.")
            return

        current_items = list(outfit_result.outfit)
        new_outfit = [it for it in current_items if it.id != item_id]

        if len(new_outfit) == len(current_items):
            await self.highrise.chat(f"@{requester.username} این آیتم رو اصلاً پوشیده نبودم.")
            return

        try:
            result = await self.highrise.set_outfit(new_outfit)
        except Exception as e:
            print(f"set_outfit error: {e}")
            await self.highrise.chat(f"@{requester.username} درآوردن انجام نشد.")
            return

        if isinstance(result, Error):
            await self.highrise.chat(f"@{requester.username} درآوردن ناموفق: {result.message}")
            return
        await self.highrise.chat(f"@{requester.username} درش آوردم ✅")

    async def cmd_remove_category(self, requester: User, category: str) -> None:
        outfit_result = await self.highrise.get_my_outfit()
        if isinstance(outfit_result, Error):
            await self.highrise.chat(f"@{requester.username} نتونستم لباس فعلی رو بخونم.")
            return

        current_items = list(outfit_result.outfit)
        new_outfit = [it for it in current_items if self._get_item_category(it.id) != category]

        if len(new_outfit) == len(current_items):
            await self.highrise.chat(f"@{requester.username} چیزی از این دسته پوشیده نبودم.")
            return

        try:
            result = await self.highrise.set_outfit(new_outfit)
        except Exception as e:
            print(f"set_outfit error: {e}")
            await self.highrise.chat(f"@{requester.username} درآوردن انجام نشد.")
            return

        if isinstance(result, Error):
            await self.highrise.chat(f"@{requester.username} درآوردن ناموفق: {result.message}")
            return
        await self.highrise.chat(f"@{requester.username} درش آوردم ✅")

    async def cmd_remove_all(self, requester: User) -> None:
        try:
            result = await self.highrise.set_outfit([])
        except Exception as e:
            print(f"set_outfit error: {e}")
            await self.highrise.chat(f"@{requester.username} انجام نشد.")
            return
        if isinstance(result, Error):
            await self.highrise.chat(f"@{requester.username} ناموفق: {result.message}")
            return
        await self.highrise.chat(f"@{requester.username} همه‌چیزو درآوردم 😅")

    async def cmd_change_color(self, requester: User, category: str, palette_index: int) -> None:
        outfit_result = await self.highrise.get_my_outfit()
        if isinstance(outfit_result, Error):
            await self.highrise.chat(f"@{requester.username} نتونستم لباس فعلی رو بخونم.")
            return

        current_items = list(outfit_result.outfit)
        matched = [it for it in current_items if self._get_item_category(it.id).startswith(category)]

        if not matched:
            await self.highrise.chat(f"@{requester.username} همچین آیتمی الان پوشیده نیستم که رنگش رو عوض کنم.")
            return

        new_outfit = [it for it in current_items if it not in matched]
        for it in matched:
            new_outfit.append(
                Item(type=it.type, amount=it.amount, id=it.id, account_bound=it.account_bound, active_palette=palette_index)
            )

        try:
            result = await self.highrise.set_outfit(new_outfit)
        except Exception as e:
            print(f"set_outfit error: {e}")
            await self.highrise.chat(f"@{requester.username} تغییر رنگ انجام نشد.")
            return

        if isinstance(result, Error):
            await self.highrise.chat(
                f"@{requester.username} تغییر رنگ ناموفق: {result.message} "
                f"(ممکنه این دسته اصلاً با active_palette رنگ عوض نکنه)"
            )
            return
        await self.highrise.chat(f"@{requester.username} رنگش عوض شد ✅")

    async def cmd_show_outfit(self, requester: User) -> None:
        outfit_result = await self.highrise.get_my_outfit()
        if isinstance(outfit_result, Error):
            await self.highrise.chat(f"@{requester.username} نتونستم لباس فعلی رو بخونم.")
            return
        ids = [it.id for it in outfit_result.outfit]
        await self.highrise.chat(f"@{requester.username} الان پوشیدم: " + ", ".join(ids[:10]))

    async def cmd_show_inventory(self, requester: User) -> None:
        inv_result = await self.highrise.get_inventory()
        if isinstance(inv_result, Error):
            await self.highrise.chat(f"@{requester.username} نتونستم کمد رو بخونم.")
            return
        ids = [it.id for it in inv_result.items]
        if not ids:
            await self.highrise.chat(f"@{requester.username} کمدم خالیه.")
            return
        await self.highrise.chat(f"@{requester.username} توی کمدم: " + ", ".join(ids[:15]))

    # ---------- سیستم لوپ دنس (با جدول duration) ----------
    BOT_DANCE_KEY = "__bot__"  # کلید مخصوص لوپ دنس خودِ بات (نه یه کاربر خاص)

    async def _user_dance_loop(self, key: str, emote_id: str, stop_event: asyncio.Event, target_user_id: str | None) -> None:
        duration = self.dances.get(emote_id)

        async def fire():
            if target_user_id is None:
                # None یعنی برای خودِ بات (بدون target_user_id، طبق مستندات SDK)
                result = await self.highrise.send_emote(emote_id)
            else:
                result = await self.highrise.send_emote(emote_id, target_user_id)
            return result

        if duration is None:
            try:
                await asyncio.wait_for(fire(), timeout=8)
            except Exception as e:
                print(f"Dance single-shot error: {e}")
            return

        interval = max(0.3, duration - LOOP_EARLY_SECONDS)
        start = time.monotonic()
        cycle = 0

        while not stop_event.is_set():
            t_before = time.monotonic()
            try:
                result = await asyncio.wait_for(fire(), timeout=8)
                t_after = time.monotonic()
                print(
                    f"DANCE_DEBUG cycle={cycle} emote={emote_id} "
                    f"call_took={t_after - t_before:.2f}s result={result!r}"
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                print(f"DANCE_DEBUG cycle={cycle} emote={emote_id} TIMEOUT after {time.monotonic() - t_before:.2f}s")
            except Exception as e:
                print(f"DANCE_DEBUG cycle={cycle} emote={emote_id} EXCEPTION: {e!r}")
                return

            cycle += 1
            next_deadline = start + cycle * interval
            wait_time = max(0.0, next_deadline - time.monotonic())
            print(f"DANCE_DEBUG cycle={cycle} sleeping {wait_time:.2f}s (interval={interval:.2f}s)")
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=wait_time)

    def _start_user_dance(self, key: str, emote_id: str, target_user_id: str | None = None) -> None:
        if target_user_id is None and key != self.BOT_DANCE_KEY:
            target_user_id = key  # حالت عادی: key همون user_id ـه

        old = self.user_dance_states.get(key)
        if old:
            old_event, old_task = old[0], old[1]
            old_event.set()
            if not old_task.done():
                old_task.cancel()

        stop_event = asyncio.Event()
        task = asyncio.create_task(self._user_dance_loop(key, emote_id, stop_event, target_user_id))
        self.user_dance_states[key] = (stop_event, task, emote_id)

    async def _stop_user_dance(self, key: str) -> bool:
        state = self.user_dance_states.pop(key, None)
        if not state:
            return False
        stop_event, task = state[0], state[1]
        stop_event.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return True

    def _refresh_dances_using(self, emote_id: str) -> None:
        """وقتی duration یه دنس عوض میشه، هر کسی که الان داره همون دنس رو اجرا می‌کنه، خودکار رفرش میشه."""
        for key, state in list(self.user_dance_states.items()):
            if state[2] == emote_id:
                target_user_id = None if key == self.BOT_DANCE_KEY else key
                self._start_user_dance(key, emote_id, target_user_id)

    async def cmd_tip(self, requester: User, target_word: str, amount: int) -> None:
        bar_map = {
            1: "gold_bar_1", 5: "gold_bar_5", 10: "gold_bar_10", 50: "gold_bar_50",
            100: "gold_bar_100", 500: "gold_bar_500", 1000: "gold_bar_1k",
            5000: "gold_bar_5000", 10000: "gold_bar_10k",
        }
        bar_name = bar_map.get(amount)
        if not bar_name:
            await self.highrise.chat(
                f"@{requester.username} فقط این مقدارها پشتیبانی میشن: {', '.join(str(a) for a in bar_map)}"
            )
            return

        room_result = await self.highrise.get_room_users()
        if isinstance(room_result, Error):
            await self.highrise.chat(f"@{requester.username} نتونستم لیست روم رو بخونم.")
            return
        all_users = [u for u, _pos in room_result.content if u.id != self.own_user_id]

        if target_word in {"all", "a", "همه"}:
            targets = all_users
        else:
            uname = target_word.lstrip("@").lower()
            targets = [u for u in all_users if u.username.lower() == uname]
            if not targets:
                await self.highrise.chat(f"@{requester.username} این کاربر توی روم پیدا نشد.")
                return

        success = failed = 0
        for target in targets:
            try:
                result = await self.highrise.tip_user(target.id, bar_name)
                if result == "success":
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                print(f"tip_user error: {e}")
                failed += 1

        if failed == 0:
            await self.highrise.chat(f"@{requester.username} تیپ انجام شد ✅ ({success} نفر)")
        else:
            await self.highrise.chat(f"@{requester.username} تیپ: {success} موفق، {failed} ناموفق (شاید گلد کافی نبود)")

    async def cmd_show_wallet(self, requester: User) -> None:
        wallet_result = await self.highrise.get_wallet()
        if isinstance(wallet_result, Error):
            await self.highrise.chat(f"@{requester.username} نتونستم کیف‌پول رو بخونم.")
            return
        parts = [f"{c.type}: {c.amount}" for c in wallet_result.content]
        if not parts:
            await self.highrise.chat(f"@{requester.username} کیف‌پولم خالیه.")
            return
        await self.highrise.chat(f"@{requester.username} کیف‌پولم: " + " | ".join(parts))

    async def go_greet_user(self, requester: User, say_after: str) -> None:
        # شرط ۱: اگه بات قفله، اصلاً حرکت نکن
        if self.movement_locked:
            await self.highrise.chat(f"@{requester.username} الان قفلم، نمی‌تونم راه برم 🔒")
            return

        # شرط ۲: اگه بات همین الان داره برای یکی دیگه میره، صبر کن
        if self.busy_with_username and self.busy_with_username != requester.username:
            await self.highrise.chat(
                f"@{requester.username} صبر کن، الان دارم میرم پیش {self.busy_with_username} 🙏"
            )
            return

        self.busy_with_username = requester.username
        try:
            result = await self.highrise.get_room_users()

            if isinstance(result, Error):
                print(f"get_room_users Error: {result.message}")
                await self.highrise.chat(f"@{requester.username} نتونستم موقعیتت رو پیدا کنم.")
                return

            target_position = None
            for u, pos in result.content:
                if u.id == requester.id:
                    target_position = pos
                    break

            if target_position is None:
                await self.highrise.chat(f"@{requester.username} پیدات نکردم توی اتاق.")
                return

            # شرط ۳: دقیقاً روی کاربر نایست، یه کم کنارش (اگه Position عادی بود)
            if isinstance(target_position, Position):
                target_position = Position(
                    x=target_position.x + 1.0,
                    y=target_position.y,
                    z=target_position.z,
                    facing=target_position.facing,
                )

            try:
                await self.highrise.walk_to(target_position)
            except Exception as e:
                print(f"walk_to error: {e}")
                await self.highrise.chat(f"@{requester.username} پیدات کردم ولی نتونستم بیام کنارت.")
                return

            await self.highrise.chat(f"@{requester.username} {say_after}")
        finally:
            self.busy_with_username = None

    def check_and_use_quota(self, user_id: str) -> bool:
        today = date.today().isoformat()
        used_date, count = self.daily_usage.get(user_id, (today, 0))
        if used_date != today:
            count = 0
        if count >= DAILY_AI_LIMIT:
            self.daily_usage[user_id] = (today, count)
            return False
        self.daily_usage[user_id] = (today, count + 1)
        return True

    async def gather_nearby_context(self, requester: User) -> str:
        """
        پیام‌های اخیرِ افرادی که (بر اساس موقعیت فعلیشون) نزدیک درخواست‌دهنده هستن رو جمع می‌کنه.
        این‌جوری گفتگوی «فوتبال» سمت چپ روم با گفتگوی «ماشین» سمت راست قاطی نمیشه.
        """
        result = await self.highrise.get_room_users()
        if isinstance(result, Error):
            return ""

        positions = {}
        for u, pos in result.content:
            if isinstance(pos, Position):
                positions[u.id] = pos

        requester_pos = positions.get(requester.id)
        if requester_pos is None:
            return ""

        # فاصله رو فقط روی صفحه‌ی زمین (x, z) حساب می‌کنیم، نه ارتفاع (y)
        def distance(p):
            return ((p.x - requester_pos.x) ** 2 + (p.z - requester_pos.z) ** 2) ** 0.5

        nearby_user_ids = {
            uid for uid, pos in positions.items() if distance(pos) <= NEARBY_RADIUS
        }

        now = time.time()
        relevant = [
            (ts, uname, msg)
            for ts, uid, uname, msg in self.chat_log
            if uid in nearby_user_ids and (now - ts) <= CONTEXT_WINDOW_SECONDS
        ]
        relevant = relevant[-MAX_CONTEXT_MESSAGES:]

        if not relevant:
            return ""

        return "\n".join(f"{uname}: {msg}" for _, uname, msg in relevant)

    # ---------- حافظه‌ی بلندمدت (از طریق API روی هاست) ----------
    async def get_long_term_memory(self, user_id: str) -> str | None:
        if not DB_API_URL:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                params = {"user_id": user_id, "secret": DB_API_SECRET}
                async with session.get(f"{DB_API_URL}/get_memory.php", params=params) as resp:
                    data = await resp.json(content_type=None)
                    return data.get("note")
        except Exception as e:
            print(f"get_long_term_memory error: {e}")
            return None

    async def save_long_term_memory(self, user_id: str, note: str) -> bool:
        if not DB_API_URL:
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = {"user_id": user_id, "note": note, "secret": DB_API_SECRET}
                async with session.post(f"{DB_API_URL}/save_memory.php", data=payload) as resp:
                    data = await resp.json(content_type=None)
                    return bool(data.get("ok"))
        except Exception as e:
            print(f"save_long_term_memory error: {e}")
            return False

    # ---------- ساخت دیتاست: هر ۱۰۰ پیام یه‌جا پاک‌سازی و ذخیره میشن ----------
    async def process_dataset_batch(self) -> None:
        batch = self.dataset_buffer[:DATASET_BATCH_SIZE]
        self.dataset_buffer = self.dataset_buffer[DATASET_BATCH_SIZE:]

        numbered = "\n".join(f"{i+1}: {msg}" for i, msg in enumerate(batch))

        api_key = os.environ.get("AI_API_KEY", "").strip()
        bad_numbers = set()
        if api_key:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "این‌ها پیام‌های چت یه روم Highrise هستن، شماره‌گذاری شده. "
                        "بگو کدوم شماره‌ها باید حذف بشن چون فحش/توهین/اسپم/بی‌معنی هستن. "
                        "فقط شماره‌ها رو با کاما جدا کن (مثلاً: 3,17,42). اگه هیچی نبود بنویس NONE."
                    ),
                },
                {"role": "user", "content": numbered[-6000:]},
            ]
            payload = {"model": self.ai_model, "messages": messages, "temperature": 0.0, "max_tokens": 200}
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            timeout = aiohttp.ClientTimeout(total=25)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(self.ai_url, headers=headers, json=payload) as resp:
                        data = await resp.json(content_type=None)
                        reply = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                        if reply and reply.upper() != "NONE":
                            for part in reply.split(","):
                                part = part.strip()
                                if part.isdigit():
                                    bad_numbers.add(int(part))
            except Exception as e:
                print(f"process_dataset_batch classify error: {e}")

        clean_messages = [msg for i, msg in enumerate(batch) if (i + 1) not in bad_numbers]
        # حذف تکراری‌های داخل همین دسته (دیتابیس هم خودش تکراری‌های قدیمی‌تر رو با UNIQUE KEY رد می‌کنه)
        clean_messages = list(dict.fromkeys(clean_messages))

        if not clean_messages or not DB_API_URL:
            return

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = {
                    "messages": json.dumps(clean_messages, ensure_ascii=False),
                    "secret": DB_API_SECRET,
                }
                async with session.post(f"{DB_API_URL}/save_dataset.php", data=payload) as resp:
                    result = await resp.json(content_type=None)
                    print(f"Dataset batch saved: {result}")
        except Exception as e:
            print(f"save_dataset batch error: {e}")

    async def ask_gapgpt(self, user_id: str, username: str, text: str, context: str = "") -> str:
        api_key = os.environ.get("AI_API_KEY", "").strip()
        if not api_key:
            return "AI_API_KEY تنظیم نشده."

        messages = [
            {
                "role": "system",
                "content": (
                    "بات رسمی Highrise هستی، آواتارت دخترونه‌ست. رسمی، مودب، کمی گرم؛ نه شوخ، نه زیادی ناناز. "
                    "خیلی کوتاه جواب بده (۱-۲ خط). "
                    "اگه پرسیدن جنسیتت چیه: بگو هوش مصنوعی‌ای، جنسیت دختر رو متناسب با آواتارت انتخاب کردی. "
                    "به فحش دوستانه‌ی کاربرها وارد نشو، فقط خنثی رد شو؛ فقط دعوای جدی رو مودبانه هشدار بده."
                ),
            }
        ]
        long_term_note = await self.get_long_term_memory(user_id)
        if long_term_note:
            messages.append({
                "role": "system",
                "content": f"چیزهایی که قبلاً درباره‌ی این کاربر یاد گرفتی: {long_term_note}",
            })

        for item in self.histories[user_id]:
            messages.append(item)

        if context:
            messages.append({
                "role": "system",
                "content": (
                    "این‌ها پیام‌های اخیریه که افراد نزدیک همین کاربر توی چت گفتن "
                    "(ممکنه خودِ کاربر هم توشون باشه). ازشون برای فهمیدن زمینه‌ی حرف کاربر استفاده کن، "
                    "ولی توی جوابت مستقیم تکرارشون نکن:\n" + context
                ),
            })

        messages.append({"role": "user", "content": f"{username}: {text}"})

        payload = {
            "model": self.ai_model,
            "messages": messages,
            "temperature": 0.9,
            "max_tokens": 120,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=25)

        for attempt in range(2):  # یه‌بار تلاش دوباره، چون GapGPT گاهی ناپایداره
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(self.ai_url, headers=headers, json=payload) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status != 200:
                            print("GapGPT error:", resp.status, data)
                            if attempt == 0:
                                continue
                            return "فعلاً نتونستم جواب بدم."
                        reply = (
                            data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                            .strip()
                        )
                        if not reply:
                            if attempt == 0:
                                continue
                            return "جوابی نگرفتم."
                        self.histories[user_id].append({"role": "user", "content": f"{username}: {text}"})
                        self.histories[user_id].append({"role": "assistant", "content": reply})
                        return reply[:250]
            except Exception as e:
                print("AI request error:", e)
                if attempt == 0:
                    continue
                return "مشکل در ارتباط با هوش مصنوعی."

    async def handle_come_here_with_priority(self, requester: User, payload: str) -> None:
        partner = self.current_ai_partner
        now = time.time()

        is_someone_else_active = (
            partner is not None
            and partner[0] != requester.id
            and (now - partner[2]) <= ACTIVE_CONVO_WINDOW
        )

        if is_someone_else_active:
            partner_id, partner_username, _ = partner
            await self.highrise.chat(
                f"@{partner_username} ببخشید، {requester.username} کارم داره؛ "
                f"اگه کارت باهام تموم نشده بگو 'صبرکن' 🙏"
            )

            deadline = time.time() + NUDGE_TIMEOUT
            objected = False
            while time.time() < deadline:
                await asyncio.sleep(1)
                for ts, uid, uname, msg in reversed(self.chat_log):
                    if ts < deadline - NUDGE_TIMEOUT:
                        break
                    if uid == partner_id and re.search(r"صبر|وایسا|وایستا", msg):
                        objected = True
                        break
                if objected:
                    break

            if objected:
                await self.highrise.chat(f"@{requester.username} یه لحظه صبر کن، {partner_username} هنوز کارم داره 🙏")
                return

        reply_text = await self.ask_come_reply(requester.username, payload)
        await self.go_greet_user(requester, reply_text)
        self.current_ai_partner = (requester.id, requester.username, time.time())

    async def ask_come_reply(self, username: str, original_message: str) -> str:
        """یه جمله‌ی کوتاه و طبیعی تولید می‌کنه، انگار بات داره میاد سمت کاربر."""
        api_key = os.environ.get("AI_API_KEY", "").strip()
        if not api_key:
            return "دارم میام! 🏃"

        messages = [
            {
                "role": "system",
                "content": (
                    "تو یه بات دخترونه‌ی رسمیِ Highrise هستی که الان داره به سمت یه کاربر راه میره چون صداش زده. "
                    "فقط یه جمله‌ی خیلی کوتاه (حداکثر ۱۰ کلمه)، مودب و ملایم بگو که داری میای، "
                    "و اگه کاربر دلیلی برای صدا زدن گفته (مثلاً 'کارت دارم')، مودبانه بپرس چیکار داره. "
                    "بدون شوخی زیاده و بدون توضیح اضافه، فقط همون یه جمله."
                ),
            },
            {"role": "user", "content": f"{username} گفت: {original_message}"},
        ]
        payload = {
            "model": self.ai_model,
            "messages": messages,
            "temperature": 0.9,
            "max_tokens": 60,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=15)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.ai_url, headers=headers, json=payload) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200:
                        print("GapGPT come-reply error:", resp.status, data)
                        return "دارم میام! 🏃"
                    reply = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        .strip()
                    )
                    return reply[:100] if reply else "دارم میام! 🏃"
        except Exception as e:
            print("AI come-reply error:", e)
            return "دارم میام! 🏃"

    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip()
        if not text:
            return

        # همه‌ی پیام‌ها (حتی بدون !) رو ثبت می‌کنیم تا بعداً بشه زمینه‌ی گفتگو رو فهمید
        self.chat_log.append((time.time(), user.id, user.username, text))

        # برای دیتاست: هر ۱۰۰ پیام که جمع شد، یه‌جا پردازش و ذخیره میشه
        self.dataset_buffer.append(text)
        if len(self.dataset_buffer) >= DATASET_BATCH_SIZE:
            asyncio.create_task(self.process_dataset_batch())

        # --- توقف دنس: با هر پیشوندی (/stop، !stop، یا حتی بدون علامت) کار می‌کنه ---
        stop_words = {"stop", "استوپ", "متوقف", "/stop", "!stop"}
        if text.strip().lower() in stop_words:
            stopped = await self._stop_user_dance(user.id)
            await self.highrise.chat(
                f"@{user.username} دنس متوقف شد ✅" if stopped else f"@{user.username} دنس فعالی نداری"
            )
            return

        # --- @روشن : همیشه کار می‌کنه، حتی وقتی خاموشیم ---
        if text in {"@روشن", "@on"}:
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            self.is_shutdown = False
            await self.highrise.chat("روشن شدم ✅")
            return

        if self.is_shutdown:
            return

        # --- @خاموش : فقط مالک ---
        if text in {"@خاموش", "@off"}:
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            await self.highrise.chat("باشه، خاموش میشم 🔌 (برای روشن‌کردن: @روشن)")
            self.is_shutdown = True
            return

        # --- #گزارش : چند دقیقه چت رو زیرنظر می‌گیره تا دعوای واقعی رو پیدا کنه ---
        if text.strip() in {"#گزارش", "#report"}:
            asyncio.create_task(self.start_report_monitoring(user))
            return

        # اگه کاربر پابندِ منتظرِ پاسخِ «تنها» بود، یعنی جواب داد → دیگه بات نره سراغ یکی دیگه
        if self.pending_lonely_user_id == user.id:
            self.pending_lonely_user_id = None

        # --- @بیا : فقط مالک ---
        if text in {"@بیا", "@come", "@بیا اینجا"}:
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            await self.go_greet_user(user, f"سلام {user.username} 👋 | آیدی: {user.id}")
            return

        # --- @قفل / @باز : فقط مالک، قفل‌کردن حرکت بات ---
        if text in {"@قفل", "@lock"}:
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            self.movement_locked = True
            await self.highrise.chat("بات قفل شد 🔒 دیگه راه نمیرم.")
            return

        if text in {"@باز", "@unlock"}:
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            self.movement_locked = False
            await self.highrise.chat("بات باز شد 🔓 دوباره می‌تونم راه برم.")
            return

        # --- /emote_id مستقیم ---
        if text.startswith("/"):
            payload = text[1:].strip()
            if payload.lower() in {"stop", "استوپ", "متوقف"}:
                stopped = await self._stop_user_dance(user.id)
                await self.highrise.chat(
                    f"@{user.username} دنس متوقف شد ✅" if stopped else f"@{user.username} دنس فعالی نداری"
                )
                return
            emote_id = extract_emote_id(payload)
            if not emote_id:
                await self.highrise.chat(f"@{user.username} فرمت دنس معتبر نیست.")
                return
            self._start_user_dance(user.id, emote_id)
            known = emote_id in self.dances
            note = "" if known else " (duration نامشخصه، فقط یه‌بار اجرا میشه)"
            await self.highrise.chat(f"@{user.username} دنس شروع شد 💃{note} (توقف: /stop)")
            return

        # --- بقیه فقط با ! ---
        if not text.startswith("!"):
            return

        payload = text[1:].strip()
        lower = payload.lower()

        if lower == "ping":
            await self.highrise.chat("pong 🏓")
            return

        if lower == "clear":
            self.histories.pop(str(user.id), None)
            await self.highrise.chat(f"@{user.username} حافظه پاک شد ✅")
            return

        # --- !یادت بمونه <متن> : ذخیره‌ی یه نکته توی حافظه‌ی بلندمدت کاربر ---
        if lower.startswith("یادت بمونه") or lower.startswith("یادت باشه"):
            note = payload.split(" ", 2)
            note_text = note[2] if len(note) > 2 else ""
            if not note_text.strip():
                await self.highrise.chat(f"@{user.username} بعد از 'یادت بمونه' باید یه متن بنویسی")
                return
            saved = await self.save_long_term_memory(str(user.id), note_text.strip())
            if saved:
                await self.highrise.chat(f"@{user.username} باشه، یادم موند 🧠")
            else:
                await self.highrise.chat(f"@{user.username} نتونستم ذخیره کنم، بعداً امتحان کن")
            return

        # --- تشخیص نیت «بیا اینجا/کنارم/پیشم» : بات میره کنار هر کسی که این‌رو بگه ---
        if is_come_here_request(payload):
            await self.handle_come_here_with_priority(user, payload)
            return

        # --- !ادمین <لینک/یوزرنیم> : فقط مالک واقعی می‌تونه ادمین اضافه کنه ---
        # --- !تیپ <همه/all/a/یوزرنیم> <مقدار> یا کوتاه: !t ---
        # --- !ثبت VIP : مختصات همین لحظه‌ی کاربر رو میگه، خودت دستی توی VIP_ZONES کد اضافه کن ---
        if lower in {"ثبت vip", "ثبت وی‌آی‌پی", "ثبت وی آی پی"}:
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک/ادمین فعاله.")
                return
            room_result = await self.highrise.get_room_users()
            if isinstance(room_result, Error):
                await self.highrise.chat(f"@{user.username} نتونستم موقعیت رو بخونم.")
                return
            pos = None
            for u, p in room_result.content:
                if u.id == user.id and isinstance(p, Position):
                    pos = p
                    break
            if pos is None:
                await self.highrise.chat(f"@{user.username} موقعیتت رو پیدا نکردم.")
                return
            await self.highrise.chat(
                f"@{user.username} مختصات اینجا: x={pos.x:.2f}, y={pos.y:.2f}, z={pos.z:.2f} "
                f"— این‌رو توی VIP_ZONES (بالای main.py) اضافه کن"
            )
            return

        if lower.startswith("تیپ ") or lower.startswith("t "):
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک/ادمین فعاله.")
                return
            parts = payload.split()
            if len(parts) != 3:
                await self.highrise.chat(f"@{user.username} فرمت درست: !تیپ همه 5  یا  !t a 5")
                return
            target_word, amount_str = parts[1], parts[2]
            try:
                amount = int(amount_str)
            except ValueError:
                await self.highrise.chat(f"@{user.username} مقدار باید عدد باشه.")
                return
            await self.cmd_tip(user, target_word, amount)
            return

        if lower.startswith("ادمین "):
            if not self.is_true_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک اصلی بات فعاله.")
                return
            link_or_username = payload.split(" ", 1)[1].strip()
            await self.add_admin_by_link(user, link_or_username)
            return

        # --- !ادمین‌آیدی <user_id خام> : بدون جستجو، مستقیم آیدی رو اضافه می‌کنه ---
        if lower.startswith("ادمین‌آیدی ") or lower.startswith("ادمین آیدی "):
            if not self.is_true_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک اصلی بات فعاله.")
                return
            raw_id = payload.split(" ", 1)[1].strip()
            if not raw_id:
                await self.highrise.chat(f"@{user.username} آیدی رو بفرست.")
                return
            self.admin_ids.add(raw_id)
            save_admins(self.admin_ids)
            await self.highrise.chat(f"@{user.username} آیدی {raw_id} ادمین شد ✅ (ذخیره شد)")
            return

        # --- مدیریت آیتم/آواتار بات: فقط مالک ---
        if lower.startswith("بخر "):
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            raw = payload.split(" ", 1)[1].strip()
            item_id = extract_item_id(raw)
            if item_id is None:
                await self.highrise.chat(f"@{user.username} آیدی آیتم معتبر نیست.")
                return
            await self.cmd_buy_item(user, item_id)
            return

        if lower.startswith("بپوش "):
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            raw = payload.split(" ", 1)[1].strip()
            item_id = extract_item_id(raw)
            if item_id is None:
                await self.highrise.chat(f"@{user.username} آیدی آیتم معتبر نیست.")
                return
            await self.cmd_wear_item(user, item_id)
            return

        if lower.startswith("دربیار "):
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            raw = payload.split(" ", 1)[1].strip()
            item_id = extract_item_id(raw)
            if item_id is None:
                await self.highrise.chat(f"@{user.username} آیدی آیتم معتبر نیست.")
                return
            await self.cmd_unwear_item(user, item_id)
            return

        # --- !درآر <کلاه/عینک/کفش/همه> ---
        if lower.startswith("درآر "):
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            word = payload.split(" ", 1)[1].strip()
            if word == "همه":
                await self.cmd_remove_all(user)
                return
            category = self.CATEGORY_ALIASES.get(word)
            if category:
                await self.cmd_remove_category(user, category)
                return
            # دسته‌ی شناخته‌شده نبود؛ شاید خودِ لینک/آیدی یه آیتم خاص باشه
            item_id = extract_item_id(word)
            if item_id:
                await self.cmd_unwear_item(user, item_id)
                return
            await self.highrise.chat(
                f"@{user.username} این‌رو نشناختم — یا اسم دسته (کلاه/عینک/کفش/همه) بده، "
                f"یا لینک/آیدی خودِ آیتم رو بفرست."
            )
            return

        # --- !رنگ <چشم/مو/پوست/ابرو> <شماره> ---
        if lower.startswith("رنگ "):
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            parts = payload.split()
            if len(parts) != 3:
                await self.highrise.chat(f"@{user.username} فرمت درست: !رنگ چشم 5")
                return
            word, index_str = parts[1], parts[2]
            category = self.COLOR_CATEGORY_ALIASES.get(word)
            if not category:
                await self.highrise.chat(
                    f"@{user.username} این دسته رو نمی‌شناسم. الان فقط: چشم، مو، پوست، ابرو"
                )
                return
            try:
                index = int(index_str)
            except ValueError:
                await self.highrise.chat(f"@{user.username} شماره‌ی رنگ باید عدد باشه.")
                return
            await self.cmd_change_color(user, category, index)
            return

        if lower in {"لباسام", "outfit"}:
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            await self.cmd_show_outfit(user)
            return

        if lower in {"کمدم", "inventory"}:
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            await self.cmd_show_inventory(user)
            return

        # --- !دنس <emote_id> <ثانیه> : اضافه/آپدیت‌کردن duration توی جدول ---
        # --- !دنس <emote_id> <ثانیه> : فقط وقتی دقیقاً همین فرمت باشه ذخیره می‌کنیم
        # وگرنه (مثل "!دنس اجرا کن برام <لینک>") میره پایین‌تر برای تشخیص خودکار دنس ---
        if lower.startswith("دنس "):
            parts = payload.split()
            looks_like_save_format = len(parts) == 3 and _is_positive_float(parts[2])

            if looks_like_save_format:
                if not self.is_owner(user):
                    await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                    return
                raw_id, seconds_str = parts[1], parts[2]
                emote_id = extract_emote_id(raw_id)
                if not emote_id:
                    await self.highrise.chat(f"@{user.username} آیدی دنس معتبر نیست.")
                    return
                seconds = float(seconds_str)
                self.dances[emote_id] = seconds
                save_dances(self.dances)
                self._refresh_dances_using(emote_id)  # هرکی الان همین دنس رو اجرا می‌کنه، خودکار رفرش میشه
                await self.highrise.chat(f"@{user.username} ذخیره شد ✅ {emote_id} → {seconds} ثانیه")
                return

            # --- !دنس بات <لینک/آیدی> : خودِ بات این دنس رو (بدون توقف) اجرا می‌کنه ---
            if len(parts) >= 2 and parts[1] in {"بات", "bot"}:
                if not self.is_owner(user):
                    await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                    return
                if len(parts) < 3:
                    await self.highrise.chat(f"@{user.username} فرمت درست: !دنس بات <لینک/آیدی>")
                    return
                if parts[2].lower() in {"stop", "استوپ", "متوقف"}:
                    stopped = await self._stop_user_dance(self.BOT_DANCE_KEY)
                    await self.highrise.chat(
                        f"@{user.username} دنس بات متوقف شد ✅" if stopped else f"@{user.username} بات الان دنسی نداره"
                    )
                    return
                emote_id = extract_emote_id(parts[2])
                if not emote_id:
                    await self.highrise.chat(f"@{user.username} آیدی دنس معتبر نیست.")
                    return
                self._start_user_dance(self.BOT_DANCE_KEY, emote_id, target_user_id=None)
                known = emote_id in self.dances
                note = "" if known else " (duration نامشخصه، فقط یه‌بار اجرا میشه)"
                await self.highrise.chat(f"@{user.username} دنس بات شروع شد 💃{note}")
                return

            # فرمت مدیریتی نبود → فال‌ترو به تشخیص خودکار دنس پایین‌تر (find_dance_in_text)

        # --- !اجرا دنس <لینک/آیدی> : اجرای مستقیم بدون نیاز به گفتن ثانیه (اگه از قبل ذخیره شده) ---
        if lower.startswith("اجرا دنس "):
            raw = payload.split(" ", 2)
            if len(raw) < 3:
                await self.highrise.chat(f"@{user.username} فرمت درست: !اجرا دنس <لینک/آیدی>")
                return
            emote_id = extract_emote_id(raw[2].strip())
            if not emote_id:
                await self.highrise.chat(f"@{user.username} آیدی دنس معتبر نیست.")
                return
            self._start_user_dance(user.id, emote_id)
            known = emote_id in self.dances
            note = "" if known else " (duration نامشخصه، فقط یه‌بار اجرا میشه)"
            await self.highrise.chat(f"@{user.username} دنس شروع شد 💃{note}")
            return

        if lower in {"کیف‌پولم", "کیف پولم", "wallet"}:
            if not self.is_owner(user):
                await self.highrise.chat("این دستور فقط برای مالک بات فعاله.")
                return
            await self.cmd_show_wallet(user)
            return

        if lower == "quota":
            if self.is_owner(user):
                await self.highrise.chat(f"@{user.username} تو مالکی، محدودیت نداری 👑")
                return
            today = date.today().isoformat()
            used_date, count = self.daily_usage.get(str(user.id), (today, 0))
            remaining = DAILY_AI_LIMIT - (count if used_date == today else 0)
            await self.highrise.chat(f"@{user.username} امروز {remaining} پیام دیگه از AI می‌تونی بپرسی")
            return

        if not payload:
            return

        # --- تشخیص خودکار دنس داخل جمله (بدون نیاز به AI) ---
        dance_id = find_dance_in_text(payload)
        if dance_id:
            try:
                self._start_user_dance(user.id, dance_id)
                await self.highrise.chat(f"@{user.username} " + random.choice(DANCE_REPLIES))
            except Exception as e:
                print(f"Inline dance error: {e}")
                await self.highrise.chat(f"@{user.username} این دنس اجرا نشد.")
            return

        # --- محدودیت روزانه (مالک محدودیت نداره) ---
        if not self.is_owner(user):
            if not self.check_and_use_quota(str(user.id)):
                await self.highrise.chat(f"@{user.username} سهمیه‌ی امروزت ({DAILY_AI_LIMIT} پیام) تموم شده، فردا دوباره امتحان کن.")
                return

        nearby_context = await self.gather_nearby_context(user)

        reply = await self.ask_gapgpt(str(user.id), user.username, payload, context=nearby_context)
        await self.highrise.chat(reply)
        self.current_ai_partner = (user.id, user.username, time.time())


if __name__ == "__main__":
    room_id = os.environ.get("ROOM_ID")
    api_token = os.environ.get("API_TOKEN")

    if not room_id or not api_token:
        print("ROOM_ID or API_TOKEN is missing.", file=sys.stderr)
        sys.exit(1)

    threading.Thread(target=run_fake_server, daemon=True).start()

    result = subprocess.run(
        ["highrise", "main:Bot", room_id, api_token],
        check=False,
    )
    sys.exit(result.returncode)
