import os
import threading
import subprocess
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque
from urllib.parse import parse_qs, urlparse

import aiohttp
from highrise import BaseBot, SessionMetadata, User


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


# یوزرنیم‌هایی که همیشه "مالک" شناخته میشن (حروف کوچیک)
EXTRA_OWNER_USERNAMES = {"syntaxerror.py"}

DAILY_AI_LIMIT = 20


class Bot(BaseBot):
    def __init__(self):
        super().__init__()
        self.ai_url = "https://api.gapgpt.app/v1/chat/completions"
        self.ai_model = os.environ.get("AI_MODEL", "gpt-4.1")
        self.histories = defaultdict(lambda: deque(maxlen=8))
        self.owner_id = None
        self.daily_usage = {}

        # سیستم دنس: user_id -> emote_id فعلی
        self.user_dance_states = {}

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot started")
        self.owner_id = session_metadata.room_info.owner_id

    async def on_user_join(self, user: User, position) -> None:
        await self.highrise.chat(f"سلام {user.username} 👋")

    async def on_user_leave(self, user: User) -> None:
        await self._stop_user_dance(user.id)

    def is_owner(self, user: User) -> bool:
        if self.owner_id and user.id == self.owner_id:
            return True
        return user.username.lower() in EXTRA_OWNER_USERNAMES

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

    # ---------- سیستم دنس ----------
    async def _stop_user_dance(self, user_id) -> bool:
        had_active = user_id in self.user_dance_states
        self.user_dance_states.pop(user_id, None)
        if had_active:
            # یه ایموت کوتاه و خنثی می‌فرستیم تا لوپ رقص فعلی رو قطع کنه
            try:
                await self.highrise.send_emote("emote-hello", user_id)
            except Exception as e:
                print(f"Stop emote error: {e}")
        return had_active

    # ---------- @بیا : اومدن کنار کاربر ----------
    async def come_to_user(self, requester: User) -> None:
        try:
            room_users = await self.highrise.get_room_users()
        except Exception as e:
            print(f"get_room_users error: {e}")
            await self.highrise.chat(f"@{requester.username} نتونستم موقعیتت رو پیدا کنم.")
            return

        # دیباگ: دقیقاً ببینیم SDK چه فرمتی برمی‌گردونه
        print(f"DEBUG room_users type: {type(room_users)}")
        if room_users:
            print(f"DEBUG first item: {room_users[0]!r}")

        target_position = None
        try:
            for item in room_users:
                # حالت ۱: تاپل (User, Position)
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    u, pos = item
                # حالت ۲: آبجکتی با .user و .position
                elif hasattr(item, "user") and hasattr(item, "position"):
                    u, pos = item.user, item.position
                else:
                    u, pos = None, None

                if u is not None and getattr(u, "id", None) == requester.id:
                    target_position = pos
                    break
        except Exception as e:
            print(f"parsing room_users error: {e}")

        if target_position is None:
            await self.highrise.chat(f"@{requester.username} پیدات نکردم توی اتاق (شاید بات نسخه‌ی این متد رو پشتیبانی نکنه).")
            return

        try:
            await self.highrise.walk_to(target_position)
        except Exception as e:
            print(f"walk_to error: {e}")
            await self.highrise.chat(f"@{requester.username} پیدات کردم ولی نتونستم بیام کنارت.")
            return

        await self.highrise.chat(
            f"اومدم پیشت {requester.username} 👋 | یوزرنیم: {requester.username} | آیدی: {requester.id}"
        )

    # ---------- AI ----------
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

    # ---------- چت اصلی ----------
    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip()
        if not text:
            return

        # --- @بیا ---
        if text in {"@بیا", "@come", "@بیا اینجا"}:
            await self.come_to_user(user)
            return

        # --- /emote_id یا لینک دنس : اجرای دنس تکرارشونده ---
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
                await self.highrise.chat(f"@{user.username} فرمت دنس معتبر نیست. مثال: /dance-macarena یا لینک high.rs")
                return

            try:
                await self.highrise.send_emote(emote_id, user.id)
            except Exception as e:
                print(f"Dance start error: {e}")
                await self.highrise.chat(f"@{user.username} این دنس اجرا نشد، آیدیش رو چک کن.")
                return

            self.user_dance_states[user.id] = emote_id
            await self.highrise.chat(f"@{user.username} دنس شروع شد 💃 (برای توقف بنویس: /stop)")
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

        if lower == "quota":
            today = date.today().isoformat()
            used_date, count = self.daily_usage.get(str(user.id), (today, 0))
            remaining = DAILY_AI_LIMIT - (count if used_date == today else 0)
            await self.highrise.chat(f"@{user.username} امروز {remaining} پیام دیگه از AI می‌تونی بپرسی")
            return

        if not payload:
            return

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
