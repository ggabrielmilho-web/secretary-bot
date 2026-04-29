import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from aiohttp import web

from bot.config import settings
from bot.database import crud
from bot.integrations import mercadopago as mp
import bot.integrations.google_calendar as _gcal

logger = logging.getLogger(__name__)

_bot = None


def set_bot(bot) -> None:
    """Injeta a instância do bot Telegram para envio de mensagens."""
    global _bot
    _bot = bot


async def _send_telegram(telegram_id: int, text: str, parse_mode: str = "Markdown") -> None:
    """Envia mensagem Telegram via instância global do bot."""
    if not _bot:
        logger.warning("Bot não inicializado — não é possível enviar mensagem Telegram.")
        return
    try:
        await _bot.send_message(chat_id=telegram_id, text=text, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem Telegram para {telegram_id}: {e}")


# ---------------------------------------------------------------------------
# Mercado Pago Webhook
# ---------------------------------------------------------------------------

def _verify_mp_signature(request_body: bytes, signature_header: str) -> bool:
    """Valida assinatura HMAC do webhook do Mercado Pago."""
    if not settings.MERCADOPAGO_WEBHOOK_SECRET:
        return True  # sem secret configurado, aceita tudo

    expected = hmac.new(
        settings.MERCADOPAGO_WEBHOOK_SECRET.encode(),
        request_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")


async def handle_mercadopago_webhook(request: web.Request) -> web.Response:
    """
    Recebe notificações do Mercado Pago.
    Ativa o plano do usuário quando o pagamento é aprovado.
    """
    try:
        body = await request.read()
        signature = request.headers.get("x-signature", "")

        if not _verify_mp_signature(body, signature):
            logger.warning("Assinatura do webhook Mercado Pago inválida.")
            return web.Response(status=400, text="Invalid signature")

        payload = json.loads(body)
        logger.info(f"Webhook MP recebido: {payload.get('type')} / action={payload.get('action')}")

        # Ignora eventos que não são de pagamento
        if payload.get("type") != "payment":
            return web.Response(status=200, text="OK")

        mp_payment_id = str(payload.get("data", {}).get("id", ""))
        if not mp_payment_id:
            return web.Response(status=200, text="OK")

        # Consulta status real no MP (evita spoofing)
        try:
            payment_info = await mp.get_payment_info(mp_payment_id)
        except Exception as e:
            logger.error(f"Falha ao consultar pagamento {mp_payment_id}: {e}")
            return web.Response(status=200, text="OK")

        status = payment_info.get("status")
        logger.info(f"Pagamento {mp_payment_id} — status: {status}")

        if status != "approved":
            await crud.update_payment_status(mp_payment_id, status)
            return web.Response(status=200, text="OK")

        # Aprovado — extrai telegram_id e plano do external_reference
        external_ref = payment_info.get("external_reference", "")
        if ":" not in external_ref:
            logger.error(f"external_reference inválido: {external_ref}")
            return web.Response(status=200, text="OK")

        telegram_id_str, plan = external_ref.split(":", 1)
        try:
            telegram_id = int(telegram_id_str)
        except ValueError:
            logger.error(f"telegram_id inválido em external_reference: {telegram_id_str}")
            return web.Response(status=200, text="OK")

        db_user = await crud.get_user_by_telegram_id(telegram_id)
        if not db_user:
            logger.error(f"Usuário {telegram_id} não encontrado após pagamento aprovado.")
            return web.Response(status=200, text="OK")

        # Ativa plano por 31 dias (mensal)
        subscription_ends_at: Optional[datetime] = None
        if plan == "monthly":
            subscription_ends_at = datetime.now() + timedelta(days=31)
        elif plan == "lifetime":
            subscription_ends_at = None

        await crud.activate_plan(db_user.id, plan, subscription_ends_at)
        await crud.update_payment_status(mp_payment_id, "approved")

        # Atualiza last_payment_id na conta
        from bot.database.connection import async_session
        from bot.database.models import User
        from sqlalchemy import select
        async with async_session() as session:
            result = await session.execute(select(User).where(User.id == db_user.id))
            user_obj = result.scalar_one_or_none()
            if user_obj:
                user_obj.last_payment_id = mp_payment_id

        plan_label = "Vitalício" if plan == "lifetime" else "Mensal"
        msg = (
            f"✅ *Pagamento aprovado!*\n\n"
            f"Seu plano *{plan_label}* foi ativado com sucesso.\n"
        )
        if subscription_ends_at:
            msg += f"Válido até: {subscription_ends_at.strftime('%d/%m/%Y')}\n"
        msg += "\nAproveite seu Secretário IA! 🚀"

        await _send_telegram(telegram_id, msg)
        logger.info(f"Plano {plan} ativado para usuário {telegram_id} — pagamento {mp_payment_id}")

    except Exception as e:
        logger.error(f"Erro inesperado no webhook Mercado Pago: {e}", exc_info=True)

    return web.Response(status=200, text="OK")


# ---------------------------------------------------------------------------
# Google OAuth2 Callback
# ---------------------------------------------------------------------------

async def handle_oauth_callback(request: web.Request) -> web.Response:
    """
    Recebe callback do Google OAuth2 após autorização do usuário.
    Troca o código por tokens e salva no banco.
    """
    try:
        code = request.rel_url.query.get("code")
        state = request.rel_url.query.get("state")  # state = telegram_id
        error = request.rel_url.query.get("error")

        if error:
            logger.warning(f"Usuário negou acesso Google OAuth: {error}")
            return web.Response(
                content_type="text/html",
                text="<html><body><h2>Autorização cancelada.</h2><p>Você pode fechar esta página.</p></body></html>",
            )

        if not code or not state:
            return web.Response(status=400, text="Parâmetros inválidos.")

        try:
            telegram_id = int(state)
        except ValueError:
            return web.Response(status=400, text="State inválido.")

        tokens = await _gcal.exchange_code(code, state)

        db_user = await crud.get_user_by_telegram_id(telegram_id)
        if not db_user:
            logger.error(f"Usuário {telegram_id} não encontrado após OAuth callback.")
            return web.Response(status=400, text="Usuário não encontrado.")

        await crud.update_google_tokens(
            user_id=db_user.id,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            token_expiry=tokens.get("token_expiry"),
        )

        await _send_telegram(
            telegram_id,
            "✅ *Google Calendar conectado com sucesso!*\n\n"
            "Sua agenda agora está sincronizada. Ao criar reuniões, elas aparecerão automaticamente no seu Google Calendar.\n\n"
            "Você já pode fechar a aba do navegador.",
        )
        logger.info(f"Google Calendar conectado para usuário {telegram_id}")

        return web.Response(
            content_type="text/html",
            text=(
                "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
                "<h2>✅ Google Calendar conectado!</h2>"
                "<p>Você já pode fechar esta aba e voltar ao Telegram.</p>"
                "</body></html>"
            ),
        )

    except Exception as e:
        logger.error(f"Erro no callback OAuth2: {e}", exc_info=True)
        return web.Response(
            content_type="text/html",
            text="<html><body><h2>Erro ao conectar agenda.</h2><p>Tente novamente mais tarde.</p></body></html>",
        )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    """Cria a aplicação aiohttp com as rotas registradas."""
    app = web.Application()
    app.router.add_post("/webhook/mercadopagosecretaria", handle_mercadopago_webhook)
    app.router.add_get("/oauth/callback", handle_oauth_callback)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    return app
