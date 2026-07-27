import os
import threading
import asyncio
from contextlib import suppress
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


def parse_tip_amount(text: str):
    try:
        amount = int(text.strip())
        return amount if amount > 0 else None
    except Exception:
        return None


def gold_bar_name(amount: int):
    mapping = {
        1: "gold_bar_1", 5: "gold_bar_5", 10: "gold_bar_10", 50: "gold_bar_50",
        100: "gold_bar_100", 500: "gold_bar_500", 1000: "gold_bar_1k",
        5000: "gold_bar_5000", 10000: "gold_bar_10k",
    }
    return mapping.get(amount)


class Bot(BaseBot):
    DEFAULT_INTERVAL = 4.5   # مقدار پیش‌فرض (با تست دقیق‌ترش کن)

    def __init__(self):
        super().__init__()
        self.active_users: dict[str, User] = {}
        self.user_dance_states: dict[str, tuple[asyncio.Event, asyncio.Task]] = {}
        self.bot_dance_task: asyncio.Task | None = None
        self.owner_id = None
        self.emote_intervals: dict[str, float] = {}
        self.bot_dance_id = "dance-floss"

    def get_interval(self, emote_id: str) -> float:
        return self.emote_intervals.get(emote_id, self.DEFAULT_INTERVAL)

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Bot has started!")
        self.owner_id = session_metadata.room_info.owner_id

        if self.bot_dance_task is None or self.bot_dance_task.done():
            self.bot_dance_task = asyncio.create_task(self._bot_dance_loop())

    async def on_user_join(self, user: User, position) -> None:
        self.active_users[user.id] = user
        await self.highrise.chat(f"سلام {user.username} ✨ خوش اومدی")

    async def on_user_leave(self, user: User) -> None:
        self.active_users.pop(user.id, None)
        await self._stop_user_dance(user.id)

    # ==================== لوپ دنس بات ====================
    async def _bot_dance_loop(self) -> None:
        await asyncio.sleep(1.5)
        while True:
            try:
                await self.highrise.send_emote(self.bot_dance_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"Bot dance error: {e}")

            await asyncio.sleep(self.get_interval(self.bot_dance_id))

    # ==================== لوپ دنس کاربر (نسخه تمیز) ====================
    async def _user_dance_loop(self, user_id: str, emote_id: str, stop_event: asyncio.Event) -> None:
        # یه مکث خیلی کوتاه اول کار که دستور قبلی کامل تموم بشه
        await asyncio.sleep(0.3)

        while not stop_event.is_set():
            try:
                await self.highrise.send_emote(emote_id, target_user_id=user_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"User dance error ({user_id}): {e}")
                # اگه خطا داد، کمی صبر کن و دوباره امتحان کن (کاربر ممکنه موقتاً مشکل داشته باشه)
                await asyncio.sleep(1.5)
                continue

            interval = self.get_interval(emote_id)

            # صبر کردن تا یا زمان تموم بشه یا دستور stop بیاد
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass  # زمان تموم شد → دوباره دنس بفرست

    def _start_user_dance(self, user: User, emote_id: str) -> None:
        # اگه قبلاً دنس داشته، کامل متوقفش کن
        old_state = self.user_dance_states.get(user.id)
        if old_state:
            old_event, old_task = old_state
            old_event.set()
            if not old_task.done():
                old_task.cancel()

        stop_event = asyncio.Event()
        task = asyncio.create_task(self._user_dance_loop(user.id, emote_id, stop_event))
        self.user_dance_states[user.id] = (stop_event, task)

    async def _stop_user_dance(self, user_id: str) -> bool:
        state = self.user_dance_states.pop(user_id, None)
        if not state:
            return False

        stop_event, task = state
        stop_event.set()
        if not task.done():
            task.cancel()

        with suppress(asyncio.CancelledError):
            await task
        return True

    # ==================== تیپ ====================
    async def _tip_all(self, amount: int, actor: User) -> None:
        bar_name = gold_bar_name(amount)
        if not bar_name:
            await self.highrise.chat(
                f"@{actor.username} فعلاً فقط این مقدارها پشتیبانی می‌شن: 1، 5، 10، 50، 100، 500، 1000، 5000، 10000"
            )
            return

        targets = [u for uid, u in self.active_users.items() if uid != actor.id]
        if not targets:
            await self.highrise.chat(f"@{actor.username} کسی داخل اتاق نیست.")
            return

        success = failed = 0
        for target in targets:
            try:
                await self.highrise.tip_user(target.id, bar_name)
                success += 1
            except Exception as e:
                failed += 1
                print(f"Tip failed for {target.username}: {e}")

        if failed == 0:
            await self.highrise.chat(f"@{actor.username} تیپ انجام شد ✅")
        else:
            await self.highrise.chat(f"@{actor.username} تیپ انجام شد ✅ | موفق: {success} | ناموفق: {failed}")

    # ==================== چت ====================
    async def on_chat(self, user: User, message: str) -> None:
        text = message.strip()
        lower = text.casefold()

        if lower == "!ping":
            await self.highrise.chat("pong 🏓")
            return

        if lower in {"stop", "متوقف", "استوپ", "!stop"}:
            stopped = await self._stop_user_dance(user.id)
            await self.highrise.chat(
                f"@{user.username} متوقف شد ✅" if stopped else f"@{user.username} دنس فعالی نداری"
            )
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

        # تنظیم زمان‌بندی زنده
        if lower.startswith("!settiming"):
            if user.id != self.owner_id:
                await self.highrise.chat("فقط صاحب اتاق می‌تونه زمان‌بندی رو عوض کنه.")
                return

            parts = text.split()
            if len(parts) != 3:
                await self.highrise.chat("فرمت درست: !settiming emote_id 3.8")
                return

            emote_id, seconds_str = parts[1], parts[2]
            try:
                seconds = float(seconds_str)
                if seconds < 1.5:
                    await self.highrise.chat("حداقل زمان ۱.۵ ثانیه باشه.")
                    return
            except ValueError:
                await self.highrise.chat("عدد ثانیه معتبر نیست.")
                return

            self.emote_intervals[emote_id] = seconds
            await self.highrise.chat(f"زمان‌بندی `{emote_id}` روی **{seconds}** ثانیه تنظیم شد ✅")
            return

        # دستور دنس: /emote_id یا /لینک
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
        interval = self.get_interval(emote_id)
        await self.highrise.chat(f"@{user.username} اجرا شد ✅ (هر {interval} ثانیه)")


if __name__ == "__main__":
    threading.Thread(target=run_fake_server, daemon=True).start()

    from highrise.__main__ import main as highrise_main, BotDefinition
    from asyncio import run as arun

    room_id = os.environ.get("ROOM_ID")
    api_token = os.environ.get("API_TOKEN")

    print(f"DEBUG: ROOM_ID = {repr(room_id)}")
    print(f"DEBUG: API_TOKEN length = {len(api_token) if api_token else 'None/empty'}")

    if not room_id or not api_token:
        raise SystemExit("ERROR: ROOM_ID or API_TOKEN environment variable is missing!")

    definitions = [BotDefinition(Bot(), room_id, api_token)]
    arun(highrise_main(definitions))
