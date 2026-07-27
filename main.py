import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

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


def extract_emote_id(raw_value: str) -> str | None:
    value = raw_value.strip()
    if not value:
        return None

    # Accept either a plain emote id or a Highrise item link.
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if "high.rs" not in parsed.netloc:
            return None

        query = parse_qs(parsed.query)
        emote_id = query.get("id", [None])[0]
        return emote_id.strip() if emote_id else None

    if value.startswith(("high.rs/", "www.high.rs/")):
        return extract_emote_id("https://" + value)

    # Plain emote id
    if " " in value:
        return None

    return value


# ---------- The actual Highrise bot ----------
class Bot(BaseBot):
    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot has started!")

    async def on_user_join(self, user: User, position) -> None:
        await self.highrise.chat(f"سلام {user.username} خوش اومدی! 👋")

    async def on_chat(self, user: User, message: str) -> None:
        if message.lower() == "!ping":
            await self.highrise.chat("pong 🏓")
            return

        # Dance command format:
        # /dance-twerk
        # /https://high.rs/item?id=dance-twerk&type=emote
        if not message.startswith("/"):
            return

        payload = message[1:].strip()
        if not payload:
            await self.highrise.chat(f"@{user.username} فرمت درست نیست. مثال: /dance-twerk")
            return

        emote_id = extract_emote_id(payload)
        if not emote_id:
            await self.highrise.chat(f"@{user.username} دنس معتبر نیست.")
            return

        # First try to apply the emote to the same player.
        try:
            await self.highrise.send_emote(emote_id, target_user_id=user.id)
            return
        except TypeError:
            # Older SDKs or signature mismatches may not accept target_user_id.
            pass
        except Exception:
            # If targeting fails for any reason, fallback to self-emote below.
            pass

        # Fallback: let the bot perform the emote itself.
        try:
            await self.highrise.send_emote(emote_id)
        except Exception:
            await self.highrise.chat(f"@{user.username} اجرای این دنس ممکن نیست.")


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
