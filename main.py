import os
import threading
import subprocess
import sys
import asyncio
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


def parse_tip_amount(text: str) -> int | None:
    try:
        value = int(text.strip())
        if value <= 0:
            return None
        return value
    except Exception:
        return None


def gold_bar_name(amount: int) -> str | None:
    allowed = {
        1: "gold_bar_1",
        5: "gold_bar_5",
        10: "gold_bar_10",
        50: "gold_bar_50",
        100: "gold_bar_100",
        500: "gold_bar_500",
        1000: "gold_bar_1k",
        5000: "gold_bar_5000",
        10000: "gold_bar_10k",
    }
    return allowed.get(amount)


# ---------- The actual Highrise bot ----------
class Bot(BaseBot):
    BOT_DANCE_ID = "dance-floss"
    BOT_DANCE_DELAY = 8
    USER_DANCE_DELAY = 8

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot has started!")

        self.active_users = {}
        self.user_dance_tasks = {}
        self.bot_dance_task = None

        if self.bot_dance_task is None or self.bot_dance_task.done():
            self.bot_dance_task = asyncio.create_task(self._bot_dance_loop())

    async def on_user_join(self, user: User, position) -> None:
        self.active_users[user.id] = user
        await self.highrise.chat(f"سلام {user.username} خوش اومدی! 👋")
        await self.highrise.chat(f"@{user.username} الان بات داره می‌رقصه و آماده‌ست 🎵")

    async def on_user_leave(self, user: User) -> None:
        self.active_users.pop(user.id, None)

        task = self.user_dance_tasks.pop(user.id, None)
        if task and not task.done():
            task.cancel()

    async def _bot_dance_loop(self) -> None:
        await asyncio.sleep(2)

        while True:
            try:
                await self.highrise.send_emote(self.BOT_DANCE_ID)
            except Exception as e:
                print(f"Auto-dance error: {e}")

            await asyncio.sleep(self.BOT_DANCE_DELAY)

    async def _user_dance_loop(self, user: User, emote_id: str) -> None:
        await asyncio.sleep(1)

        use_target = True
        while True:
            try:
                if use_target:
                    await self.highrise.send_emote(emote_id, target_user_id=user.id)
                else:
                    await self.highrise.send_emote(emote_id)
            except TypeError:
                use_target = False
                try:
                    await self.highrise.send_emote(emote_id)
                except Exception as e:
                    print(f"User dance fallback error: {e}")
            except Exception as e:
                print(f"User dance error: {e}")

            await asyncio.sleep(self.USER_DANCE_DELAY)

    def _start_user_dance(self, user: User, emote_id: str) -> None:
        old_task = self.user_dance_tasks.get(user.id)
        if old_task and not old_task.done():
            old_task.cancel()

        task = asyncio.create_task(self._user_dance_loop(user, emote_id))
        self.user_dance_tasks[user.id] = task

    async def _tip_all(self, amount: int, actor: User) -> None:
        bar_name = gold_bar_name(amount)
        if not bar_name:
            await self.highrise.chat(
                f"@{actor.username} فقط این مقدارها فعلاً پشتیبانی می‌شن: 1, 5, 10, 50, 100, 500, 1000, 5000, 10000"
            )
            return

        if not self.active_users:
            await self.highrise.chat(f"@{actor.username} کسی داخل اتاق نیست که تیپ بگیره.")
            return

        success = 0
        failed = 0

        for uid, user in list(self.active_users.items()):
            try:
                await self.highrise.tip_user(uid, bar_name)
                success += 1
            except Exception as e:
                failed += 1
                print(f"Tip failed for {user.username} ({uid}): {e}")

        if failed == 0:
            await self.highrise.chat(f"@{actor.username} به {success} نفر، هر نفر {amount} گلد تیپ شد ✅")
        else:
            await self.highrise.chat(
                f"@{actor.username} تیپ انجام شد ✅ | موفق: {success} | ناموفق: {failed}"
            )

    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip()
        lower = text.lower()

        if lower == "!ping":
            await self.highrise.chat("pong 🏓")
            return

        # Tip command:
        # tip all 5
        if lower.startswith("tip all "):
            parts = text.split()
            if len(parts) != 3:
                await self.highrise.chat(f"@{user.username} فرمت درست: tip all 5")
                return

            amount = parse_tip_amount(parts[2])
            if amount is None:
                await self.highrise.chat(f"@{user.username} عدد تیپ معتبر نیست.")
                return

            await self._tip_all(amount, user)
            return

        # Optional manual dance command:
        # /dance-twerk
        # /https://high.rs/item?id=dance-twerk&type=emote
        if not text.startswith("/"):
            return

        payload = text[1:].strip()
        if not payload:
            await self.highrise.chat(f"@{user.username} فرمت درست نیست. مثال: /dance-twerk")
            return

        emote_id = extract_emote_id(payload)
        if not emote_id:
            await self.highrise.chat(f"@{user.username} دنس معتبر نیست.")
            return

        self._start_user_dance(user, emote_id)
        await self.highrise.chat(f"@{user.username} دنس {emote_id} شروع شد و هی تکرار میشه ✅")


if __name__ == "__main__":
    room_id = os.environ.get("ROOM_ID")
    api_token = os.environ.get("API_TOKEN")

    if not room_id or not api_token:
        print("ROOM_ID or API_TOKEN is missing.", file=sys.stderr)
        sys.exit(1)

    # Start fake HTTP server in a background thread
    threading.Thread(target=run_fake_server, daemon=True).start()

    # Run the Highrise bot using the official CLI entrypoint
    result = subprocess.run(
        ["highrise", "main:Bot", room_id, api_token],
        check=False,
    )
    sys.exit(result.returncode)
