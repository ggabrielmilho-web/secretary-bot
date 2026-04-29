import io
import logging
from typing import Optional

import openai
from telegram import Update
from telegram.ext import ContextTypes
from agents import Runner, InputGuardrailTripwireTriggered

from bot.agent.memory import ConversationMemory
from bot.agent.secretary_agent import build_secretary_agent
from bot.config import settings
from bot.database import crud

logger = logging.getLogger(__name__)

_memory = ConversationMemory()


def get_agent():
    return build_secretary_agent()


async def check_subscription(update: Update) -> tuple[bool, Optional[object]]:
    """
    Verifica assinatura do usuário. Retorna (ativo, db_user).
    Se inativo, envia mensagem de erro e retorna (False, db_user).
    """
    user = update.effective_user
    db_user = await crud.get_user_by_telegram_id(user.id)

    if not db_user:
        await update.message.reply_text(
            "Olá! Use /start para criar sua conta e começar seu período de teste gratuito."
        )
        return False, None

    active, error_msg = crud.check_subscription_status(db_user)
    if not active:
        await update.message.reply_text(error_msg)
        return False, db_user

    return True, db_user


async def _run_agent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_text: str,
    db_user,
) -> None:
    """Executa o fluxo completo do agente com o texto fornecido."""
    user = update.effective_user

    await update.message.chat.send_action("typing")

    history = await _memory.get_history(db_user.id)
    input_messages = [*history, {"role": "user", "content": message_text}]

    logger.info(f"Executando agente para {user.id} — histórico: {len(history)} msgs")
    try:
        result = await Runner.run(
            get_agent(),
            input=input_messages,
            context={
                "user_id": db_user.id,
                "telegram_id": user.id,
                "db_user": db_user,
            },
        )
        logger.info(f"Agente respondeu para {user.id}")
        response_text = result.final_output

    except InputGuardrailTripwireTriggered:
        logger.warning(f"Guardrail acionado para telegram_id={user.id}")
        await update.message.reply_text(
            "⏰ Seu acesso expirou. Use /planos para assinar e continuar."
        )
        return

    except Exception as e:
        logger.error(f"Erro ao executar agente para usuário {user.id}: {e}")
        await update.message.reply_text("Ocorreu um erro ao processar sua mensagem. Tente novamente em instantes.")
        return

    await _memory.save_message(db_user.id, "user", message_text)
    await _memory.save_message(db_user.id, "assistant", response_text)

    try:
        await update.message.reply_text(response_text, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(response_text)


async def _transcribe_audio(update: Update) -> Optional[str]:
    """Baixa o áudio/voz do Telegram e transcreve via Whisper. Retorna o texto ou None se falhar."""
    msg = update.message

    if msg.voice:
        tg_file = await msg.voice.get_file()
        filename = "voice.ogg"
    elif msg.audio:
        tg_file = await msg.audio.get_file()
        mime = msg.audio.mime_type or ""
        ext = mime.split("/")[-1] if "/" in mime else "mp3"
        filename = f"audio.{ext}"
    else:
        return None

    try:
        audio_bytes = await tg_file.download_as_bytearray()
        audio_io = io.BytesIO(bytes(audio_bytes))
        audio_io.name = filename

        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_io,
            language="pt",
        )
        text = transcript.text.strip()
        logger.info(f"Transcrição concluída ({len(text)} chars)")
        return text if text else None

    except Exception as e:
        logger.error(f"Erro ao transcrever áudio: {e}")
        return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler de mensagens de texto."""
    user = update.effective_user
    message_text = update.message.text

    if not user or not message_text:
        return

    logger.info(f"Mensagem recebida de {user.id} ({user.full_name}): {message_text[:60]}")

    active, db_user = await check_subscription(update)
    if not active:
        return

    await _run_agent(update, context, message_text, db_user)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler de mensagens de voz e áudio — transcreve e passa pro agente."""
    user = update.effective_user

    if not user:
        return

    active, db_user = await check_subscription(update)
    if not active:
        return

    await update.message.chat.send_action("typing")

    text = await _transcribe_audio(update)
    if not text:
        await update.message.reply_text("Não consegui transcrever o áudio. Tente novamente ou envie como texto.")
        return

    await _run_agent(update, context, text, db_user)
