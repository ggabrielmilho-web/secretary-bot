import json
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from agents import function_tool, RunContextWrapper
from pydantic import BaseModel, Field

from bot.database import crud
from bot.database.crud import DuplicateReminderError
import bot.integrations.google_calendar as _gcal_module

logger = logging.getLogger(__name__)

TZ_SP = ZoneInfo("America/Sao_Paulo")

PRIORITY_EMOJI = {"urgente": "🔴", "alta": "🟠", "media": "🟡", "baixa": "🟢"}


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------

class CriarLembreteInput(BaseModel):
    message: str = Field(description="Mensagem do lembrete, o que deve ser lembrado")
    remind_at: str = Field(description="Data e hora ISO 8601 (YYYY-MM-DDTHH:MM:SS) para disparar o lembrete")
    is_recurring: Optional[bool] = Field(default=False, description="True para lembrete recorrente")
    recurrence_rule: Optional[str] = Field(
        default=None,
        description="Regra de recorrência: 'daily', 'weekdays', 'weekly:mon,wed,fri', 'monthly:15'",
    )
    task_id: Optional[int] = Field(default=None, description="ID da tarefa vinculada (opcional)")
    meeting_id: Optional[int] = Field(default=None, description="ID da reunião vinculada (opcional)")


class ListarLembretesInput(BaseModel):
    active_only: Optional[bool] = Field(default=True, description="True para listar apenas lembretes ativos/não enviados")


class DesativarLembreteInput(BaseModel):
    reminder_id: Optional[int] = Field(default=None, description="ID numérico do lembrete")
    message_search: Optional[str] = Field(default=None, description="Trecho da mensagem do lembrete para busca")


class ResumoDoDiaInput(BaseModel):
    date: Optional[str] = Field(default=None, description="Data ISO 8601 (YYYY-MM-DD). Padrão: hoje")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@function_tool
