import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram.ext import ContextTypes

from bot.config import settings
from bot.database import crud
import bot.integrations.google_calendar as _gcal_module

logger = logging.getLogger(__name__)

TZ_SP = ZoneInfo("America/Sao_Paulo")

PRIORITY_EMOJI = {"urgente": "🔴", "alta": "🟠", "media": "🟡", "baixa": "🟢"}


def _next_occurrence(remind_at: datetime, recurrence_rule: str) -> datetime | None:
    """Calcula próxima ocorrência de um lembrete recorrente."""
    rule = (recurrence_rule or "").lower()

    if rule == "daily":
        return remind_at + timedelta(days=1)

    if rule == "weekdays":
        next_dt = remind_at + timedelta(days=1)
        while next_dt.weekday() >= 5:  # sábado=5, domingo=6
            next_dt += timedelta(days=1)
        return next_dt

    if rule.startswith("weekly:"):
        # Ex: weekly:mon,wed,fri
        day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        days_str = rule.replace("weekly:", "").split(",")
        target_days = sorted({day_map[d.strip()] for d in days_str if d.strip() in day_map})
        if not target_days:
            return None
        current_weekday = remind_at.weekday()
        for day in target_days:
            if day > current_weekday:
                return remind_at + timedelta(days=(day - current_weekday))
        # Volta para o primeiro da semana seguinte
        days_until = (7 - current_weekday) + target_days[0]
        return remind_at + timedelta(days=days_until)

    if rule.startswith("monthly:"):
        # Ex: monthly:15
        try:
            target_day = int(rule.replace("monthly:", ""))
            next_month = remind_at.replace(day=1) + timedelta(days=32)
            return next_month.replace(day=target_day, hour=remind_at.hour, minute=remind_at.minute)
        except (ValueError, OverflowError):
            return None

    return None


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verifica lembretes pendentes e envia notificações via Telegram. Roda a cada 60s."""
    try:
        pending = await crud.list_pending_reminders()
    except Exception as e:
        logger.error(f"Erro ao buscar lembretes pendentes: {e}")
        return

    for reminder in pending:
        try:
            # Busca telegram_id do usuário
            from bot.database.models import User
            from bot.database.connection import async_session
            from sqlalchemy import select

            async with async_session() as session:
                result = await session.execute(select(User).where(User.id == reminder.user_id))
                user = result.scalar_one_or_none()

            if not user:
                continue

            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=f"🔔 *Lembrete:* {reminder.message}",
                parse_mode="Markdown",
            )
            await crud.mark_reminder_sent(reminder.id)
            logger.info(f"Lembrete {reminder.id} enviado para usuário {user.telegram_id}")

            # Cria próxima ocorrência se recorrente
            if reminder.is_recurring and reminder.recurrence_rule:
                next_dt = _next_occurrence(reminder.remind_at, reminder.recurrence_rule)
                if next_dt:
                    await crud.create_reminder(
                        user_id=reminder.user_id,
                        message=reminder.message,
                        remind_at=next_dt,
                        is_recurring=True,
                        recurrence_rule=reminder.recurrence_rule,
                        task_id=reminder.task_id,
                        meeting_id=reminder.meeting_id,
                        reminder_type=reminder.reminder_type,
                    )

        except Exception as e:
            logger.error(f"Erro ao processar lembrete {reminder.id}: {e}")


async def daily_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia resumo diário a cada usuário ativo às 7:00. Inclui dados do Google Calendar."""
    now = datetime.now(TZ_SP)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=None)

    try:
        users = await crud.get_all_active_users()
    except Exception as e:
        logger.error(f"Erro ao buscar usuários para resumo diário: {e}")
        return

    for user in users:
        try:
            user_email = settings.USER_EMAIL_MAP.get(user.telegram_id)

            # Reuniões — Google Calendar como fonte principal
            events_data = []
            if user_email and _gcal_module.google_calendar and _gcal_module.google_calendar.available:
                events_data = await _gcal_module.google_calendar.list_events(user_email, start, end) or []

            if not events_data:
                meetings = await crud.list_meetings(user_id=user.id, date_from=start, date_to=end)
                events_data = [
                    {
                        "title": m.title,
                        "datetime_start": m.datetime_start.isoformat(),
                        "location": m.location or "",
                        "meet_link": "",
                    }
                    for m in meetings
                ]

            # Tarefas pendentes — separar atrasadas das normais
            tasks = await crud.list_tasks(user_id=user.id, status="pendente")
            now_dt = now.replace(tzinfo=None)
            tasks_atrasadas = [t for t in tasks if t.due_date and t.due_date < now_dt]
            tasks_normais = [t for t in tasks if not (t.due_date and t.due_date < now_dt)]

            # Lembretes do dia
            reminders = await crud.list_reminders_for_day(user_id=user.id, date=now.replace(tzinfo=None))

            # Monta mensagem
            msg = f"☀️ Bom dia! Aqui está seu resumo de hoje ({now.strftime('%d/%m/%Y')}):\n\n"

            if tasks_atrasadas:
                msg += f"⚠️ *Tarefas atrasadas ({len(tasks_atrasadas)}):*\n"
                for t in tasks_atrasadas:
                    emoji = PRIORITY_EMOJI.get(t.priority, "⚪")
                    dias = max((now_dt - t.due_date).days, 0)
                    msg += f"  {emoji} [{t.priority.upper()}] {t.title} — ⏰ {dias} dia{'s' if dias != 1 else ''} de atraso\n"
                msg += "\n"

            if tasks_normais:
                msg += f"📋 *Tarefas pendentes ({len(tasks_normais)}):*\n"
                for t in tasks_normais:
                    emoji = PRIORITY_EMOJI.get(t.priority, "⚪")
                    msg += f"  {emoji} [{t.priority.upper()}] {t.title}\n"
                msg += "\n"
            elif not tasks_atrasadas:
                msg += "📋 Nenhuma tarefa pendente ✅\n\n"

            if events_data:
                msg += f"📅 *Reuniões de hoje ({len(events_data)}):*\n"
                for ev in events_data:
                    try:
                        hora = datetime.fromisoformat(ev["datetime_start"]).strftime("%H:%M")
                    except Exception:
                        hora = "??"
                    local = f" ({ev['location']})" if ev.get("location") else ""
                    meet = " 🔗 Meet" if ev.get("meet_link") else ""
                    msg += f"  🕐 {hora} - {ev['title']}{local}{meet}\n"
                msg += "\n"
            else:
                msg += "📅 Nenhuma reunião hoje 🎉\n\n"

            if reminders:
                msg += "⏰ *Lembretes:*\n"
                for r in reminders:
                    hora = r.remind_at.strftime("%H:%M")
                    msg += f"  🔔 {hora} - {r.message}\n"

            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=msg,
                parse_mode="Markdown",
            )
            logger.info(f"Resumo diário enviado para usuário {user.telegram_id}")

        except Exception as e:
            logger.error(f"Erro ao enviar resumo diário para usuário {user.telegram_id}: {e}")


async def cleanup_old_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove mensagens antigas do histórico de conversa. Roda às 3:00."""
    try:
        removed = await crud.cleanup_old_messages(days=settings.CONVERSATION_RETENTION_DAYS)
        logger.info(f"Limpeza de histórico: {removed} mensagens removidas.")
    except Exception as e:
        logger.error(f"Erro na limpeza de histórico: {e}")


async def complete_past_meetings_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Marca como concluídas reuniões de dias anteriores. Roda às 0:05."""
    try:
        count = await crud.complete_past_meetings()
        if count:
            logger.info(f"Job noturno: {count} reunião(ões) marcada(s) como concluída(s).")
    except Exception as e:
        logger.error(f"Erro ao concluir reuniões passadas: {e}")
