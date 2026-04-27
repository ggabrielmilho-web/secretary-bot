import logging
from agents import RunContextWrapper, GuardrailFunctionOutput, input_guardrail, Agent

from bot.config import settings

logger = logging.getLogger(__name__)


@input_guardrail
async def authorization_guardrail(
    ctx: RunContextWrapper[dict], agent: Agent, input: object
) -> GuardrailFunctionOutput:
    """Verifica se o usuário tem autorização para usar o bot."""
    telegram_id = ctx.context.get("telegram_id")

    if telegram_id not in settings.AUTHORIZED_USERS:
        logger.warning(f"Acesso negado para telegram_id={telegram_id}")
        return GuardrailFunctionOutput(
            output_info={"reason": "unauthorized", "telegram_id": telegram_id},
            tripwire_triggered=True,
        )

    return GuardrailFunctionOutput(
        output_info={"reason": "authorized"},
        tripwire_triggered=False,
    )
