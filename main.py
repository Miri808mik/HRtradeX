import os
import threading
import subprocess
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque

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


# یوزرنیم‌هایی که همیشه به عنوان "مالک" بات شناخته میشن،
# حتی اگه صاحب رسمی همون اتاق نباشن (حروف کوچیک، بدون @)
EXTRA_OWNER_USERNAMES = {"syntaxerror.py"}

# سقف پیام روزانه برای هر کاربر (برای درخواست از AI)
DAILY_AI_LIMIT = 50


class Bot(BaseBot):
    def __init__(self):
        super().__init__()
        self.ai_url = "https://api.gapgpt.app/v1/chat/completions"
        # اگه مدل بهتری روی GapGPT فعال بود، از طریق Environment Variable AI_MODEL عوضش کن
        self.ai_model = os.environ.get("AI_MODEL", "gpt-4.1")
        self.histories = defaultdict(lambda: deque(maxlen=8))
        self.owner_id = None

        # شمارنده‌ی روزانه: user_id -> (تاریخ امروز, تعداد پیام امروز)
        self.daily_usage = {}

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot started")
        self.owner_id = session_metadata.room_info.owner_id

    async def on_user_join(self, user: User, position) -> None:
        await self.highrise.chat(f"سلام {user.username} 👋")

    def is_owner(self, user: User) -> bool:
        if self.owner_id and user.id == self.owner_id:
            return True
        return user.username.lower() in EXTRA_OWNER_USERNAMES

    def check_and_use_quota(self, user_id: str) -> bool:
        """اگه هنوز سهمیه‌ی امروز تموم نشده True برمی‌گردونه و یکی کم می‌کنه."""
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

    async def come_to_user(self, requester: User) -> None:
        """پیدا کردن موقعیت فعلی کاربر و رفتن بات کنارش + نمایش مشخصات"""
        try:
            room_users = await self.highrise.get_room_users()
        except Exception as e:
            print(f"get_room_users error: {e}")
            await self.highrise.chat(f"@{requester.username} نتونستم موقعیتت رو پیدا کنم.")
            return

        target_position = None
        for u, pos in room_users:
            if u.id == requester.id:
                target_position = pos
                break

        if target_position is None:
            await self.highrise.chat(f"@{requester.username} پیدات نکردم توی اتاق.")
            return

        try:
            await self.highrise.walk_to(target_position)
        except Exception as e:
            print(f"walk_to error: {e}")

        await self.highrise.chat(
            f"اومدم پیشت {requester.username} 👋 | یوزرنیم: {requester.username} | آیدی: {requester.id}"
        )

    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip()
        if not text:
            return

        # --- @بیا : بات میاد کنار کاربر و مشخصاتش رو نشون میده ---
        if text.strip() in {"@بیا", "@come", "@بیا اینجا"}:
            await self.come_to_user(user)
            return

        # --- بقیه‌ی پیام‌ها فقط اگه با ! شروع بشن پردازش میشن ---
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

        # --- محدودیت روزانه فقط برای پیام‌های AI اعمال میشه ---
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
