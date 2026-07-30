import os
import re
import json
import time
import random
import threading
import subprocess
import sys
import asyncio
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque
from urllib.parse import parse_qs, urlparse

import aiohttp
from highrise import BaseBot, SessionMetadata, User, Position, AnchorPosition, Error


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

    def is_owner(self, user: User) -> bool:
        if self.owner_id and user.id == self.owner_id:
            return True
        return user.username.lower() in EXTRA_OWNER_USERNAMES

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
                    "تو یه رفیق صمیمی توی روم Highrise هستی، نه یه بات رسمی. "
                    "خیلی خیلی کوتاه جواب بده (یکی دو خط، مثل چت واقعی با دوست). "
                    "لحنت باحال، خودمونی و بامزه باشه، نه رسمی و خشک. "
                    "اگه بین دوستا شوخی، طعنه، یا فحش دوستانه رد و بدل میشه، جدیش نگیر و باهاشون همراهی/شوخی کن، "
                    "اصلاً نصیحت نکن و نگو 'لطفاً محترمانه صحبت کنید' یا مشابهش — "
                    "فقط وقتی واقعاً یه دعوای جدی و پر از توهین می‌بینی جدی برخورد کن (اونم نه با نصیحت، فقط با گزارش به تیم مدیریت که جدا هندل میشه). "
                    "جواب‌ها غیرتکراری و طبیعی باشن."
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
                    "یه بات فارسیِ Highrise هستی که الان داره به سمت یه کاربر راه میره چون صداش زده. "
                    "فقط یه جمله‌ی خیلی کوتاه (حداکثر ۱۰ کلمه) و طبیعی بگو که داری میای، "
                    "و اگه کاربر دلیلی برای صدا زدن گفته (مثلاً 'کارت دارم')، ازش بپرس چیکار داره. "
                    "بدون توضیح اضافه، فقط همون یه جمله."
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
                try:
                    await self.highrise.send_emote("emote-hello", user.id)
                    await self.highrise.chat(f"@{user.username} دنس متوقف شد ✅")
                except Exception as e:
                    print(f"Stop error: {e}")
                return
            emote_id = extract_emote_id(payload)
            if not emote_id:
                await self.highrise.chat(f"@{user.username} فرمت دنس معتبر نیست.")
                return
            try:
                await self.highrise.send_emote(emote_id, user.id)
                await self.highrise.chat(f"@{user.username} دنس شروع شد 💃 (توقف: /stop)")
            except Exception as e:
                print(f"Dance error: {e}")
                await self.highrise.chat(f"@{user.username} این دنس اجرا نشد.")
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
                await self.highrise.send_emote(dance_id, user.id)
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
