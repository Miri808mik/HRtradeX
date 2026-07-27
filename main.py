import os
import threading
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from openai import AsyncOpenAI
from highrise import BaseBot, SessionMetadata, User


# ---------- Fake HTTP server so Render's free Web Service stays alive ----------
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, format, *args):
        pass  # keep logs clean


def run_fake_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


# ---------- GapGPT client ----------
gap_client = AsyncOpenAI(
    api_key=os.environ.get("AI_API_KEY", ""),
    base_url="https://api.gapgpt.app/v1",
)


async def ask_ai(user_text: str) -> str:
    if not os.environ.get("AI_API_KEY"):
        return "AI_API_KEY تنظیم نشده."

    try:
        response = await gap_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "تو یک دستیار فارسی کوتاه، صمیمی و مفید برای چت داخل بازی Highrise هستی. پاسخ‌ها کوتاه و طبیعی باشند.",
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            temperature=0.7,
        )

        reply = response.choices[0].message.content or ""
        reply = reply.strip()

        if not reply:
            return "پاسخی نداد."

        return reply[:250]

    except Exception as e:
        print(f"AI error: {e}")
        return "خطا در ارتباط با هوش مصنوعی."


# ---------- The actual Highrise bot ----------
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

        if text == "!سلام":
            reply = await ask_ai("به فارسی و خیلی کوتاه جواب بده: سلام!")
            await self.highrise.chat(reply)
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
