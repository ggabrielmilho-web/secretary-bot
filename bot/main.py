import asyncio
import logging
import os
from datetime import time
from zoneinfo import ZoneInfo

import aiohttp.web
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

TZ_SP = ZoneInfo("America/Sao_Paulo")


async def run_all() -> None:
    from bot.config import settings
    from bot.database.connection import init_db
    from bot.integrations.google_calendar import init_google_calendar
    from bot.handlers.whatsapp_handler import create_whatsapp_app

    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurado no .env")

    os.environ.setdefault("OPENAI_API_KEY", settings.OPENAI_API_KEY)

    await init_db()
    init_google_calendar(settings.GOOGLE_SERVICE_ACCOUNT_FILE)

    # Webhook WhatsApp sempre ativo
    whatsapp_app = create_whatsapp_app()
    runner = aiohttp.web.AppRunner(whatsapp_app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", settings.WEBHOOK_PORT)
    await site.start()
    logger.info(f"Webhook WhatsApp escutando na porta {settings.WEBHOOK_PORT}")

    if settings.TELEGRAM_ENABLED:
        # Modo Telegram — usa o JobQueue do python-telegram-bot para o scheduler
        from telegram.ext import Application, MessageHandler, filters
        from bot.handlers.telegram_handler import handle_message, handle_voice
        from bot.scheduler.reminder_jobs import (
            check_reminders, daily_summary, cleanup_old_messages, complete_past_meetings_job
        )

        if not settings.TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado no .env")

        application = (
            Application.builder()
            .token(settings.TELEGRAM_BOT_TOKEN)
            .build()
        )

        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

        jq = application.job_queue
        jq.run_repeating(check_reminders, interval=settings.REMINDER_CHECK_INTERVAL_SECONDS, first=10, name="check_reminders")
        jq.run_daily(daily_summary, time=time(hour=settings.DAILY_SUMMARY_HOUR, minute=settings.DAILY_SUMMARY_MINUTE, tzinfo=TZ_SP), name="daily_summary")
        jq.run_daily(complete_past_meetings_job, time=time(hour=0, minute=5, tzinfo=TZ_SP), name="complete_past_meetings")
        jq.run_daily(cleanup_old_messages, time=time(hour=3, minute=0, tzinfo=TZ_SP), name="cleanup_old_messages")

        async with application:
            await application.initialize()
            await application.start()
            await application.updater.start_polling(allowed_updates=["message", "voice", "audio"])
            logger.info("Telegram ativo.")
            await asyncio.Event().wait()

    else:
        # Modo WhatsApp — scheduler com APScheduler
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from bot.scheduler.whatsapp_jobs import (
            check_reminders_whatsapp, daily_summary_whatsapp,
            cleanup_old_messages_whatsapp, complete_past_meetings_whatsapp
        )

        scheduler = AsyncIOScheduler(timezone=TZ_SP)
        scheduler.add_job(check_reminders_whatsapp, "interval", seconds=settings.REMINDER_CHECK_INTERVAL_SECONDS, id="check_reminders")
        scheduler.add_job(daily_summary_whatsapp, "cron", hour=settings.DAILY_SUMMARY_HOUR, minute=settings.DAILY_SUMMARY_MINUTE, id="daily_summary")
        scheduler.add_job(complete_past_meetings_whatsapp, "cron", hour=0, minute=5, id="complete_past_meetings")
        scheduler.add_job(cleanup_old_messages_whatsapp, "cron", hour=3, minute=0, id="cleanup_old_messages")
        scheduler.start()

        logger.info("Telegram desativado. Rodando apenas WhatsApp.")
        await asyncio.Event().wait()


def main() -> None:
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
