import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from agents import function_tool, RunContextWrapper
from pydantic import BaseModel, Field

from bot.database import crud
from bot.database.crud import DuplicateMeetingError
import bot.integrations.google_calendar as _gcal_module

logger = logging.getLogger(__name__)

TZ_SP = ZoneInfo("America/Sao_Paulo")


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------

class CriarReuniaoInput(BaseModel):
    title: str = Field(description="Título da reunião ou compromisso")
    datetime_start: str = Field(description="Data e hora ISO 8601 (YYYY-MM-DDTHH:MM:SS)")
    duration_minutes: Optional[int] = Field(default=60, description="Duração em minutos (padrão 60)")
    location: Optional[str] = Field(default=None, description="Local da reunião (sala, link, endereço)")
    participants: Optional[str] = Field(default=None, description="Participantes separados por vírgula")
    description: Optional[str] = Field(default=None, description="Descrição ou pauta (opcional)")
    force: Optional[bool] = Field(default=False, description="True para criar mesmo com conflito de horário confirmado pelo diretor")


class ListarAgendaInput(BaseModel):
    date_from: str = Field(description="Data/hora inicial ISO 8601")
    date_to: str = Field(description="Data/hora final ISO 8601")


class CancelarReuniaoInput(BaseModel):
    meeting_id: Optional[int] = Field(default=None, description="ID numérico da reunião no banco local")
    title_search: Optional[str] = Field(default=None, description="Trecho do título para busca no banco local")
    google_event_id: Optional[str] = Field(default=None, description="ID do evento no Google Calendar (para eventos que não têm registro no banco local, como convites aceitos por e-mail)")
    google_event_title: Optional[str] = Field(default=None, description="Título descritivo do evento GCal para exibir no resultado")


class ResponderConviteInput(BaseModel):
    event_id: Optional[str] = Field(default=None, description="ID do evento no Google Calendar")
    title_search: Optional[str] = Field(default=None, description="Trecho do título do convite")
    response: str = Field(description="Resposta: 'accepted' para aceitar, 'declined' para recusar")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@function_tool
