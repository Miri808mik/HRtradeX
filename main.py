import os
import threading
import subprocess
import sys
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import aiohttp
from highrise import BaseBot, SessionMetadata, User


# ---------- Fake HTTP server so Render's free Web Service stays alive ----------
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


async def ask_ai(user_text: str) -> str:
    api_key = os.environ.get("AI_API_KEY", "").strip()
    base_url = os.environ.get("AI_BASE_URL", "").strip().rstrip("/")
    model = os.environ.get("AI_MODEL", "default").strip()

    if not api_key or not base_url:
        return "توکن هوش مصنوعی تنظیم نشده."

    url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "تو یک دستیار کوتاه، صمیمی و فارسی‌زبان برای چت داخل بازی هستی.",
            },
            {
                "role": "user",
                "content": user_text,
            },
        ],
        "temperature": 0.7,
    }

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            text = await resp.text()

            if resp.status != 200:
                return f"خطا در پاسخ هوش مصنوعی: {resp.status}"

            try:
                data = await resp.json()
            except Exception:
                return "پاسخ هوش مصنوعی قابل خواندن نبود."

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return "هوش مصنوعی جواب نداد."


class Bot(BaseBot):
    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot started")

    async def on_user_join(self, user: User, position) -> None:
        await self.highrise.chat(f"سلام {user.username} 👋")

    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip()

        if text == "!ping":
            await self.highrise.chat("pong 🏓")
            return

        if text.startswith("!سلام"):
            prompt = f"به فارسی، خیلی کوتاه و دوستانه به این پیام جواب بده: {text}"
            reply = await ask_ai(prompt)
            await self.highrise.chat(reply[:250])
            return

        if text.startswith("!ai "):
            prompt = text[4:].strip()
            if not prompt:
                await self.highrise.chat("متن هوش مصنوعی خالیه.")
                return

            reply = await ask_ai(prompt)
            await self.highrise.chat(reply[:250])
            return


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
