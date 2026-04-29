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

    # Plano e trial
    TRIAL_DURATION_DAYS: int = int(os.getenv("TRIAL_DURATION_DAYS", "7"))
    PLAN_PRICE: float = float(os.getenv("PLAN_PRICE", "49.90"))

    # Mercado Pago
    MERCADOPAGO_ACCESS_TOKEN: str = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "")
    MERCADOPAGO_WEBHOOK_SECRET: str = os.getenv("MERCADOPAGO_WEBHOOK_SECRET", "")

    # Google OAuth2
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI: str = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/oauth/callback")

    # Webhook server
    WEBHOOK_PUBLIC_URL: str = os.getenv("WEBHOOK_PUBLIC_URL", "http://localhost:8080")
    WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))

    # Internos
    OPENAI_MODEL: str = "gpt-4.1-mini"
    MAX_CONVERSATION_HISTORY: int = 20
    REMINDER_CHECK_INTERVAL_SECONDS: int = 60
    DAILY_SUMMARY_HOUR: int = 7
    DAILY_SUMMARY_MINUTE: int = 0
    SUBSCRIPTION_CHECK_HOUR: int = 6
    CONVERSATION_RETENTION_DAYS: int = 7


settings = Settings()
