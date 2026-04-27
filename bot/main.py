import logging
import os
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram.ext import Application, MessageHandler, filters

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

TZ_SP = ZoneInfo("America/Sao_Paulo")


async def post_init(application: Application) -> None:
    """Inicializa banco de dados e Google Calendar após o Application ser criado."""
    from bot.database.connection import init_db
    from bot.integrations.google_calendar import init_google_calendar
    from bot.config import settings

    await init_db()

    init_google_calendar(settings.GOOGLE_SERVICE_ACCOUNT_FILE)

    logger.info("Inicialização completa.")


def main() -> None:
    from bot.config import settings
    from bot.handlers.telegram_handler import handle_message, handle_voice
    from bot.scheduler.reminder_jobs import check_reminders, daily_summary, cleanup_old_messages, complete_past_meetings_job

    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado no .env")

    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurado no .env")

    # Configura API key da OpenAI via variável de ambiente (forma recomendada pelo SDK)
    os.environ.setdefault("OPENAI_API_KEY", settings.OPENAI_API_KEY)

    application = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Handler de mensagens de texto
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Handler de voz e áudio
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, handle_voice)
    )

    # Jobs do scheduler
    job_queue = application.job_queue

    job_queue.run_repeating(
        check_reminders,
        interval=settings.REMINDER_CHECK_INTERVAL_SECONDS,
        first=10,
        name="check_reminders",
    )

    job_queue.run_daily(
        daily_summary,
        time=time(
            hour=settings.DAILY_SUMMARY_HOUR,
            minute=settings.DAILY_SUMMARY_MINUTE,
            tzinfo=TZ_SP,
        ),
        name="daily_summary",
    )

    job_queue.run_daily(
        complete_past_meetings_job,
        time=time(hour=0, minute=5, tzinfo=TZ_SP),
        name="complete_past_meetings",
    )

    job_queue.run_daily(
        cleanup_old_messages,
        time=time(hour=3, minute=0, tzinfo=TZ_SP),
        name="cleanup_old_messages",
    )

    logger.info("Bot iniciado. Aguardando mensagens...")
    application.run_polling(allowed_updates=["message", "voice", "audio"])


if __name__ == "__main__":
    main()
