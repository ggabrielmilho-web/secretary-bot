import json
import logging
from datetime import datetime, timedelta, date
from typing import Optional
from zoneinfo import ZoneInfo

from agents import function_tool, RunContextWrapper
from pydantic import BaseModel, Field

from bot.database import crud
from bot.database.crud import DuplicateTaskError

logger = logging.getLogger(__name__)

TZ_SP = ZoneInfo("America/Sao_Paulo")

PRIORITY_EMOJI = {"urgente": "🔴", "alta": "🟠", "media": "🟡", "baixa": "🟢"}


def _task_overdue_info(due_date: Optional[datetime], now: datetime) -> tuple[bool, int]:
    """Retorna (atrasada, dias_atraso). Tarefas sem due_date nunca são atrasadas."""
    if due_date is None:
        return False, 0
    delta = (now - due_date).days
    return delta > 0, max(delta, 0)


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------

class CriarTarefaInput(BaseModel):
    title: str = Field(description="Título curto e descritivo da tarefa")
    description: Optional[str] = Field(default=None, description="Descrição detalhada (opcional)")
    priority: Optional[str] = Field(
        default="media",
        description="Nível de prioridade: baixa, media, alta, urgente",
    )
    due_date: Optional[str] = Field(
        default=None,
        description="Data de vencimento ISO 8601 (YYYY-MM-DDTHH:MM:SS). Interpretar 'amanhã', 'sexta', etc.",
    )
    force: Optional[bool] = Field(default=False, description="True para criar mesmo com conflito de horário confirmado pelo diretor")
    is_recurring: Optional[bool] = Field(default=False, description="True quando a tarefa é recorrente (toda semana, todo dia, etc). O lembrete recorrente será criado automaticamente vinculado à tarefa.")
    recurrence_rule: Optional[str] = Field(default=None, description="Regra de recorrência: 'daily', 'weekdays', 'weekly:mon', 'weekly:fri', 'monthly:15'. Obrigatório quando is_recurring=True.")
    recurring_time: Optional[str] = Field(default=None, description="Hora do lembrete recorrente no formato HH:MM (ex: '10:00', '19:00'). Obrigatório quando is_recurring=True.")


class ListarTarefasInput(BaseModel):
    status: Optional[str] = Field(
        default="pendente",
        description="Status das tarefas: pendente, em_andamento, concluida, cancelada",
    )
    date_from: Optional[str] = Field(default=None, description="Data inicial ISO 8601 para filtro de vencimento")
    date_to: Optional[str] = Field(default=None, description="Data final ISO 8601 para filtro de vencimento")


class ConcluirTarefaInput(BaseModel):
    task_id: Optional[int] = Field(default=None, description="ID numérico da tarefa")
    title_search: Optional[str] = Field(default=None, description="Trecho do título para busca")


class ExcluirTarefaInput(BaseModel):
    task_id: Optional[int] = Field(default=None, description="ID numérico da tarefa")
    title_search: Optional[str] = Field(default=None, description="Trecho do título para busca")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@function_tool
