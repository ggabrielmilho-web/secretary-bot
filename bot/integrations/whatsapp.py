import logging
from typing import Optional

import aiohttp

from bot.config import settings

logger = logging.getLogger(__name__)


class WhatsAppClient:
    """Cliente UazAPI para envio e recebimento de mensagens WhatsApp."""

    def __init__(self) -> None:
        self._base_url = settings.UAZAPI_BASE_URL.rstrip("/")
        self._headers = {
            "token": settings.UAZAPI_TOKEN,
            "Content-Type": "application/json",
        }

    @property
    def available(self) -> bool:
        return bool(self._base_url and settings.UAZAPI_TOKEN)

    async def send_text(self, number: str, text: str) -> bool:
        """Envia mensagem de texto para um número WhatsApp."""
        if not self.available:
            logger.warning("WhatsApp client não configurado.")
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/send/text",
                    headers=self._headers,
                    json={"number": number, "text": text},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        return True
                    body = await resp.text()
                    logger.error(f"Erro ao enviar texto WhatsApp ({resp.status}): {body}")
                    return False
        except Exception as e:
            logger.error(f"Exceção ao enviar texto WhatsApp para {number}: {e}")
            return False

    async def download_media(self, message_id: str) -> Optional[bytes]:
        """Baixa mídia (áudio, imagem) de uma mensagem via ID. Retorna bytes ou None."""
        if not self.available:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/message/download",
                    headers=self._headers,
                    json={"id": message_id, "return_base64": True, "return_link": False},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"Erro ao baixar mídia {message_id} ({resp.status})")
                        return None
                    data = await resp.json()
                    b64 = data.get("base64Data", "")
                    if not b64:
                        logger.error(f"base64Data vazio para mensagem {message_id}")
                        return None
                    import base64
                    return base64.b64decode(b64)
        except Exception as e:
            logger.error(f"Exceção ao baixar mídia {message_id}: {e}")
            return None


whatsapp_client = WhatsAppClient()
