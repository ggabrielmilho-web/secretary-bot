import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import settings
from bot.database import crud
from bot.integrations.google_calendar import google_calendar as _gcal
from bot.integrations.whatsapp import whatsapp_client
from bot.scheduler.reminder_jobs import (
    PRIORITY_EMOJI, _next_occurrence, _should_show_recurring_today
)

logger = logging.getLogger(__name__)

TZ_SP = ZoneInfo("America/Sao_Paulo")


async def check_reminders_whatsapp() -> None:
    """Verifica lembretes pendentes e envia via WhatsApp. Roda a cada 60s."""
    try:
        pending = await crud.list_pending_reminders()
    except Exception as e:
        logger.error(f"Erro ao buscar lembretes pendentes: {e}")
        return

    for reminder in pending:
        try:
            from bot.database.models import User
            from bot.database.connection import async_session
            from sqlalchemy import select

            async with async_session() as session:
                result = await session.execute(select(User).where(User.id == reminder.user_id))
                user = result.scalar_one_or_none()

            if not user or not user.whatsapp_number:
                continue

            await whatsapp_client.send_text(
                user.whatsapp_number,
                f"🔔 *Lembrete:* {reminder.message}"
            )
            await crud.mark_reminder_sent(reminder.id)
            logger.info(f"Lembrete {reminder.id} enviado via WhatsApp para {user.whatsapp_number}")

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


async def daily_summary_whatsapp() -> None:
    """Envia resumo diário via WhatsApp às 7:00."""
    now = datetime.now(TZ_SP)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=None)

    try:
        users = await crud.get_all_active_users()
    except Exception as e:
        logger.error(f"Erro ao buscar usuários para resumo diário: {e}")
        return

    for user in users:
        if not user.whatsapp_number:
            continue
        try:
            user_email = settings.WHATSAPP_EMAIL_MAP.get(user.whatsapp_number)

            events_data = []
            if user_email and _gcal and _gcal.available:
                events_data = await _gcal.list_events(user_email, start, end) or []

            if not events_data:
                meetings = await crud.list_meetings(user_id=user.id, date_from=start, date_to=end)
                events_data = [
                    {"title": m.title, "datetime_start": m.datetime_start.isoformat(), "location": m.location or "", "meet_link": ""}
                    for m in meetings
                ]

            tasks = await crud.list_tasks(user_id=user.id, status="pendente")
            now_dt = now.replace(tzinfo=None)

            # Filtra tarefas recorrentes: só inclui as que batem com o dia de hoje
            tasks = [
                t for t in tasks
                if not t.is_recurring or _should_show_recurring_today(t.recurrence_rule, now_dt)
            ]

            tasks_atrasadas = [t for t in tasks if t.due_date and t.due_date < now_dt]
            tasks_normais = [t for t in tasks if not (t.due_date and t.due_date < now_dt)]
            reminders = await crud.list_reminders_for_day(user_id=user.id, date=now_dt)

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
                    hora = f" — {t.recurring_time}" if t.is_recurring and t.recurring_time else ""
                    icone = "🔁" if t.is_recurring else emoji
                    msg += f"  {icone} [{t.priority.upper()}] {t.title}{hora}\n"
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

            await whatsapp_client.send_text(user.whatsapp_number, msg)
            logger.info(f"Resumo diário WhatsApp enviado para {user.whatsapp_number}")

        except Exception as e:
            logger.error(f"Erro ao enviar resumo diário WhatsApp para {user.whatsapp_number}: {e}")


async def cleanup_old_messages_whatsapp() -> None:
    """Remove mensagens antigas do histórico. Roda às 3:00."""
    try:
        removed = await crud.cleanup_old_messages(days=settings.CONVERSATION_RETENTION_DAYS)
        logger.info(f"Limpeza de histórico: {removed} mensagens removidas.")
    except Exception as e:
        logger.error(f"Erro na limpeza de histórico: {e}")


async def complete_past_meetings_whatsapp() -> None:
    """Marca reuniões passadas como concluídas. Roda às 0:05."""
    try:
        count = await crud.complete_past_meetings()
        if count:
            logger.info(f"Job noturno WhatsApp: {count} reunião(ões) concluída(s).")
    except Exception as e:
        logger.error(f"Erro ao concluir reuniões passadas: {e}")
