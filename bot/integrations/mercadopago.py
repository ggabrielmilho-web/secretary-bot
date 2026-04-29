import asyncio
import base64
import logging
from io import BytesIO
from typing import Optional

from bot.config import settings

logger = logging.getLogger(__name__)

_sdk = None


def get_sdk():
    global _sdk
    if _sdk is None:
        import mercadopago
        _sdk = mercadopago.SDK(settings.MERCADOPAGO_ACCESS_TOKEN)
    return _sdk


async def create_pix_payment(
    telegram_id: int,
    amount: float,
    description: str,
    plan: str = "monthly",
    payer_email: Optional[str] = None,
    payer_name: Optional[str] = None,
) -> dict:
    """
    Cria pagamento PIX via Mercado Pago.
    Retorna: {payment_id, qr_code, qr_code_base64, status}
    """
    external_ref = f"{telegram_id}:{plan}"
    notification_url = f"{settings.WEBHOOK_PUBLIC_URL}/webhook/mercadopago"

    payment_data = {
        "transaction_amount": round(float(amount), 2),
        "description": description,
        "payment_method_id": "pix",
        "payer": {
            "email": payer_email or "cliente@secretariaia.com.br",
            "first_name": (payer_name or "Cliente").split()[0],
        },
        "external_reference": external_ref,
        "notification_url": notification_url,
    }

    def _sync():
        return get_sdk().payment().create(payment_data)

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, _sync)

    if response.get("status") != 201:
        body = response.get("response", {})
        cause = body.get("cause", [])
        error_detail = cause[0].get("description", str(body)) if cause else str(body)
        logger.error(f"Erro ao criar pagamento PIX: {error_detail}")
        raise Exception(f"Falha no pagamento: {error_detail}")

    data = response["response"]
    txn_data = data.get("point_of_interaction", {}).get("transaction_data", {})

    return {
        "payment_id": str(data["id"]),
        "qr_code": txn_data.get("qr_code", ""),
        "qr_code_base64": txn_data.get("qr_code_base64", ""),
        "status": data.get("status", "pending"),
    }


def decode_qr_image(qr_code_base64: str) -> BytesIO:
    """Decodifica imagem QR Code base64 para BytesIO pronto para envio."""
    img_bytes = base64.b64decode(qr_code_base64)
    img_io = BytesIO(img_bytes)
    img_io.name = "pix_qr.png"
    return img_io


async def get_payment_info(payment_id: str) -> dict:
    """Consulta o status atual de um pagamento no Mercado Pago."""
    def _sync():
        return get_sdk().payment().get(payment_id)

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, _sync)

    if response.get("status") != 200:
        raise Exception(f"Erro ao consultar pagamento {payment_id}: {response}")

    data = response["response"]
    return {
        "payment_id": str(data["id"]),
        "status": data.get("status"),
        "external_reference": data.get("external_reference", ""),
        "amount": data.get("transaction_amount", 0),
    }
