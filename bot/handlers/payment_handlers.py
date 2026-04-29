import logging

from telegram import Update
from telegram.ext import ContextTypes

from agents import Runner, InputGuardrailTripwireTriggered

from bot.agent.memory import ConversationMemory
from bot.agent.secretary_agent import build_secretary_agent
from bot.config import settings
from bot.database import crud
from bot.integrations import mercadopago as mp

logger = logging.getLogger(__name__)

_memory = ConversationMemory()

# Mapeamento de callback → texto enviado ao agente
_MENU_CALLBACKS = {
    "menu_agenda":    "Qual é minha agenda de hoje?",
    "menu_resumo":    "Resumo do meu dia de hoje",
    "menu_tarefas":   "Quais são minhas tarefas pendentes?",
    "menu_lembretes": "Quais são meus lembretes ativos?",
    "menu_ajuda":     "O que você pode fazer por mim?",
}


async def _run_agent_for_callback(query, user_id: int, db_user, message_text: str) -> None:
    """Executa o agente a partir de um callback de botão."""
    await query.message.chat.send_action("typing")

    history = await _memory.get_history(db_user.id)
    input_messages = [*history, {"role": "user", "content": message_text}]

    try:
        result = await Runner.run(
            build_secretary_agent(),
            input=input_messages,
            context={
                "user_id": db_user.id,
                "telegram_id": user_id,
                "db_user": db_user,
            },
        )
        response_text = result.final_output
    except InputGuardrailTripwireTriggered:
        await query.message.reply_text("⏰ Seu acesso expirou. Use /planos para assinar.")
        return
    except Exception as e:
        logger.error(f"Erro ao executar agente via callback para {user_id}: {e}")
        await query.message.reply_text("Ocorreu um erro. Tente novamente em instantes.")
        return

    await _memory.save_message(db_user.id, "user", message_text)
    await _memory.save_message(db_user.id, "assistant", response_text)

    try:
        await query.message.reply_text(response_text, parse_mode="Markdown")
    except Exception:
        await query.message.reply_text(response_text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processa todos os callbacks dos botões InlineKeyboard."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user = update.effective_user

    # Callbacks de menu rápido — acionam o agente
    if data in _MENU_CALLBACKS:
        db_user = await crud.get_user_by_telegram_id(user.id)
        if not db_user:
            await query.message.reply_text("Use /start para criar sua conta.")
            return

        active, error_msg = crud.check_subscription_status(db_user)
        if not active:
            await query.message.reply_text(error_msg)
            return

        await _run_agent_for_callback(query, user.id, db_user, _MENU_CALLBACKS[data])
        return

    # Callbacks de pagamento
    if data == "plan_monthly":
        await _handle_plan_monthly(query, user)
    elif data == "plan_coupon":
        await query.message.reply_text(
            "🎟️ Para ativar seu cupom envie:\n\n`/cupom SEUCÓDIGO`",
            parse_mode="Markdown",
        )


async def _handle_plan_monthly(query, user) -> None:
    """Gera PIX para plano mensal e envia QR Code ao usuário."""
    db_user = await crud.get_user_by_telegram_id(user.id)
    if not db_user:
        await query.message.reply_text("Use /start para criar sua conta primeiro.")
        return

    if not settings.MERCADOPAGO_ACCESS_TOKEN:
        await query.message.reply_text(
            "⚠️ O sistema de pagamento não está configurado. Entre em contato com o suporte."
        )
        return

    await query.message.reply_text("⏳ Gerando seu PIX, aguarde...")

    try:
        pix_data = await mp.create_pix_payment(
            telegram_id=user.id,
            amount=settings.PLAN_PRICE,
            description="Secretário IA — Plano Mensal",
            plan="monthly",
            payer_name=user.full_name,
        )
    except Exception as e:
        logger.error(f"Erro ao criar PIX para usuário {user.id}: {e}")
        await query.message.reply_text(
            "❌ Não consegui gerar o PIX agora. Tente novamente ou entre em contato com o suporte."
        )
        return

    try:
        await crud.create_payment(
            user_id=db_user.id,
            mercadopago_payment_id=pix_data["payment_id"],
            amount=settings.PLAN_PRICE,
            status="pending",
            plan="monthly",
        )
    except Exception as e:
        logger.warning(f"Não foi possível registrar pagamento pendente: {e}")

    qr_code = pix_data.get("qr_code", "")
    qr_base64 = pix_data.get("qr_code_base64", "")

    if qr_base64:
        try:
            img_io = mp.decode_qr_image(qr_base64)
            await query.message.reply_photo(
                photo=img_io,
                caption=(
                    f"💰 *PIX — R$ {settings.PLAN_PRICE:.2f}/mês*\n\n"
                    "Escaneie o QR Code ou use o código abaixo.\n"
                    "Após o pagamento, sua conta será ativada automaticamente!"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Falha ao enviar imagem QR Code: {e}")

    if qr_code:
        await query.message.reply_text(
            f"📋 *PIX Copia e Cola:*\n\n`{qr_code}`\n\n"
            "Cole esse código no app do seu banco para pagar.",
            parse_mode="Markdown",
        )
    elif not qr_base64:
        await query.message.reply_text(
            "❌ Não foi possível gerar o QR Code. Tente novamente ou entre em contato com o suporte."
        )

    logger.info(f"PIX gerado para usuário {user.id} — payment_id {pix_data['payment_id']}")
