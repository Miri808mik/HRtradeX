import os
import re
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

# آدرس عمومی سرویس روی Render، برای بیدار نگه‌داشتنش
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://hrtradex.onrender.com")


class Bot(BaseBot):
    def __init__(self):
        super().__init__()
        self.ai_url = "https://api.gapgpt.app/v1/chat/completions"
        self.ai_model = os.environ.get("AI_MODEL", "gpt-4.1")
        self.histories = defaultdict(lambda: deque(maxlen=8))
        self.owner_id = None
        self.daily_usage = {}
        self._keepalive_task = None

        # قفل حرکت: وقتی True باشه بات هیچ‌جا نمی‌ره
        self.movement_locked = False
        # کاربری که الان بات داره براش میره (برای جلوگیری از تداخل چند درخواست هم‌زمان)
        self.busy_with_username = None

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot started")
        self.owner_id = session_metadata.room_info.owner_id
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def on_user_join(self, user: User, position) -> None:
        await self.highrise.chat(f"سلام {user.username} 👋")

    def is_owner(self, user: User) -> bool:
        if self.owner_id and user.id == self.owner_id:
            return True
        return user.username.lower() in EXTRA_OWNER_USERNAMES

    # ---------- بیدار نگه‌داشتن Render (پلن رایگان) ----------
    async def _keepalive_loop(self) -> None:
        # منتظر می‌مونیم بات کامل بالا بیاد
        await asyncio.sleep(30)
        while True:
            # فاصله‌ی تصادفی بین ۶ تا ۱۲ دقیقه، تا همیشه یه عدد ثابت نباشه
            wait_seconds = random.randint(6 * 60, 12 * 60)
            await asyncio.sleep(wait_seconds)
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

    async def ask_gapgpt(self, user_id: str, username: str, text: str) -> str:
        api_key = os.environ.get("AI_API_KEY", "").strip()
        if not api_key:
            return "AI_API_KEY تنظیم نشده."

        messages = [
            {
                "role": "system",
                "content": (
                    "تو یک بات فارسی برای Highrise هستی. "
                    "طبیعی، کوتاه، صمیمی و مثل چت واقعی جواب بده. "
                    "جواب‌ها کوتاه و غیرتکراری باشند."
                ),
            }
        ]
        for item in self.histories[user_id]:
            messages.append(item)
        messages.append({"role": "user", "content": f"{username}: {text}"})

        payload = {
            "model": self.ai_model,
            "messages": messages,
            "temperature": 0.8,
            "max_tokens": 300,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=25)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.ai_url, headers=headers, json=payload) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200:
                        print("GapGPT error:", resp.status, data)
                        return "فعلاً نتونستم جواب بدم."
                    reply = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        .strip()
                    )
                    if not reply:
                        return "جوابی نگرفتم."
                    self.histories[user_id].append({"role": "user", "content": f"{username}: {text}"})
                    self.histories[user_id].append({"role": "assistant", "content": reply})
                    return reply[:250]
        except Exception as e:
            print("AI request error:", e)
            return "مشکل در ارتباط با هوش مصنوعی."

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

        # --- تشخیص نیت «بیا اینجا/کنارم/پیشم» : بات میره کنار هر کسی که این‌رو بگه ---
        if is_come_here_request(payload):
            reply_text = await self.ask_come_reply(user.username, payload)
            await self.go_greet_user(user, reply_text)
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

        reply = await self.ask_gapgpt(str(user.id), user.username, payload)
        await self.highrise.chat(reply)


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
