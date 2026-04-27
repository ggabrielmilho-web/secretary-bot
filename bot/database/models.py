from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.orm import relationship

from bot.database.connection import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    timezone = Column(String(50), default="America/Sao_Paulo")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())

    tasks = relationship("Task", back_populates="user", lazy="select")
    meetings = relationship("Meeting", back_populates="user", lazy="select")
    reminders = relationship("Reminder", back_populates="user", lazy="select")
    messages = relationship("ConversationMessage", back_populates="user", lazy="select")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    priority = Column(String(20), default="media")      # baixa, media, alta, urgente
    status = Column(String(20), default="pendente")     # pendente, em_andamento, concluida, cancelada
    due_date = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="tasks")
    reminders = relationship("Reminder", back_populates="task", lazy="select")


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    datetime_start = Column(DateTime, nullable=False)
    duration_minutes = Column(Integer, default=60)
    location = Column(String(200), nullable=True)
    participants = Column(Text, nullable=True)   # JSON string
    status = Column(String(20), default="agendada")     # agendada, concluida, cancelada
    google_event_id = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="meetings")
    reminders = relationship("Reminder", back_populates="meeting", lazy="select")


class Reminder(Base):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=True)
    reminder_type = Column(String(20), nullable=False)  # tarefa, reuniao, personalizado
    message = Column(Text, nullable=False)
    remind_at = Column(DateTime, nullable=False, index=True)
    is_recurring = Column(Boolean, default=False)
    recurrence_rule = Column(String(100), nullable=True)  # daily, weekly:mon,wed,fri, monthly:15
    is_sent = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship("User", back_populates="reminders")
    task = relationship("Task", back_populates="reminders")
    meeting = relationship("Meeting", back_populates="reminders")


class ConversationMessage(Base):
    __tablename__ = "conversation_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(String(20), nullable=False)        # user, assistant
    content = Column(Text, nullable=False)
    tool_calls = Column(Text, nullable=True)         # JSON (reservado para uso futuro)
    tool_call_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship("User", back_populates="messages")
