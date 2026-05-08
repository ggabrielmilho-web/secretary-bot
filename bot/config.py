import os
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class Settings:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    TIMEZONE: str = os.getenv("TIMEZONE", "America/Sao_Paulo")

    AUTHORIZED_USERS: list[int] = [
        int(uid.strip())
        for uid in os.getenv("AUTHORIZED_USERS", "").split(",")
        if uid.strip().isdigit()
    ]

    USER_EMAIL_MAP: dict[int, str] = {
        int(pair.split(":", 1)[0].strip()): pair.split(":", 1)[1].strip()
        for pair in os.getenv("USER_EMAIL_MAP", "").split(",")
        if ":" in pair and pair.split(":", 1)[0].strip().isdigit()
    }

    GOOGLE_SERVICE_ACCOUNT_FILE: str = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "credentials/google-service-account.json",
    )

    # Telegram (pode ser desativado)
    TELEGRAM_ENABLED: bool = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"

    # WhatsApp via UazAPI
    UAZAPI_TOKEN: str = os.getenv("UAZAPI_TOKEN", "")
    UAZAPI_BASE_URL: str = os.getenv("UAZAPI_BASE_URL", "")
    WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))
    AUTHORIZED_WHATSAPP: list[str] = [
        n.strip() for n in os.getenv("AUTHORIZED_WHATSAPP", "").split(",") if n.strip()
    ]
    WHATSAPP_EMAIL_MAP: dict[str, str] = {
        pair.split(":", 1)[0].strip(): pair.split(":", 1)[1].strip()
        for pair in os.getenv("WHATSAPP_EMAIL_MAP", "").split(",")
        if ":" in pair
    }

    OPENAI_MODEL: str = "gpt-4.1-mini"
    MAX_CONVERSATION_HISTORY: int = 20
    REMINDER_CHECK_INTERVAL_SECONDS: int = 60
    DAILY_SUMMARY_HOUR: int = 7
    DAILY_SUMMARY_MINUTE: int = 0
    CONVERSATION_RETENTION_DAYS: int = 7


settings = Settings()
logger.info(f"USER_EMAIL_MAP carregado: {settings.USER_EMAIL_MAP}")