async def criar_reuniao(ctx: RunContextWrapper[dict], input: CriarReuniaoInput) -> str:
    """
    Agenda compromisso com DATA e HORA definidas, geralmente envolvendo outras pessoas ou local específico.
    Exemplos: 'reunião com João amanhã às 14h', 'call com fornecedor sexta'.
    NÃO use para atividades solo sem horário (use criar_tarefa).
    Sincroniza automaticamente com o Google Calendar do diretor.
    """
    user_id: int = ctx.context.get("user_id")
    user_email: Optional[str] = ctx.context.get("user_email")

    try:
        dt_start = datetime.fromisoformat(input.datetime_start)
    except ValueError:
        return json.dumps({"error": f"Formato de data inválido: {input.datetime_start}"}, ensure_ascii=False)

    if dt_start < datetime.now():
        return json.dumps(
            {"error": "Data da reunião está no passado. Confirme o horário com o diretor."},
            ensure_ascii=False,
        )

    hour = dt_start.hour
    if hour < 6 or hour >= 22:
        logger.warning(f"Reunião fora do horário comercial: {dt_start}")

    # Verifica conflitos cruzados (meetings + tasks) — a menos que o diretor já confirmou
    if not input.force:
        conflicts = await crud.find_schedule_conflicts(user_id, dt_start)
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

    participants_list = (
        [p.strip() for p in input.participants.split(",") if p.strip()]
        if input.participants
        else None
    )
    duration = input.duration_minutes or 60

    # Separa emails válidos de nomes — GCal só aceita emails como attendees
    gcal_attendees = [p for p in participants_list if "@" in p] if participants_list else None
    names_only = [p for p in participants_list if "@" not in p] if participants_list else []

    # Nomes sem email vão para a descrição do evento
    gcal_description = input.description or ""
    if names_only:
        nomes_str = "Participantes: " + ", ".join(names_only)
        gcal_description = f"{gcal_description}\n{nomes_str}".strip() if gcal_description else nomes_str

    # Tenta criar no Google Calendar; nunca perde a reunião por falha da API
    gcal_result = None
    gcal_status = "sem_integracao"

    if user_email and _gcal_module.google_calendar and _gcal_module.google_calendar.available:
        gcal_result = await _gcal_module.google_calendar.create_event(
            user_email=user_email,
            title=input.title,
            datetime_start=dt_start,
            duration_minutes=duration,
            location=input.location,
            description=gcal_description or None,
            attendees=gcal_attendees,
        )
        gcal_status = "sincronizado" if gcal_result else "falha_sincronizacao"
        if not gcal_result:
            logger.warning(f"Falha ao sincronizar reunião '{input.title}' com Google Calendar. Salvo apenas no banco.")

    google_event_id = gcal_result["google_event_id"] if gcal_result else None
    google_link = gcal_result.get("link", "") if gcal_result else ""
    meet_link = gcal_result.get("meet_link", "") if gcal_result else ""

    try:
        meeting = await crud.create_meeting(
            user_id=user_id,
            title=input.title,
            datetime_start=dt_start,
            duration_minutes=duration,
            location=input.location,
            participants=participants_list,
            description=input.description,
            google_event_id=google_event_id,
        )
    except DuplicateMeetingError as e:
        existing = e.existing
        return json.dumps(
            {
                "duplicate": True,
                "message": f"Já existe a reunião '{existing.title}' agendada para {existing.datetime_start.strftime('%d/%m/%Y às %H:%M')}. Não criei novamente.",
                "existing_meeting_id": existing.id,
                "existing_title": existing.title,
                "existing_datetime": existing.datetime_start.isoformat(),
            },
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Erro ao salvar reunião no banco: {e}")
        return json.dumps({"error": "Erro ao salvar reunião."}, ensure_ascii=False)

    # Lembrete automático 30 min antes — sempre criado, sem perguntar
    reminder_at = dt_start - timedelta(minutes=30)
    reminder_created = False
    if reminder_at > datetime.now():
        try:
            await crud.create_reminder(
                user_id=user_id,
                message=f"Reunião em 30 minutos: {input.title}",
                remind_at=reminder_at,
                is_recurring=False,
                recurrence_rule=None,
                task_id=None,
                meeting_id=meeting.id,
                reminder_type="reuniao",
            )
            reminder_created = True
        except Exception as e:
            logger.warning(f"Não foi possível criar lembrete automático para reunião {meeting.id}: {e}")

    result = {
        "success": True,
        "meeting_id": meeting.id,
        "title": meeting.title,
        "datetime_start": meeting.datetime_start.isoformat(),
        "duration_minutes": meeting.duration_minutes,
        "google_calendar": gcal_status,
        "lembrete_automatico": f"Lembrete criado para {reminder_at.strftime('%H:%M')} (30 min antes)" if reminder_created else "Lembrete não criado (reunião em menos de 30 min)",
    }
    if google_link:
        result["google_link"] = google_link
    if meet_link:
        result["meet_link"] = meet_link
    if gcal_status == "falha_sincronizacao":
        result["aviso"] = "Reunião salva localmente, mas não foi possível sincronizar com o Google Calendar. Verifique a integração."

    return json.dumps(result, ensure_ascii=False)


@function_tool
async def listar_agenda(ctx: RunContextWrapper[dict], input: ListarAgendaInput) -> str:
    """
    Lista reuniões e compromissos de um período.
    Use para 'agenda de hoje', 'agenda da semana', 'o que tenho amanhã'.
    Consulta o Google Calendar diretamente quando disponível (traz TODOS os eventos: criados pelo agente,
    aceitos via e-mail, recorrentes, criados manualmente). Caso contrário, usa o banco local.
    Retorna ordenado por horário.
    """
    user_email: Optional[str] = ctx.context.get("user_email")
    user_id: int = ctx.context.get("user_id")

    try:
        date_from = datetime.fromisoformat(input.date_from)
        date_to = datetime.fromisoformat(input.date_to)
    except ValueError as e:
        return json.dumps({"error": f"Formato de data inválido: {e}"}, ensure_ascii=False)

    # Google Calendar como fonte principal
    if user_email and _gcal_module.google_calendar and _gcal_module.google_calendar.available:
        events = await _gcal_module.google_calendar.list_events(user_email, date_from, date_to)
        if events is not None:
            return json.dumps({"events": events, "total": len(events), "source": "google_calendar"}, ensure_ascii=False)

    # Fallback: banco local
    try:
        meetings = await crud.list_meetings(user_id=user_id, date_from=date_from, date_to=date_to)
        items = [
            {
                "meeting_id": m.id,
                "title": m.title,
                "datetime_start": m.datetime_start.isoformat(),
                "duration_minutes": m.duration_minutes,
                "location": m.location,
                "source": "banco_local",
            }
            for m in meetings
        ]
        return json.dumps({"events": items, "total": len(items), "source": "banco_local"}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Erro ao listar agenda do banco: {e}")
        return json.dumps({"error": "Erro ao buscar agenda."}, ensure_ascii=False)


@function_tool
async def cancelar_reuniao(ctx: RunContextWrapper[dict], input: CancelarReuniaoInput) -> str:
    """
    Cancela reunião. Remove do Google Calendar e do banco local. Desativa lembretes vinculados.
    Retorna lista discriminada de cancelados e falhas — NUNCA assuma que todos deram certo.

    Dois cenários:
    SEMPRE prefira usar google_event_id quando disponível (retornado por listar_agenda).
    É mais confiável que title_search e funciona para eventos do banco E do GCal puro.

    Três caminhos (em ordem de preferência):
    1. google_event_id: busca registro local pelo GCal ID, cancela no GCal + banco + lembretes.
    2. meeting_id: cancela pelo ID do banco local + GCal + lembretes.
    3. title_search: busca por título no banco (menos confiável — use só se não tiver ID).

    Se houver mais de uma reunião similar no banco, LISTE as opções e pergunte qual cancelar.
    Esta tool cancela APENAS reuniões. Não afeta tarefas nem lembretes avulsos.
    """
    user_id: int = ctx.context.get("user_id")
    user_email: Optional[str] = ctx.context.get("user_email")

    cancelados = []
    falharam = []

    # --- Cenário B: google_event_id fornecido ---
    # Pode ser evento só no GCal (sem banco) OU evento do banco identificado pelo GCal ID
    if input.google_event_id:
        titulo = input.google_event_title or input.google_event_id

        # Tenta encontrar registro local correspondente (evento criado pelo bot)
        local_meeting = await crud.get_meeting_by_google_event_id(input.google_event_id)

        # Remove do Google Calendar
        gcal_ok = False
        if user_email and _gcal_module.google_calendar and _gcal_module.google_calendar.available:
            gcal_ok = await _gcal_module.google_calendar.delete_event(user_email, input.google_event_id)

        if not gcal_ok:
            falharam.append({"titulo": titulo, "erro": "falha ao remover do Google Calendar"})
            return json.dumps(
                {"cancelados": cancelados, "falharam": falharam,
                 "total_cancelados": 0, "total_falharam": 1},
                ensure_ascii=False,
            )

        # Se existe no banco, cancela e desativa lembretes
        if local_meeting:
            try:
                await crud.cancel_meeting(local_meeting.id)
                await crud.deactivate_reminders_by_meeting(local_meeting.id)
                titulo = f"{local_meeting.title} - {local_meeting.datetime_start.strftime('%d/%m %Hh')}"
            except Exception as e:
                logger.error(f"Erro ao cancelar reunião local {local_meeting.id}: {e}")

        cancelados.append({"titulo": titulo, "fonte": "google_calendar"})
        return json.dumps(
            {"cancelados": cancelados, "falharam": falharam,
             "total_cancelados": len(cancelados), "total_falharam": len(falharam)},
            ensure_ascii=False,
        )

    # --- Cenário A: evento com registro no banco ---
    if not input.meeting_id and not input.title_search:
        return json.dumps({"error": "Informe o ID, parte do título ou o google_event_id da reunião."}, ensure_ascii=False)

    try:
        meetings = await crud.get_meeting(
            user_id=user_id,
            meeting_id=input.meeting_id,
            title_search=input.title_search,
        )
    except Exception as e:
        logger.error(f"Erro ao buscar reunião: {e}")
        return json.dumps({"error": "Erro ao buscar reunião."}, ensure_ascii=False)

    if not meetings:
        return json.dumps({"error": "Nenhuma reunião encontrada com esse critério."}, ensure_ascii=False)

    if len(meetings) > 1:
        options = [
            {"meeting_id": m.id, "title": m.title, "datetime_start": m.datetime_start.isoformat()}
            for m in meetings
        ]
        return json.dumps(
            {"ambiguity": True, "message": "Encontrei mais de uma reunião. Qual delas cancelar?", "options": options},
            ensure_ascii=False,
        )

    meeting = meetings[0]
    titulo = f"{meeting.title} - {meeting.datetime_start.strftime('%d/%m %Hh')}"

    # Remove do Google Calendar com retry
    gcal_ok = True
    if user_email and meeting.google_event_id and _gcal_module.google_calendar and _gcal_module.google_calendar.available:
        gcal_ok = await _gcal_module.google_calendar.delete_event(user_email, meeting.google_event_id)
        if not gcal_ok:
            logger.error(f"Falha ao remover evento {meeting.google_event_id} do GCal após retries")

    # Cancela no banco e desativa lembretes
    try:
        await crud.cancel_meeting(meeting.id)
        await crud.deactivate_reminders_by_meeting(meeting.id)
        item = {"titulo": titulo, "meeting_id": meeting.id}
        if not gcal_ok:
            item["aviso"] = "Cancelado no banco, mas falhou no Google Calendar"
        cancelados.append(item)
    except Exception as e:
        logger.error(f"Erro ao cancelar reunião {meeting.id} no banco: {e}")
        falharam.append({"titulo": titulo, "erro": str(e)})

    return json.dumps(
        {"cancelados": cancelados, "falharam": falharam,
         "total_cancelados": len(cancelados), "total_falharam": len(falharam)},
        ensure_ascii=False,
    )


@function_tool
async def listar_convites_pendentes(ctx: RunContextWrapper[dict]) -> str:
    """
    Lista convites de reunião pendentes de resposta no Google Calendar.
    Use quando o diretor perguntar 'tem convite pendente?', 'alguém me chamou pra reunião?',
    'convites não respondidos'.
    """
    user_email: Optional[str] = ctx.context.get("user_email")

    if not user_email or not _gcal_module.google_calendar or not _gcal_module.google_calendar.available:
        return json.dumps(
            {"error": "Integração com Google Calendar não disponível para verificar convites."},
            ensure_ascii=False,
        )

    try:
        pending = await _gcal_module.google_calendar.list_pending_invites(user_email)
        return json.dumps({"convites_pendentes": pending, "total": len(pending)}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Erro ao listar convites pendentes: {e}")
        return json.dumps({"error": "Erro ao buscar convites pendentes."}, ensure_ascii=False)


@function_tool
async def responder_convite(ctx: RunContextWrapper[dict], input: ResponderConviteInput) -> str:
    """
    Aceita ou recusa um convite de reunião no Google Calendar.
    Use quando o diretor disser 'aceita esse convite', 'recusa a reunião de sexta', 'confirma presença'.
    response deve ser 'accepted' para aceitar ou 'declined' para recusar.
    """
    user_email: Optional[str] = ctx.context.get("user_email")

    if not user_email or not _gcal_module.google_calendar or not _gcal_module.google_calendar.available:
        return json.dumps(
            {"error": "Integração com Google Calendar não disponível."},
            ensure_ascii=False,
        )

    if input.response not in ("accepted", "declined"):
        return json.dumps(
            {"error": "Resposta inválida. Use 'accepted' para aceitar ou 'declined' para recusar."},
            ensure_ascii=False,
        )

    event_id = input.event_id
    if not event_id and input.title_search:
        # Tenta encontrar o evento por título nos convites pendentes
        pending = await _gcal_module.google_calendar.list_pending_invites(user_email)
        matches = [e for e in pending if input.title_search.lower() in e.get("title", "").lower()]
        if not matches:
            return json.dumps({"error": f"Nenhum convite encontrado com '{input.title_search}'."}, ensure_ascii=False)
        if len(matches) > 1:
            options = [{"event_id": e["google_event_id"], "title": e["title"]} for e in matches]
            return json.dumps(
                {"ambiguity": True, "message": "Encontrei mais de um convite. Qual deles?", "options": options},
                ensure_ascii=False,
            )
        event_id = matches[0]["google_event_id"]

    if not event_id:
        return json.dumps({"error": "Informe o ID do evento ou parte do título para busca."}, ensure_ascii=False)

    try:
        success = await _gcal_module.google_calendar.respond_to_invite(user_email, event_id, input.response)
        action = "aceito" if input.response == "accepted" else "recusado"
        return json.dumps(
            {"success": success, "event_id": event_id, "response": input.response, "message": f"Convite {action} com sucesso."},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Erro ao responder convite {event_id}: {e}")
        return json.dumps({"error": "Erro ao responder convite."}, ensure_ascii=False)
