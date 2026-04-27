import logging
from bot.database import crud

logger = logging.getLogger(__name__)


class ConversationMemory:
    """Gerencia histórico de conversa no banco de dados."""

    async def get_history(self, telegram_id: int, limit: int = 20) -> list[dict]:
        """
        Retorna as últimas N mensagens do usuário formatadas para o Agents SDK.

        Garante que a sequência de roles alterna corretamente (user/assistant).
        Mensagens inconsistentes são descartadas automaticamente pelo crud.get_history.
        """
        try:
            user = await crud.get_or_create_user(telegram_id=telegram_id, name="")
            return await crud.get_history(user_id=user.id, limit=limit)
        except Exception as e:
            logger.error(f"Erro ao carregar histórico do usuário {telegram_id}: {e}")
            return []

    async def save_message(self, telegram_id: int, role: str, content: str) -> None:
        """Salva mensagem (role='user' ou role='assistant') no histórico."""
        try:
            user = await crud.get_or_create_user(telegram_id=telegram_id, name="")
            await crud.save_message(user_id=user.id, role=role, content=content)
        except Exception as e:
            logger.error(f"Erro ao salvar mensagem no histórico do usuário {telegram_id}: {e}")
