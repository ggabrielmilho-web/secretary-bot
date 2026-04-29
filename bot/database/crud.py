import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.connection import async_session
from bot.database.models import ConversationMessage, Coupon, Meeting, Payment, Reminder, Task, User

logger = logging.getLogger(__name__)


class DuplicateMeetingError(Exception):
    """Lançada quando já existe reunião no mesmo horário (±30 min) para o usuário."""
    def __init__(self, existing: "Meeting") -> None:
        self.existing = existing
        super().__init__(f"Reunião duplicada: {existing.title} em {existing.datetime_start}")


class DuplicateTaskError(Exception):
    """Lançada quando já existe tarefa com título similar no mesmo dia para o usuário."""
    def __init__(self, existing: "Task") -> None:
        self.existing = existing
        super().__init__(f"Tarefa duplicada: {existing.title}")


class DuplicateReminderError(Exception):
    """Lançada quando já existe lembrete similar no intervalo de ±5 minutos para o usuário."""
    def __init__(self, existing: "Reminder") -> None:
        self.existing = existing
        super().__init__(f"Lembrete duplicado: {existing.message} em {existing.remind_at}")


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

def check_subscription_status(user: Optional[User]) -> tuple[bool, str]:
    """Verifica se o usuário tem assinatura ativa. Função pura, sem acesso ao banco."""
    if not user:
        return False, "Use /start para criar sua conta."
    if not user.is_active:
        return False, "Sua conta está desativada. Entre em contato com o suporte."
    if user.plan == "lifetime":
        return True, ""
    if user.plan == "trial":
        if user.trial_ends_at and user.trial_ends_at > datetime.now():
            dias = (user.trial_ends_at - datetime.now()).days + 1
            return True, f"Trial ativo ({dias} dia(s) restante(s))"
        return False, (
            "⏰ Seu período de teste expirou!\n\n"
            "Para continuar usando, assine agora:\n"
            "Use /planos para ver as opções."
        )
    # monthly
    if user.subscription_ends_at and user.subscription_ends_at > datetime.now():
        return True, ""
    return False, (
        "⏰ Sua assinatura expirou!\n\n"
        "Use /renovar para continuar usando."
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()


async def create_user(telegram_id: int, name: str, trial_days: int = 7) -> User:
    async with async_session() as session:
        user = User(
            telegram_id=telegram_id,
            name=name,
            plan="trial",
            trial_ends_at=datetime.now() + timedelta(days=trial_days),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        logger.info(f"Novo usuário criado: {telegram_id} ({name}) — trial até {user.trial_ends_at}")
        return user


async def get_or_create_user(telegram_id: int, name: str, trial_days: int = 7) -> User:
    """Busca ou cria usuário. Usado internamente pelo agente."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                telegram_id=telegram_id,
                name=name,
                plan="trial",
                trial_ends_at=datetime.now() + timedelta(days=trial_days),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            await session.refresh(user)
            logger.info(f"Novo usuário criado via get_or_create: {telegram_id} ({name})")
        return user


async def get_all_active_users() -> list[User]:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.is_active == True))
        return list(result.scalars().all())


async def activate_plan(
    user_id: int,
    plan: str,
    subscription_ends_at: Optional[datetime] = None,
) -> Optional[User]:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.plan = plan
            user.is_active = True
            user.subscription_ends_at = subscription_ends_at
            if plan == "lifetime":
                user.subscription_ends_at = None
        return user


async def update_google_tokens(
    user_id: int,
    access_token: str,
    refresh_token: Optional[str],
    token_expiry: Optional[datetime],
) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.google_access_token = access_token
            if refresh_token:
                user.google_refresh_token = refresh_token
            user.google_token_expiry = token_expiry


async def deactivate_user(user_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.is_active = False


async def get_expired_trials() -> list[User]:
    """Usuários em trial que expiraram mas ainda estão ativos."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(
                and_(
                    User.plan == "trial",
                    User.is_active == True,
                    User.trial_ends_at <= datetime.now(),
                )
            )
        )
        return list(result.scalars().all())