async def criar_tarefa(ctx: RunContextWrapper[dict], input: CriarTarefaInput) -> str:
    """
    Cria uma nova tarefa/atividade de trabalho para o diretor.
    Use APENAS quando o diretor pedir para anotar algo que ele PRECISA EXECUTAR.
    Exemplos: 'anota aí', 'preciso fazer', 'não me deixa esquecer de fazer X'.
    NÃO use para compromissos com horário definido (use criar_reuniao).
    NÃO use para alertas de horário específico (use criar_lembrete).
    """
    user_id: int = ctx.context.get("user_id")

    due_date: Optional[datetime] = None
    if input.due_date:
        try:
            due_date = datetime.fromisoformat(input.due_date)
            if due_date < datetime.now(TZ_SP).replace(tzinfo=None):
                return json.dumps(
                    {"error": "Data de vencimento está no passado. Confirme a data com o diretor."},
                    ensure_ascii=False,
                )
        except ValueError:
            return json.dumps({"error": f"Formato de data inválido: {input.due_date}"}, ensure_ascii=False)

    priority = (input.priority or "media").lower()
    if priority not in ("baixa", "media", "alta", "urgente"):
        priority = "media"

    # Verifica conflitos cruzados apenas quando due_date tem hora específica (não meia-noite)
    if due_date and not input.force and (due_date.hour != 0 or due_date.minute != 0):
        conflicts = await crud.find_schedule_conflicts(user_id, due_date)
        if conflicts:
            items = "; ".join(
                f"{c['title']} ({c['datetime']})" for c in conflicts
            )
            return json.dumps(
                {
                    "conflict": True,
                    "message": f"Você já tem compromisso(s) nesse horário: {items}. Deseja manter os dois ou reagendar?",
                    "conflicts": conflicts,
                },
                ensure_ascii=False,
            )

    try:
        task = await crud.create_task(
            user_id=user_id,
            title=input.title,
            description=input.description,
            priority=priority,
            due_date=due_date,
            is_recurring=input.is_recurring or False,
            recurrence_rule=input.recurrence_rule if input.is_recurring else None,
            recurring_time=input.recurring_time if input.is_recurring else None,
        )
        # Lembrete automático 30 min antes — apenas se due_date tem hora específica
        reminder_created = False
        reminder_at = None
        if due_date and (due_date.hour != 0 or due_date.minute != 0):
            reminder_at = due_date - timedelta(minutes=30)
            if reminder_at > datetime.now():
                try:
                    await crud.create_reminder(
                        user_id=user_id,
                        message=f"Tarefa em 30 minutos: {input.title}",
                        remind_at=reminder_at,
                        is_recurring=False,
                        recurrence_rule=None,
                        task_id=task.id,
                        meeting_id=None,
                        reminder_type="tarefa",
                    )
                    reminder_created = True
                except Exception as e:
                    logger.warning(f"Não foi possível criar lembrete automático para tarefa {task.id}: {e}")

        # Lembrete recorrente — criado em código para garantir task_id vinculado
        lembrete_recorrente = None
        if input.is_recurring and input.recurrence_rule and input.recurring_time:
            try:
                from bot.scheduler.reminder_jobs import _next_occurrence
                hora, minuto = (int(p) for p in input.recurring_time.split(":")[:2])
                now = datetime.now(TZ_SP).replace(tzinfo=None)
                # Cria um datetime de referência para hoje com o horário solicitado
                base_dt = now.replace(hour=hora, minute=minuto, second=0, microsecond=0)
                # Calcula próxima ocorrência da tarefa
                next_dt = _next_occurrence(base_dt, input.recurrence_rule)
                if next_dt is None:
                    next_dt = base_dt + timedelta(days=1)
                # Lembrete dispara 30 min antes do horário da tarefa
                reminder_at = next_dt - timedelta(minutes=30)
                # Se ficar no passado, pula para a próxima ocorrência
                if reminder_at < datetime.now():
                    next_dt2 = _next_occurrence(next_dt, input.recurrence_rule)
                    if next_dt2:
                        reminder_at = next_dt2 - timedelta(minutes=30)
                        next_dt = next_dt2
                await crud.create_reminder(
                    user_id=user_id,
                    message=f"Tarefa em 30 minutos: {input.title}",
                    remind_at=reminder_at,
                    is_recurring=True,
                    recurrence_rule=input.recurrence_rule,
                    task_id=task.id,
                    meeting_id=None,
                    reminder_type="tarefa",
                )
                lembrete_recorrente = f"Lembrete criado para {reminder_at.strftime('%d/%m às %H:%M')} (30 min antes — {input.recurrence_rule})"
                logger.info(f"Lembrete recorrente criado para tarefa {task.id}: {input.recurrence_rule} às {reminder_at.strftime('%H:%M')}")
            except Exception as e:
                logger.warning(f"Não foi possível criar lembrete recorrente para tarefa {task.id}: {e}")

        return json.dumps(
            {
                "success": True,
                "task_id": task.id,
                "title": task.title,
                "priority": task.priority,
                "due_date": task.due_date.isoformat() if task.due_date else None,
                "lembrete_automatico": f"Lembrete criado para {reminder_at.strftime('%H:%M')} (30 min antes)" if reminder_created else None,
                "lembrete_recorrente": lembrete_recorrente,
            },
            ensure_ascii=False,
        )
    except DuplicateTaskError as e:
        existing = e.existing
        return json.dumps(
            {
                "duplicate": True,
                "message": f"Tarefa similar já existe: '{existing.title}'. Não criei novamente.",
                "existing_task_id": existing.id,
                "existing_title": existing.title,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Erro ao criar tarefa: {e}")
        return json.dumps({"error": "Erro interno ao criar tarefa."}, ensure_ascii=False)


@function_tool
async def listar_tarefas(ctx: RunContextWrapper[dict], input: ListarTarefasInput) -> str:
    """
    Lista tarefas do diretor.
    Use para 'minhas tarefas', 'o que tenho pendente', 'tarefas concluídas'.
    Retorna lista ordenada por prioridade.
    """
    user_id: int = ctx.context.get("user_id")

    date_from = datetime.fromisoformat(input.date_from) if input.date_from else None
    date_to = datetime.fromisoformat(input.date_to) if input.date_to else None

    try:
        tasks = await crud.list_tasks(
            user_id=user_id,
            status=input.status or "pendente",
            date_from=date_from,
            date_to=date_to,
        )
        now = datetime.now(TZ_SP).replace(tzinfo=None)
        items = []
        for t in tasks:
            atrasada, dias_atraso = _task_overdue_info(t.due_date, now)
            items.append({
                "task_id": t.id,
                "title": t.title,
                "priority": t.priority,
                "priority_emoji": PRIORITY_EMOJI.get(t.priority, "⚪"),
                "status": t.status,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "atrasada": atrasada,
                "dias_atraso": dias_atraso,
                "description": t.description,
                "is_recurring": t.is_recurring,
                "recurrence_rule": t.recurrence_rule,
                "recurring_time": t.recurring_time,
            })
        return json.dumps({"tasks": items, "total": len(items)}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Erro ao listar tarefas: {e}")
        return json.dumps({"error": "Erro ao buscar tarefas."}, ensure_ascii=False)


@function_tool
async def concluir_tarefa(ctx: RunContextWrapper[dict], input: ConcluirTarefaInput) -> str:
    """
    Marca tarefa como concluída.
    Use quando o diretor disser 'feito', 'concluído', 'pode tirar'.
    Se houver mais de uma tarefa similar, LISTE as opções e pergunte qual.
    """
    user_id: int = ctx.context.get("user_id")

    if not input.task_id and not input.title_search:
        return json.dumps({"error": "Informe o ID ou parte do título da tarefa."}, ensure_ascii=False)

    try:
        tasks = await crud.complete_task(
            user_id=user_id,
            task_id=input.task_id,
            title_search=input.title_search,
        )
        if not tasks:
            return json.dumps({"error": "Nenhuma tarefa encontrada com esse critério."}, ensure_ascii=False)
        if len(tasks) > 1:
            options = [{"task_id": t.id, "title": t.title, "priority": t.priority} for t in tasks]
            return json.dumps(
                {"ambiguity": True, "message": "Encontrei mais de uma tarefa. Qual delas?", "options": options},
                ensure_ascii=False,
            )
        task = tasks[0]
        await crud.deactivate_reminders_by_task(task.id)
        return json.dumps(
            {"success": True, "task_id": task.id, "title": task.title, "status": "concluida"},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Erro ao concluir tarefa: {e}")
        return json.dumps({"error": "Erro ao concluir tarefa."}, ensure_ascii=False)


@function_tool
async def excluir_tarefa(ctx: RunContextWrapper[dict], input: ExcluirTarefaInput) -> str:
    """
    Remove/cancela tarefa.
    Use SOMENTE quando o diretor disser explicitamente 'remove a tarefa X', 'cancela a tarefa X', 'exclui a tarefa X'.
    NUNCA chame esta tool quando o diretor pedir para remover LEMBRETE ou REUNIÃO — esta tool não tem relação com lembretes nem reuniões.
    NUNCA cancele tarefas por iniciativa própria. Só age quando o diretor pedir explicitamente.
    SEMPRE confirme antes de excluir se houver ambiguidade.
    """
    user_id: int = ctx.context.get("user_id")

    if not input.task_id and not input.title_search:
        return json.dumps({"error": "Informe o ID ou parte do título da tarefa."}, ensure_ascii=False)

    try:
        tasks = await crud.delete_task(
            user_id=user_id,
            task_id=input.task_id,
            title_search=input.title_search,
        )
        if not tasks:
            return json.dumps({"error": "Nenhuma tarefa encontrada com esse critério."}, ensure_ascii=False)
        if len(tasks) > 1:
            options = [{"task_id": t.id, "title": t.title} for t in tasks]
            return json.dumps(
                {"ambiguity": True, "message": "Encontrei mais de uma tarefa. Qual delas deseja remover?", "options": options},
                ensure_ascii=False,
            )
        task = tasks[0]
        await crud.deactivate_reminders_by_task(task.id)
        return json.dumps(
            {"success": True, "task_id": task.id, "title": task.title, "status": "cancelada"},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Erro ao excluir tarefa: {e}")
        return json.dumps({"error": "Erro ao remover tarefa."}, ensure_ascii=False)
