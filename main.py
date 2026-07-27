import os
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from highrise import BaseBot, User, SessionMetadata


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
    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot has started!")

    async def on_user_join(self, user: User, position) -> None:
        await self.highrise.chat(f"سلام {user.username} خوش اومدی! 👋")

    async def on_chat(self, user: User, message: str) -> None:
        if message.lower() == "!ping":
            await self.highrise.chat("pong 🏓")


if __name__ == "__main__":
    # Start fake HTTP server in a background thread
    threading.Thread(target=run_fake_server, daemon=True).start()

    # Run the Highrise bot
    from highrise.__main__ import main as highrise_main

    room_id = os.environ.get("ROOM_ID")
    api_token = os.environ.get("API_TOKEN")

    import sys
    sys.argv = ["highrise", "main:Bot", room_id, api_token]
    highrise_main()
