import logging
from agents import RunContextWrapper, GuardrailFunctionOutput, input_guardrail, Agent

from bot.database import crud

logger = logging.getLogger(__name__)


@input_guardrail
async def subscription_guardrail(
    ctx: RunContextWrapper[dict], agent: Agent, input: object
) -> GuardrailFunctionOutput:
    """Verifica se o usuário tem assinatura ativa antes de executar o agente."""
    telegram_id = ctx.context.get("telegram_id")
    user = await crud.get_user_by_telegram_id(telegram_id)
    active, _ = crud.check_subscription_status(user)

    if not active:
        logger.warning(f"Acesso bloqueado (assinatura inativa) para telegram_id={telegram_id}")
        return GuardrailFunctionOutput(
            output_info={"reason": "subscription_inactive", "telegram_id": telegram_id},
            tripwire_triggered=True,
        )

    return GuardrailFunctionOutput(
        output_info={"reason": "authorized"},
        tripwire_triggered=False,
    )
