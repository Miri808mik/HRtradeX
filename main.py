import os
import threading
import subprocess
import sys
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

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


def extract_emote_id(raw_value: str) -> str | None:
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


def parse_tip_amount(text: str) -> int | None:
    try:
        amount = int(text.strip())
        return amount if amount > 0 else None
    except Exception:
        return None


def gold_bar_name(amount: int) -> str | None:
    mapping = {
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
    return mapping.get(amount)


class Bot(BaseBot):
    BOT_DANCE_ID = "dance-floss"
    BOT_DANCE_DELAY = 0.8
    USER_DANCE_DELAY = 0.8

    def __init__(self):
        super().__init__()
        self.active_users: dict[str, User] = {}
        self.user_dance_tasks: dict[str, asyncio.Task] = {}
        self.bot_dance_task: asyncio.Task | None = None

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot has started!")

        if self.bot_dance_task is None or self.bot_dance_task.done():
            self.bot_dance_task = asyncio.create_task(self._bot_dance_loop())

    async def on_user_join(self, user: User, position) -> None:
        self.active_users[user.id] = user
        await self.highrise.chat(f"سلام {user.username} ✨ خوش اومدی")

    async def on_user_leave(self, user: User) -> None:
        self.active_users.pop(user.id, None)

        task = self.user_dance_tasks.pop(user.id, None)
        if task and not task.done():
            task.cancel()

    async def _bot_dance_loop(self) -> None:
        await asyncio.sleep(1)

        while True:
            try:
                await self.highrise.send_emote(self.BOT_DANCE_ID)
            except Exception as e:
                print(f"Bot dance error: {e}")

            await asyncio.sleep(self.BOT_DANCE_DELAY)

    async def _user_dance_loop(self, user: User, emote_id: str) -> None:
        await asyncio.sleep(0.5)

        while True:
            try:
                await self.highrise.send_emote(emote_id, target_user_id=user.id)
            except TypeError:
                try:
                    await self.highrise.send_emote(emote_id)
                except Exception as e:
                    print(f"User dance fallback error: {e}")
                    return
            except Exception as e:
                print(f"User dance error: {e}")
                return

            await asyncio.sleep(self.USER_DANCE_DELAY)

    def _start_user_dance(self, user: User, emote_id: str) -> None:
        old_task = self.user_dance_tasks.get(user.id)
        if old_task and not old_task.done():
            old_task.cancel()

        task = asyncio.create_task(self._user_dance_loop(user, emote_id))
        self.user_dance_tasks[user.id] = task

    def _stop_user_dance(self, user: User) -> bool:
        task = self.user_dance_tasks.pop(user.id, None)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def _tip_all(self, amount: int, actor: User) -> None:
        bar_name = gold_bar_name(amount)
        if not bar_name:
            await self.highrise.chat(
                f"@{actor.username} فعلاً فقط این مقدارها پشتیبانی می‌شن: 1، 5، 10، 50، 100، 500، 1000، 5000، 10000"
            )
            return

        targets = [
            u for uid, u in self.active_users.items()
            if uid != actor.id
        ]

        if not targets:
            await self.highrise.chat(f"@{actor.username} کسی داخل اتاق نیست.")
            return

        success = 0
        failed = 0

        for target in targets:
            try:
                await self.highrise.tip_user(target.id, bar_name)
                success += 1
            except Exception as e:
                failed += 1
                print(f"Tip failed for {target.username} ({target.id}): {e}")

        if failed == 0:
            await self.highrise.chat(f"@{actor.username} تیپ انجام شد ✅")
        else:
            await self.highrise.chat(f"@{actor.username} تیپ انجام شد ✅ | موفق: {success} | ناموفق: {failed}")

    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip()
        lower = text.casefold()

        if lower == "!ping":
            await self.highrise.chat("pong 🏓")
            return

        if lower in {"stop", "متوقف", "استوپ"}:
            if self._stop_user_dance(user):
                await self.highrise.chat(f"@{user.username} متوقف شد ✅")
            else:
                await self.highrise.chat(f"@{user.username} دنس فعالی نداری")
            return

        if lower.startswith("tip all "):
            parts = text.split()
            if len(parts) != 3:
                await self.highrise.chat(f"@{user.username} فرمت درست: tip all 5")
                return

            amount = parse_tip_amount(parts[2])
            if amount is None:
                await self.highrise.chat(f"@{user.username} عدد معتبر نیست")
                return

            await self._tip_all(amount, user)
            return

        if not text.startswith("/"):
            return

        payload = text[1:].strip()
        if not payload:
            await self.highrise.chat(f"@{user.username} فرمت درست نیست")
            return

        emote_id = extract_emote_id(payload)
        if not emote_id:
            await self.highrise.chat(f"@{user.username} دنس معتبر نیست")
            return

        self._start_user_dance(user, emote_id)
        await self.highrise.chat(f"@{user.username} اجرا شد ✅")


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
