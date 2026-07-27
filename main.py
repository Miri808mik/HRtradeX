import os
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from highrise import BaseBot, User, SessionMetadata


# ---------- Default dance commands ----------
# اسم دستور -> emote_id واقعی Highrise
# می‌تونی بعداً هر emote_id دیگه‌ای که پیدا کردی رو همینجا اضافه کنی
DEFAULT_DANCES = {
    "macarena": "dance-macarena",
    "hello": "emote-hello",
    "tired": "emote-tired",
}

# رقص‌هایی که از داخل چت با !adddance اضافه میشن (فقط تا وقتی بات ری‌استارت نشده باقی می‌مونن)
runtime_dances = {}


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


# ---------- The actual Highrise bot ----------
class Bot(BaseBot):
    def __init__(self):
        super().__init__()
        self.owner_id = None

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot has started!")
        self.owner_id = session_metadata.room_info.owner_id

    async def on_user_join(self, user: User, position) -> None:
        await self.highrise.chat(f"سلام {user.username} خوش اومدی! 👋")

    def get_dance_id(self, name: str):
        name = name.lower()
        if name in runtime_dances:
            return runtime_dances[name]
        return DEFAULT_DANCES.get(name)

    async def on_chat(self, user: User, message: str) -> None:
        if not message.startswith("!"):
            return

        parts = message[1:].split(" ", 1)
        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        # --- !ping ---
        if command == "ping":
            await self.highrise.chat("pong 🏓")
            return

        # --- !dancelist : لیست رقص‌های موجود ---
        if command == "dancelist":
            all_names = list(DEFAULT_DANCES.keys()) + list(runtime_dances.keys())
            await self.highrise.chat("رقص‌های موجود: " + ", ".join(all_names))
            return

        # --- !adddance <name> <emote_id> : فقط برای صاحب اتاق ---
        if command == "adddance":
            if user.id != self.owner_id:
                await self.highrise.chat("فقط صاحب اتاق می‌تونه رقص جدید اضافه کنه.")
                return
            args = rest.split(" ", 1)
            if len(args) < 2:
                await self.highrise.chat("فرمت درست: !adddance اسم emote_id")
                return
            new_name, new_emote_id = args[0].lower(), args[1].strip()
            runtime_dances[new_name] = new_emote_id
            await self.highrise.chat(f"رقص '{new_name}' اضافه شد ✅ (تا ریست بعدی می‌مونه)")
            return

        # --- !<dance_name> [متن اختیاری] : اجرای رقص + ارسال پیام ---
        emote_id = self.get_dance_id(command)
        if emote_id:
            try:
                await self.highrise.send_emote(emote_id)
            except Exception as e:
                print(f"Emote error: {e}")
            if rest:
                await self.highrise.chat(rest)


if __name__ == "__main__":
    # Start fake HTTP server in a background thread
    threading.Thread(target=run_fake_server, daemon=True).start()

    # Run the Highrise bot using the SDK's programmatic API
    from highrise.__main__ import main as highrise_main, BotDefinition
    from asyncio import run as arun

    room_id = os.environ.get("ROOM_ID")
    api_token = os.environ.get("API_TOKEN")

    # Debug: confirm the environment variables were actually read
    print(f"DEBUG: ROOM_ID = {repr(room_id)}")
    print(f"DEBUG: API_TOKEN length = {len(api_token) if api_token else 'None/empty'}")

    if not room_id or not api_token:
        raise SystemExit("ERROR: ROOM_ID or API_TOKEN environment variable is missing!")

    definitions = [BotDefinition(Bot(), room_id, api_token)]
    arun(highrise_main(definitions))