async def criar_lembrete(ctx: RunContextWrapper[dict], input: CriarLembreteInput) -> str:
    """
    Cria alerta para HORÁRIO ESPECÍFICO.
    Use quando o diretor disser 'me lembra às 9h', 'me avisa antes da reunião', 'não me deixa esquecer'.
    Pode ser vinculado a tarefa ou reunião existente.
    Para lembretes recorrentes: 'daily', 'weekdays', 'weekly:mon,wed,fri', 'monthly:15'.
    NÃO use para atividades sem horário definido (use criar_tarefa).
    """
    user_id: int = ctx.context.get("user_id")

    try:
        remind_at = datetime.fromisoformat(input.remind_at)
    except ValueError:
        return json.dumps({"error": f"Formato de data inválido: {input.remind_at}"}, ensure_ascii=False)

    if remind_at < datetime.now(TZ_SP).replace(tzinfo=None):
        return json.dumps(
            {"error": "Horário do lembrete está no passado. Confirme o horário com o diretor."},
            ensure_ascii=False,
        )

    reminder_type = "personalizado"
    if input.task_id:
        reminder_type = "tarefa"
    elif input.meeting_id:
        reminder_type = "reuniao"

    try:
        reminder = await crud.create_reminder(
            user_id=user_id,
            message=input.message,
            remind_at=remind_at,
            is_recurring=input.is_recurring or False,
            recurrence_rule=input.recurrence_rule,
            task_id=input.task_id,
            meeting_id=input.meeting_id,
            reminder_type=reminder_type,
        )
        return json.dumps(
            {
                "success": True,
                "reminder_id": reminder.id,
                "message": reminder.message,
                "remind_at": reminder.remind_at.isoformat(),
                "is_recurring": reminder.is_recurring,
                "recurrence_rule": reminder.recurrence_rule,
            },
            ensure_ascii=False,
        )
    except DuplicateReminderError as e:
        existing = e.existing
        return json.dumps(
            {
                "duplicate": True,
                "message": f"Lembrete similar já existe para {existing.remind_at.strftime('%H:%M')}: '{existing.message}'. Não criei novamente.",
                "existing_reminder_id": existing.id,
                "existing_message": existing.message,
                "existing_remind_at": existing.remind_at.isoformat(),
            },
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Erro ao criar lembrete: {e}")
        return json.dumps({"error": "Erro ao criar lembrete."}, ensure_ascii=False)


@function_tool
async def listar_lembretes(ctx: RunContextWrapper[dict], input: ListarLembretesInput) -> str:
    """
    Lista lembretes ativos do diretor.
    Use para 'meus lembretes', 'o que me lembra', 'tem lembrete pra hoje?'.
    """
    user_id: int = ctx.context.get("user_id")

    try:
        reminders = await crud.list_reminders(
            user_id=user_id,
            active_only=input.active_only if input.active_only is not None else True,
        )
        items = [
            {
                "reminder_id": r.id,
                "message": r.message,
                "remind_at": r.remind_at.isoformat(),
                "is_recurring": r.is_recurring,
                "recurrence_rule": r.recurrence_rule,
                "reminder_type": r.reminder_type,
            }
            for r in reminders
        ]
        return json.dumps({"reminders": items, "total": len(items)}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Erro ao listar lembretes: {e}")
        return json.dumps({"error": "Erro ao buscar lembretes."}, ensure_ascii=False)


@function_tool
async def desativar_lembrete(ctx: RunContextWrapper[dict], input: DesativarLembreteInput) -> str:
    """
    Remove/desativa um lembrete existente.
    Use quando o diretor disser 'cancela o lembrete', 'remove o lembrete de X', 'não precisa mais me lembrar'.
    Busca por ID ou por trecho da mensagem.
    Se encontrar mais de um lembrete com o mesmo trecho, lista as opções e confirma qual remover.
    Esta tool desativa APENAS lembretes. NUNCA chame outras tools de exclusão junto com esta.
    NÃO use para cancelar reuniões (use cancelar_reuniao, que desativa os lembretes vinculados automaticamente).
    """
    user_id: int = ctx.context.get("user_id")

    if not input.reminder_id and not input.message_search:
        return json.dumps({"error": "Informe o ID ou parte da mensagem do lembrete."}, ensure_ascii=False)

    try:
        reminders = await crud.deactivate_reminder(
            user_id=user_id,
            reminder_id=input.reminder_id,
            message_search=input.message_search,
        )
    except Exception as e:
        logger.error(f"Erro ao desativar lembrete: {e}")
        return json.dumps({"error": "Erro ao remover lembrete."}, ensure_ascii=False)

    if not reminders:
        return json.dumps({"error": "Nenhum lembrete ativo encontrado com esse critério."}, ensure_ascii=False)

    if len(reminders) > 1:
        options = [
            {"reminder_id": r.id, "message": r.message, "remind_at": r.remind_at.isoformat()}
            for r in reminders
        ]
        return json.dumps(
            {"ambiguity": True, "message": "Encontrei mais de um lembrete. Qual deles remover?", "options": options},
            ensure_ascii=False,
        )

    r = reminders[0]
    return json.dumps(
        {"success": True, "reminder_id": r.id, "message": r.message, "status": "desativado"},
        ensure_ascii=False,
    )


@function_tool
async def resumo_do_dia(ctx: RunContextWrapper[dict], input: ResumoDoDiaInput) -> str:
    """
    Gera resumo COMPLETO de um dia: tarefas pendentes + reuniões/agenda + lembretes.
    Use para 'meu dia', 'o que tenho hoje', 'resumo', 'como tá minha agenda'.
    Consulta o Google Calendar para reuniões quando disponível.
    """
    user_id: int = ctx.context.get("user_id")
    db_user = ctx.context.get("db_user")

    if input.date:
        try:
            base_date = datetime.fromisoformat(input.date)
        except ValueError:
            return json.dumps({"error": f"Formato de data inválido: {input.date}"}, ensure_ascii=False)
    else:
        base_date = datetime.now(TZ_SP).replace(tzinfo=None)

    start = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = base_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Tarefas pendentes — separadas em atrasadas e normais
    try:
        tasks = await crud.list_tasks(user_id=user_id, status="pendente")
        now_dt = base_date
        tasks_atrasadas = []
        tasks_pendentes = []
        for t in tasks:
            atrasada = t.due_date is not None and t.due_date < now_dt
            dias_atraso = max((now_dt - t.due_date).days, 0) if atrasada else 0
            item = {
                "task_id": t.id,
                "title": t.title,
                "priority": t.priority,
                "priority_emoji": PRIORITY_EMOJI.get(t.priority, "⚪"),
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "atrasada": atrasada,
                "dias_atraso": dias_atraso,
            }
            if atrasada:
                tasks_atrasadas.append(item)
            else:
                tasks_pendentes.append(item)
    except Exception as e:
        logger.error(f"Erro ao buscar tarefas para resumo: {e}")
        tasks_atrasadas = []
        tasks_pendentes = []

    # Reuniões — Google Calendar como fonte principal
    events_data = []
    events_source = "banco_local"

    if db_user and _gcal_module.user_has_calendar(db_user):
        gcal_events = await _gcal_module.list_events(db_user, start, end)
        if gcal_events is not None:
            events_data = gcal_events
            events_source = "google_calendar"

    if not events_data:
        try:
            meetings = await crud.list_meetings(user_id=user_id, date_from=start, date_to=end)
            events_data = [
                {
                    "title": m.title,
                    "datetime_start": m.datetime_start.isoformat(),
                    "location": m.location or "",
                    "meet_link": "",
                    "source": "banco_local",
                }
                for m in meetings
            ]
            events_source = "banco_local"
        except Exception as e:
            logger.error(f"Erro ao buscar reuniões para resumo: {e}")

    # Lembretes do dia
    try:
        reminders = await crud.list_reminders_for_day(user_id=user_id, date=base_date)
        reminders_data = [
            {"message": r.message, "remind_at": r.remind_at.isoformat()}
            for r in reminders
        ]
    except Exception as e:
        logger.error(f"Erro ao buscar lembretes para resumo: {e}")
        reminders_data = []

    return json.dumps(
        {
            "date": base_date.strftime("%d/%m/%Y"),
            "tasks_atrasadas": tasks_atrasadas,
            "tasks_pendentes": tasks_pendentes,
            "events": events_data,
            "events_source": events_source,
            "reminders": reminders_data,
        },
        ensure_ascii=False,
    )
