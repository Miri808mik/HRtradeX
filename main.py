import os
import threading
import subprocess
import sys
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


class Bot(BaseBot):
    def __init__(self):
        super().__init__()
        self.ai_url = "https://api.gapgpt.app/v1/chat/completions"
        self.ai_model = "gpt-4o"
        self.histories = defaultdict(lambda: deque(maxlen=8))

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot started")

    async def on_user_join(self, user: User, position) -> None:
        await self.highrise.chat(f"سلام {user.username} 👋")

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

    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip()

        if not text:
            return

        if text.startswith("!"):
            cmd = text.lower()

            if cmd == "!ping":
                await self.highrise.chat("pong 🏓")
                return

            if cmd == "!clear":
                self.histories.pop(str(user.id), None)
                await self.highrise.chat(f"@{user.username} حافظه پاک شد ✅")
                return

            return

        reply = await self.ask_gapgpt(str(user.id), user.username, text)
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