async def get_expired_subscriptions() -> list[User]:
    """Usuários mensais com assinatura expirada mas ainda ativos."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(
                and_(
                    User.plan == "monthly",
                    User.is_active == True,
                    User.subscription_ends_at <= datetime.now(),
                )
            )
        )
        return list(result.scalars().all())


async def get_expiring_soon(days: int = 3) -> list[User]:
    """Usuários mensais cuja assinatura vence nos próximos X dias."""
    now = datetime.now()
    deadline = now + timedelta(days=days)
    async with async_session() as session:
        result = await session.execute(
            select(User).where(
                and_(
                    User.plan == "monthly",
                    User.is_active == True,
                    User.subscription_ends_at >= now,
                    User.subscription_ends_at <= deadline,
                )
            )
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------

async def get_coupon(code: str) -> Optional[Coupon]:
    async with async_session() as session:
        result = await session.execute(
            select(Coupon).where(Coupon.code == code.upper().strip())
        )
        return result.scalar_one_or_none()


async def use_coupon(coupon_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(select(Coupon).where(Coupon.id == coupon_id))
        coupon = result.scalar_one_or_none()
        if coupon:
            coupon.times_used = (coupon.times_used or 0) + 1


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

async def create_payment(
    user_id: int,
    mercadopago_payment_id: str,
    amount: float,
    status: str,
    plan: str = "monthly",
) -> Payment:
    async with async_session() as session:
        payment = Payment(
            user_id=user_id,
            mercadopago_payment_id=mercadopago_payment_id,
            amount=amount,
            status=status,
            plan=plan,
        )
        session.add(payment)
        await session.flush()
        await session.refresh(payment)
        return payment


async def get_payment_by_mp_id(mp_payment_id: str) -> Optional[Payment]:
    async with async_session() as session:
        result = await session.execute(
            select(Payment).where(Payment.mercadopago_payment_id == mp_payment_id)
        )
        return result.scalar_one_or_none()


async def update_payment_status(mp_payment_id: str, status: str) -> Optional[Payment]:
    async with async_session() as session:
        result = await session.execute(
            select(Payment).where(Payment.mercadopago_payment_id == mp_payment_id)
        )
        payment = result.scalar_one_or_none()
        if payment:
            payment.status = status
        return payment


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

PRIORITY_ORDER = {"urgente": 0, "alta": 1, "media": 2, "baixa": 3}


async def create_task(
    user_id: int,
    title: str,
    description: Optional[str] = None,
    priority: str = "media",
    due_date: Optional[datetime] = None,
) -> Task:
    async with async_session() as session:
        keyword = title.strip()[:40]
        dup_conditions = [
            Task.user_id == user_id,
            Task.title.ilike(f"%{keyword}%"),
            Task.status == "pendente",
        ]
        if due_date:
            day_start = due_date.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = due_date.replace(hour=23, minute=59, second=59, microsecond=999999)
            dup_conditions += [Task.due_date >= day_start, Task.due_date <= day_end]

        dup_result = await session.execute(select(Task).where(and_(*dup_conditions)))
        existing = dup_result.scalars().first()
        if existing:
            raise DuplicateTaskError(existing)

        task = Task(
            user_id=user_id,
            title=title,
            description=description,
            priority=priority,
            due_date=due_date,
        )
        session.add(task)
        await session.flush()
        await session.refresh(task)
        return task


async def list_tasks(
    user_id: int,
    status: str = "pendente",
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[Task]:
    async with async_session() as session:
        conditions = [Task.user_id == user_id, Task.status == status]
        if date_from:
            conditions.append(Task.due_date >= date_from)
        if date_to:
            conditions.append(Task.due_date <= date_to)

        result = await session.execute(select(Task).where(and_(*conditions)))
        tasks = list(result.scalars().all())
        tasks.sort(key=lambda t: PRIORITY_ORDER.get(t.priority, 99))
        return tasks


async def _find_tasks(session: AsyncSession, user_id: int, task_id: Optional[int], title_search: Optional[str]) -> list[Task]:
    if task_id:
        result = await session.execute(
            select(Task).where(and_(Task.id == task_id, Task.user_id == user_id))
        )
        task = result.scalar_one_or_none()
        return [task] if task else []
    if title_search:
        result = await session.execute(
            select(Task).where(
                and_(
                    Task.user_id == user_id,
                    Task.title.ilike(f"%{title_search}%"),
                    Task.status.notin_(["concluida", "cancelada"]),
                )
            )
        )
        return list(result.scalars().all())
    return []


async def complete_task(
    user_id: int,
    task_id: Optional[int] = None,
    title_search: Optional[str] = None,
) -> list[Task]:
    async with async_session() as session:
        tasks = await _find_tasks(session, user_id, task_id, title_search)
        for task in tasks:
            task.status = "concluida"
            task.completed_at = datetime.utcnow()
        return tasks


async def delete_task(
    user_id: int,
    task_id: Optional[int] = None,
    title_search: Optional[str] = None,
) -> list[Task]:
    async with async_session() as session:
        tasks = await _find_tasks(session, user_id, task_id, title_search)
        for task in tasks:
            task.status = "cancelada"
        return tasks


# ---------------------------------------------------------------------------
# Schedule conflict check (cross-table: meetings + tasks)
# ---------------------------------------------------------------------------

async def find_schedule_conflicts(
    user_id: int,
    dt: datetime,
    window_minutes: int = 30,
) -> list[dict]:
    dt_min = dt - timedelta(minutes=window_minutes)
    dt_max = dt + timedelta(minutes=window_minutes)
    conflicts = []

    async with async_session() as session:
        m_result = await session.execute(
            select(Meeting).where(
                and_(
                    Meeting.user_id == user_id,
                    Meeting.datetime_start >= dt_min,
                    Meeting.datetime_start <= dt_max,
                    Meeting.status == "agendada",
                )
            )
        )
        for m in m_result.scalars().all():
            conflicts.append({
                "type": "reuniao",
                "title": m.title,
                "datetime": m.datetime_start.strftime("%d/%m/%Y às %H:%M"),
            })

        t_result = await session.execute(
            select(Task).where(
                and_(
                    Task.user_id == user_id,
                    Task.due_date >= dt_min,
                    Task.due_date <= dt_max,
                    Task.status == "pendente",
                )
            )
        )
        for t in t_result.scalars().all():
            if t.due_date and (t.due_date.hour != 0 or t.due_date.minute != 0):
                conflicts.append({
                    "type": "tarefa",
                    "title": t.title,
                    "datetime": t.due_date.strftime("%d/%m/%Y às %H:%M"),
                })

    return conflicts


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

async def create_meeting(
    user_id: int,
    title: str,
    datetime_start: datetime,
    duration_minutes: int = 60,
    location: Optional[str] = None,
    participants: Optional[list] = None,
    description: Optional[str] = None,
    google_event_id: Optional[str] = None,
) -> Meeting:
    async with async_session() as session:
        dup_result = await session.execute(
            select(Meeting).where(
                and_(
                    Meeting.user_id == user_id,
                    Meeting.datetime_start >= datetime_start - timedelta(minutes=30),
                    Meeting.datetime_start <= datetime_start + timedelta(minutes=30),
                    Meeting.status == "agendada",
                )
            )
        )
        existing = dup_result.scalars().first()
        if existing:
            raise DuplicateMeetingError(existing)

        meeting = Meeting(
            user_id=user_id,
            title=title,
            datetime_start=datetime_start,
            duration_minutes=duration_minutes,
            location=location,
            participants=json.dumps(participants, ensure_ascii=False) if participants else None,
            description=description,
            google_event_id=google_event_id,
        )
        session.add(meeting)
        await session.flush()
        await session.refresh(meeting)
        return meeting


async def list_meetings(
    user_id: int,
    date_from: datetime,
    date_to: datetime,
) -> list[Meeting]:
    async with async_session() as session:
        result = await session.execute(
            select(Meeting).where(
                and_(
                    Meeting.user_id == user_id,
                    Meeting.datetime_start >= date_from,
                    Meeting.datetime_start <= date_to,
                    Meeting.status != "cancelada",
                )
            ).order_by(Meeting.datetime_start)
        )
        return list(result.scalars().all())


async def get_meeting(
    user_id: int,
    meeting_id: Optional[int] = None,
    title_search: Optional[str] = None,
) -> list[Meeting]:
    async with async_session() as session:
        if meeting_id:
            result = await session.execute(
                select(Meeting).where(
                    and_(Meeting.id == meeting_id, Meeting.user_id == user_id)
                )
            )
            m = result.scalar_one_or_none()
            return [m] if m else []
        if title_search:
            result = await session.execute(
                select(Meeting).where(
                    and_(
                        Meeting.user_id == user_id,
                        Meeting.title.ilike(f"%{title_search}%"),
                        Meeting.status != "cancelada",
                    )
                )
            )
            return list(result.scalars().all())
        return []


async def get_meeting_by_google_event_id(google_event_id: str) -> Optional[Meeting]:
    async with async_session() as session:
        result = await session.execute(
            select(Meeting).where(
                and_(Meeting.google_event_id == google_event_id, Meeting.status == "agendada")
            )
        )
        return result.scalar_one_or_none()


async def cancel_meeting(meeting_id: int) -> Optional[Meeting]:
    async with async_session() as session:
        result = await session.execute(select(Meeting).where(Meeting.id == meeting_id))
        meeting = result.scalar_one_or_none()
        if meeting:
            meeting.status = "cancelada"
        return meeting


async def complete_past_meetings() -> int:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with async_session() as session:
        result = await session.execute(
            select(Meeting).where(
                and_(Meeting.datetime_start < today_start, Meeting.status == "agendada")
            )
        )
        meetings = result.scalars().all()
        for m in meetings:
            m.status = "concluida"
        logger.info(f"Reuniões concluídas automaticamente: {len(meetings)}")
        return len(meetings)


async def deactivate_reminders_by_task(task_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(
                and_(Reminder.task_id == task_id, Reminder.is_active == True)
            )
        )
        reminders = result.scalars().all()
        for r in reminders:
            r.is_active = False
        return len(reminders)


async def deactivate_reminders_by_meeting(meeting_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(
                and_(Reminder.meeting_id == meeting_id, Reminder.is_active == True)
            )
        )
        reminders = result.scalars().all()
        for r in reminders:
            r.is_active = False
        return len(reminders)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

async def create_reminder(
    user_id: int,
    message: str,
    remind_at: datetime,
    is_recurring: bool = False,
    recurrence_rule: Optional[str] = None,
    task_id: Optional[int] = None,
    meeting_id: Optional[int] = None,
    reminder_type: str = "personalizado",
) -> Reminder:
    async with async_session() as session:
        keyword = message.strip()[:40]
        dup_result = await session.execute(
            select(Reminder).where(
                and_(
                    Reminder.user_id == user_id,
                    Reminder.message.ilike(f"%{keyword}%"),
                    Reminder.remind_at >= remind_at - timedelta(minutes=5),
                    Reminder.remind_at <= remind_at + timedelta(minutes=5),
                    Reminder.is_active == True,
                )
            )
        )
        existing = dup_result.scalars().first()
        if existing:
            raise DuplicateReminderError(existing)

        reminder = Reminder(
            user_id=user_id,
            message=message,
            remind_at=remind_at,
            is_recurring=is_recurring,
            recurrence_rule=recurrence_rule,
            task_id=task_id,
            meeting_id=meeting_id,
            reminder_type=reminder_type,
        )
        session.add(reminder)
        await session.flush()
        await session.refresh(reminder)
        return reminder


async def list_reminders(user_id: int, active_only: bool = True) -> list[Reminder]:
    async with async_session() as session:
        conditions = [Reminder.user_id == user_id]
        if active_only:
            conditions.append(Reminder.is_active == True)
            conditions.append(Reminder.is_sent == False)
        result = await session.execute(
            select(Reminder).where(and_(*conditions)).order_by(Reminder.remind_at)
        )
        return list(result.scalars().all())


async def list_pending_reminders() -> list[Reminder]:
    async with async_session() as session:
        now = datetime.now()
        result = await session.execute(
            select(Reminder).where(
                and_(
                    Reminder.remind_at <= now,
                    Reminder.is_sent == False,
                    Reminder.is_active == True,
                )
            )
        )
        return list(result.scalars().all())


async def mark_reminder_sent(reminder_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
        reminder = result.scalar_one_or_none()
        if reminder:
            reminder.is_sent = True


async def deactivate_reminder(
    user_id: int,
    reminder_id: Optional[int] = None,
    message_search: Optional[str] = None,
) -> list[Reminder]:
    async with async_session() as session:
        if reminder_id:
            result = await session.execute(
                select(Reminder).where(
                    and_(Reminder.id == reminder_id, Reminder.user_id == user_id)
                )
            )
            reminders = result.scalars().all()
        elif message_search:
            result = await session.execute(
                select(Reminder).where(
                    and_(
                        Reminder.user_id == user_id,
                        Reminder.message.ilike(f"%{message_search}%"),
                        Reminder.is_active == True,
                    )
                )
            )
            reminders = result.scalars().all()
        else:
            return []

        for r in reminders:
            r.is_active = False
        return list(reminders)


async def list_reminders_for_day(user_id: int, date: datetime) -> list[Reminder]:
    start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = date.replace(hour=23, minute=59, second=59, microsecond=999999)
    async with async_session() as session:
        result = await session.execute(
            select(Reminder).where(
                and_(
                    Reminder.user_id == user_id,
                    Reminder.remind_at >= start,
                    Reminder.remind_at <= end,
                    Reminder.is_active == True,
                )
            ).order_by(Reminder.remind_at)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Conversation History
# ---------------------------------------------------------------------------

async def save_message(
    user_id: int,
    role: str,
    content: str,
    tool_calls: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> ConversationMessage:
    async with async_session() as session:
        msg = ConversationMessage(
            user_id=user_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )
        session.add(msg)
        await session.flush()
        await session.refresh(msg)
        return msg


async def get_history(user_id: int, limit: int = 20) -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == user_id)
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit)
        )
        messages = list(reversed(result.scalars().all()))

    raw = [{"role": m.role, "content": m.content} for m in messages]

    cleaned: list[dict] = []
    for msg in raw:
        if not cleaned:
            if msg["role"] == "user":
                cleaned.append(msg)
        else:
            last_role = cleaned[-1]["role"]
            if msg["role"] != last_role:
                cleaned.append(msg)

    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)

    return cleaned


async def cleanup_old_messages(days: int = 7) -> int:
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with async_session() as session:
        result = await session.execute(
            select(ConversationMessage).where(ConversationMessage.created_at < cutoff)
        )
        old = result.scalars().all()
        count = len(old)
        for msg in old:
            await session.delete(msg)
        logger.info(f"Removidas {count} mensagens antigas do histórico.")
        return count
