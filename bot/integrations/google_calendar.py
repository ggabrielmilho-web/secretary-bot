import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]

TZ_SP = ZoneInfo("America/Sao_Paulo")


class GoogleCalendarClient:
    """Cliente Google Calendar com Domain-Wide Delegation.

    Usa uma Service Account para acessar agendas dos diretores
    sem necessidade de OAuth individual.
    """

    def __init__(self, service_account_file: str) -> None:
        self._service_account_file = service_account_file
        self._base_credentials = None
        self._load_credentials()

    def _load_credentials(self) -> None:
        try:
            from google.oauth2 import service_account
            self._base_credentials = service_account.Credentials.from_service_account_file(
                self._service_account_file, scopes=SCOPES
            )
            logger.info("Credenciais do Google Calendar carregadas com sucesso.")
        except FileNotFoundError:
            logger.warning(
                f"Arquivo de service account não encontrado: {self._service_account_file}. "
                "Integração Google Calendar desabilitada."
            )
        except Exception as e:
            logger.warning(f"Erro ao carregar credenciais Google: {e}. Integração desabilitada.")

    @property
    def available(self) -> bool:
        return self._base_credentials is not None

    def _get_service(self, user_email: str):
        from googleapiclient.discovery import build
        delegated = self._base_credentials.with_subject(user_email)
        return build("calendar", "v3", credentials=delegated)

    def _list_events_sync(self, user_email: str, time_min: datetime, time_max: datetime) -> list[dict]:
        if time_min.tzinfo is None:
            time_min = time_min.replace(tzinfo=TZ_SP)
        if time_max.tzinfo is None:
            time_max = time_max.replace(tzinfo=TZ_SP)
        service = self._get_service(user_email)
        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                timeZone="America/Sao_Paulo",
            )
            .execute()
        )
        events = events_result.get("items", [])
        return [
            {
                "google_event_id": ev["id"],
                "title": ev.get("summary", "Sem título"),
                "datetime_start": ev["start"].get("dateTime", ev["start"].get("date")),
                "datetime_end": ev["end"].get("dateTime", ev["end"].get("date")),
                "location": ev.get("location", ""),
                "description": ev.get("description", ""),
                "organizer": ev.get("organizer", {}).get("email", ""),
                "status": ev.get("status", ""),
                "attendees": [
                    {
                        "email": a.get("email"),
                        "name": a.get("displayName", ""),
                        "response": a.get("responseStatus", ""),
                    }
                    for a in ev.get("attendees", [])
                ],
                "meet_link": ev.get("hangoutLink", ""),
                "source": "google_calendar",
            }
            for ev in events
        ]

    def _create_event_sync(
        self,
        user_email: str,
        title: str,
        datetime_start: datetime,
        duration_minutes: int,
        location: Optional[str],
        description: Optional[str],
        attendees: Optional[list[str]],
    ) -> dict:
        service = self._get_service(user_email)
        end_dt = datetime_start + timedelta(minutes=duration_minutes)
        body: dict = {
            "summary": title,
            "start": {"dateTime": datetime_start.isoformat(), "timeZone": "America/Sao_Paulo"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]

        # Gera link do Google Meet automaticamente
        body["conferenceData"] = {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

        event = service.events().insert(
            calendarId="primary",
            body=body,
            conferenceDataVersion=1,
        ).execute()
        meet_link = event.get("hangoutLink", "")
        logger.info(f"Evento criado no Google Calendar: {event['id']} — {title} | Meet: {meet_link}")
        return {
            "google_event_id": event["id"],
            "link": event.get("htmlLink", ""),
            "meet_link": meet_link,
        }

    def _delete_event_sync(self, user_email: str, event_id: str, max_retries: int = 2) -> bool:
        import time
        service = self._get_service(user_email)
        for attempt in range(max_retries + 1):
            try:
                service.events().delete(calendarId="primary", eventId=event_id).execute()
                logger.info(f"Evento removido do Google Calendar: {event_id}")
                return True
            except Exception as e:
                if attempt < max_retries:
                    logger.warning(f"Falha ao deletar {event_id} (tentativa {attempt+1}/{max_retries+1}): {e}. Tentando novamente...")
                    time.sleep(2)
                else:
                    raise

    def _respond_to_invite_sync(self, user_email: str, event_id: str, response: str) -> bool:
        service = self._get_service(user_email)
        event = service.events().get(calendarId="primary", eventId=event_id).execute()
        for attendee in event.get("attendees", []):
            if attendee.get("email") == user_email:
                attendee["responseStatus"] = response
                break
        service.events().update(calendarId="primary", eventId=event_id, body=event).execute()
        logger.info(f"Convite {event_id} respondido: {response}")
        return True

    # ------------------------------------------------------------------
    # Async wrappers (evita bloquear o event loop do bot)
    # ------------------------------------------------------------------

    async def list_events(
        self, user_email: str, time_min: datetime, time_max: datetime
    ) -> list[dict]:
        if not self.available:
            return []
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._list_events_sync, user_email, time_min, time_max
            )
        except Exception as e:
            logger.error(f"Erro ao listar eventos do Google Calendar para {user_email}: {e}")
            return []

    async def create_event(
        self,
        user_email: str,
        title: str,
        datetime_start: datetime,
        duration_minutes: int = 60,
        location: Optional[str] = None,
        description: Optional[str] = None,
        attendees: Optional[list[str]] = None,
    ) -> Optional[dict]:
        if not self.available:
            return None
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                self._create_event_sync,
                user_email,
                title,
                datetime_start,
                duration_minutes,
                location,
                description,
                attendees,
            )
        except Exception as e:
            logger.error(f"[GCAL] Falha ao criar evento para {user_email}: {type(e).__name__}: {e}")
            return None

    async def delete_event(self, user_email: str, event_id: str) -> bool:
        if not self.available:
            return False
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._delete_event_sync, user_email, event_id)
        except Exception as e:
            logger.error(f"Erro ao remover evento {event_id} do Google Calendar: {e}")
            return False

    async def list_pending_invites(self, user_email: str) -> list[dict]:
        now = datetime.now(TZ_SP)
        future = now + timedelta(days=30)
        all_events = await self.list_events(user_email, now, future)
        pending = []
        for ev in all_events:
            for att in ev.get("attendees", []):
                if att.get("email") == user_email and att.get("response") == "needsAction":
                    pending.append(ev)
                    break
        return pending

    async def respond_to_invite(self, user_email: str, event_id: str, response: str) -> bool:
        if not self.available:
            return False
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._respond_to_invite_sync, user_email, event_id, response
            )
        except Exception as e:
            logger.error(f"Erro ao responder convite {event_id}: {e}")
            return False


# Instância global — inicializada no main.py
google_calendar: GoogleCalendarClient = None  # type: ignore


def init_google_calendar(service_account_file: str) -> GoogleCalendarClient:
    global google_calendar
    google_calendar = GoogleCalendarClient(service_account_file)
    return google_calendar
