import io
import logging
from typing import Optional

import aiohttp.web
import openai
from agents import Runner, InputGuardrailTripwireTriggered

from bot.agent.memory import ConversationMemory
from bot.agent.secretary_agent import build_secretary_agent
from bot.config import settings
from bot.database import crud
from bot.integrations.whatsapp import whatsapp_client

logger = logging.getLogger(__name__)

_memory = ConversationMemory()


def get_agent():
    return build_secretary_agent()


async def _transcribe_whatsapp_audio(message_id: str) -> Optional[str]:
    """Baixa áudio via UazAPI e transcreve com Whisper."""
    audio_bytes = await whatsapp_client.download_media(message_id)
    if not audio_bytes:
        return None
    try:
        audio_io = io.BytesIO(audio_bytes)
        audio_io.name = "audio.ogg"
        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_io,
            language="pt",
        )
        text = transcript.text.strip()
        logger.info(f"Transcrição WhatsApp concluída ({len(text)} chars)")
        return text if text else None
    except Exception as e:
        logger.error(f"Erro ao transcrever áudio WhatsApp: {e}")
        return None


async def _run_agent_whatsapp(phone: str, name: str, message_text: str) -> None:
    """Executa o agente e envia a resposta de volta via WhatsApp."""
    user_email: Optional[str] = settings.WHATSAPP_EMAIL_MAP.get(phone)

    try:
        db_user = await crud.get_or_create_user_by_whatsapp(
            whatsapp_number=phone, name=name
        )
    except Exception as e:
        logger.error(f"Erro ao buscar/criar usuário WhatsApp {phone}: {e}")
        await whatsapp_client.send_text(phone, "Erro interno. Tente novamente.")
        return

    history = await _memory.get_history_by_user_id(db_user.id)
    input_messages = [*history, {"role": "user", "content": message_text}]

    try:
        result = await Runner.run(
            get_agent(),
            input=input_messages,
            context={
                "user_id": db_user.id,
                "telegram_id": None,
                "user_email": user_email,
            },
        )
        response_text = result.final_output

    except InputGuardrailTripwireTriggered:
        await whatsapp_client.send_text(phone, "Desculpe, você não tem autorização para usar este bot.")
        return
    except Exception as e:
        logger.error(f"Erro ao executar agente WhatsApp para {phone}: {e}")
        await whatsapp_client.send_text(phone, "Ocorreu um erro. Tente novamente em instantes.")
        return

    await crud.save_message(db_user.id, "user", message_text)
    await crud.save_message(db_user.id, "assistant", response_text)

    # Remove formatação Markdown que o WhatsApp não suporta
    clean_text = response_text.replace("**", "*").replace("```", "")
    await whatsapp_client.send_text(phone, clean_text)


async def webhook_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Recebe webhook da UazAPI e processa a mensagem."""
    try:
        payload = await request.json()
    except Exception:
        return aiohttp.web.Response(status=400, text="Invalid JSON")

    logger.info(f"[WEBHOOK] Payload recebido: {payload}")

    message = payload.get("message", {})

    # Ignora mensagens enviadas pelo próprio bot
    if message.get("fromMe", False):
        return aiohttp.web.Response(text="ok")

    # Extrai dados do remetente
    sender = message.get("sender_pn") or message.get("sender", "")
    phone = sender.replace("@s.whatsapp.net", "").replace("@lid", "").strip()
    name = message.get("pushname") or message.get("notifyName") or phone
    message_type = message.get("messageType", "").lower()
    message_id = message.get("messageid") or message.get("id", "")

    if not phone:
        return aiohttp.web.Response(text="ok")

    # Verifica autorização
    if phone not in settings.AUTHORIZED_WHATSAPP:
        logger.warning(f"Acesso negado para WhatsApp {phone}")
        await whatsapp_client.send_text(phone, "Desculpe, você não tem autorização para usar este bot.")
        return aiohttp.web.Response(text="ok")

    # Processa mensagem de texto
    if message_type in ("text", "extendedtextmessage"):
        text = message.get("text", "").strip()
        if text:
            import asyncio
            asyncio.create_task(_run_agent_whatsapp(phone, name, text))

    # Processa áudio/voz
    elif message_type in ("audio", "audiomessage", "ptt"):
        if message_id:
            async def process_audio():
                text = await _transcribe_whatsapp_audio(message_id)
                if text:
                    await _run_agent_whatsapp(phone, name, text)
                else:
                    await whatsapp_client.send_text(phone, "Não consegui transcrever o áudio. Tente novamente ou envie como texto.")
            import asyncio
            asyncio.create_task(process_audio())

    return aiohttp.web.Response(text="ok")


def create_whatsapp_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    app.router.add_post("/webhook", webhook_handler)
    return app
