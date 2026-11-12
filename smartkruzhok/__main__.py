import asyncio
import fcntl
import logging
import os
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError

from .feedback import feedback_routes
from .store import Store
from .ui import COMMANDS, routes
from .worker import Worker


async def main(token, root, feedback_owner):
    session = AiohttpSession(timeout=45)
    async with Bot(token, session=session) as bot:
        try:
            await bot.set_my_commands(COMMANDS, request_timeout=5)
        except TelegramAPIError as error:
            logging.getLogger(__name__).warning("menu_setup_failed code=%s", type(error).__name__)
        dispatcher = Dispatcher()
        store = Store(root / "bot.sqlite3")
        dispatcher.include_router(feedback_routes(store, feedback_owner))
        dispatcher.include_router(routes(store))
        stop = asyncio.Event()
        worker = asyncio.create_task(Worker(store, bot, root / "clips").run(stop))

        async def poll():
            await dispatcher.start_polling(bot, handle_as_tasks=False, close_bot_session=False)

        polling = asyncio.create_task(poll())
        try:
            completed, _ = await asyncio.wait((worker, polling), return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                task.result()
        finally:
            stop.set()
            if not polling.done():
                polling.cancel()
            await asyncio.gather(worker, polling, return_exceptions=True)
            store.close()


if __name__ == "__main__":
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Задай BOT_TOKEN в переменных окружения.")
    feedback_owner = os.getenv("FEEDBACK_OWNER_ID", "").strip()
    if feedback_owner and (not feedback_owner.isascii() or not feedback_owner.isdecimal()
                           or not 0 < int(feedback_owner) < 2**52):
        raise SystemExit("FEEDBACK_OWNER_ID должен быть числовым идентификатором пользователя Telegram.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("Установи FFmpeg и ffprobe.")
    os.umask(0o077)
    root = Path(os.getenv("DATA_DIR", "data")).resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    (root / "clips").mkdir(mode=0o700, exist_ok=True)
    os.environ.setdefault("WHISPER_CACHE", str(root / "models"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with (root / "bot.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Бот с этим каталогом данных уже запущен.")
        asyncio.run(main(token, root, int(feedback_owner) if feedback_owner else None))
