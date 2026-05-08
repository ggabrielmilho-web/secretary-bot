import logging
from agents import RunContextWrapper, GuardrailFunctionOutput, input_guardrail, Agent

from bot.config import settings

logger = logging.getLogger(__name__)


@input_guardrail
async def authorization_guardrail(
    ctx: RunContextWrapper[dict], agent: Agent, input: object
) -> GuardrailFunctionOutput:
    """Verifica se o usuário tem autorização para usar o bot (Telegram ou WhatsApp)."""
    telegram_id = ctx.context.get("telegram_id")
    whatsapp_number = ctx.context.get("whatsapp_number")

    telegram_ok = telegram_id is not None and telegram_id in settings.AUTHORIZED_USERS
    whatsapp_ok = whatsapp_number is not None and whatsapp_number in settings.AUTHORIZED_WHATSAPP

    if not telegram_ok and not whatsapp_ok:
        logger.warning(f"Acesso negado — telegram_id={telegram_id} whatsapp={whatsapp_number}")
        return GuardrailFunctionOutput(
            output_info={"reason": "unauthorized"},
            tripwire_triggered=True,
        )

    return GuardrailFunctionOutput(
        output_info={"reason": "authorized"},
        tripwire_triggered=False,
    )
