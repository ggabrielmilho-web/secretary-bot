import asyncio
import logging
import os
import signal
from datetime import time
from zoneinfo import ZoneInfo

from aiohttp import web
from dotenv import load_dotenv
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

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
    from bot.handlers.telegram_handler import handle_message, handle_voice
    from bot.handlers.command_handlers import (
        handle_start, handle_planos, handle_renovar,
        handle_cupom, handle_ajuda,
    )
    from bot.handlers.payment_handlers import handle_callback
    from bot.scheduler.reminder_jobs import (
        check_reminders, daily_summary, check_subscriptions,
        cleanup_old_messages, complete_past_meetings_job,
    )
    from bot.webhook_server import create_app, set_bot

    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado no .env")
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurado no .env")

    os.environ.setdefault("OPENAI_API_KEY", settings.OPENAI_API_KEY)

    # Inicializa banco
    await init_db()
    logger.info("Banco de dados inicializado.")

    # Inicia servidor aiohttp (webhook Mercado Pago + OAuth callback)
    aiohttp_app = create_app()
    runner = web.AppRunner(aiohttp_app)
    await runner.setup()
    site = web.TCPSite(runner, settings.WEBHOOK_HOST, settings.WEBHOOK_PORT)
    await site.start()
    logger.info(f"Webhook server iniciado em {settings.WEBHOOK_HOST}:{settings.WEBHOOK_PORT}")

    # Constrói Application do Telegram
    application = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Injeta bot no webhook server para envio de mensagens
    set_bot(application.bot)

    # Handlers de comandos
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("planos", handle_planos))
    application.add_handler(CommandHandler("renovar", handle_renovar))
    application.add_handler(CommandHandler("cupom", handle_cupom))
    # handle_conectar_agenda desativado até verificação Google OAuth
    application.add_handler(CommandHandler("ajuda", handle_ajuda))

    # Handler de callbacks de botões
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Handlers de mensagem e áudio
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

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
        time=time(hour=settings.DAILY_SUMMARY_HOUR, minute=settings.DAILY_SUMMARY_MINUTE, tzinfo=TZ_SP),
        name="daily_summary",
    )

    job_queue.run_daily(
        check_subscriptions,
        time=time(hour=settings.SUBSCRIPTION_CHECK_HOUR, minute=0, tzinfo=TZ_SP),
        name="check_subscriptions",
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

    # Inicia o bot
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            allowed_updates=["message", "callback_query"],
        )

        logger.info("Bot iniciado. Aguardando mensagens...")

        # Aguarda sinal de encerramento (SIGINT/SIGTERM no Linux)
        stop_event = asyncio.Event()
        loop = asyncio.get_event_loop()

        def _shutdown():
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except NotImplementedError:
                # Windows não suporta add_signal_handler — encerra via KeyboardInterrupt
                pass

        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass

        await application.updater.stop()
        await application.stop()

    await runner.cleanup()
    logger.info("Bot encerrado.")


def main() -> None:
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
