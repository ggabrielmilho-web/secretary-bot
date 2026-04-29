import asyncio
import logging
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from bot.config import settings

if TYPE_CHECKING:
    from bot.database.models import User

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def get_auth_url(state: str) -> str:
    """Gera URL de autorização OAuth2 para o usuário conectar o Google Calendar."""
    try:
        from google_auth_oauthlib.flow import Flow

        client_config = {
            "web": {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
                "auth_uri": _GOOGLE_AUTH_URI,
                "token_uri": _GOOGLE_TOKEN_URI,
            }
        }
        flow = Flow.from_client_config(client_config, scopes=SCOPES, state=state)
        flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
        url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        return url
    except Exception as e:
        logger.error(f"Erro ao gerar URL OAuth2: {e}")
        raise


async def exchange_code(code: str, state: str) -> dict:
    """Troca código de autorização por access_token + refresh_token."""
    try:
        from google_auth_oauthlib.flow import Flow

        client_config = {
            "web": {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
                "auth_uri": _GOOGLE_AUTH_URI,
                "token_uri": _GOOGLE_TOKEN_URI,
            }
        }
        flow = Flow.from_client_config(client_config, scopes=SCOPES, state=state)
        flow.redirect_uri = settings.GOOGLE_REDIRECT_URI

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: flow.fetch_token(code=code),
        )
        creds = flow.credentials
        return {
            "access_token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_expiry": creds.expiry,
        }
    except Exception as e:
        logger.error(f"Erro ao trocar código OAuth2: {e}")
        raise


def _build_credentials(user: "User"):
    """Constrói objeto Credentials a partir dos tokens do usuário no banco."""
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=user.google_access_token,
        refresh_token=user.google_refresh_token,
        token_uri=_GOOGLE_TOKEN_URI,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )


def _refresh_if_expired_sync(creds) -> tuple:
    """Renova o token se expirado. Retorna (creds, renewed: bool)."""
    try:
        from google.auth.transport.requests import Request

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            return creds, True
        return creds, False
    except Exception as e:
        logger.warning(f"Falha ao renovar token Google: {e}")
        return creds, False


def _build_service_sync(user: "User"):
    """Constrói serviço Google Calendar. Retorna (service, creds, renewed)."""
    from googleapiclient.discovery import build

    creds = _build_credentials(user)
    creds, renewed = _refresh_if_expired_sync(creds)
    service = build("calendar", "v3", credentials=creds)
    return service, creds, renewed


def is_available() -> bool:
    """Retorna True se as credenciais OAuth2 estão configuradas."""
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def user_has_calendar(user: "User") -> bool:
    """Retorna True se o usuário tem tokens OAuth2 salvos."""
    return bool(user and user.google_access_token)


# ---------------------------------------------------------------------------
# Calendar operations
# ---------------------------------------------------------------------------

async def list_events(user: "User", time_min: datetime, time_max: datetime) -> list[dict]:
    """Lista eventos do Google Calendar do usuário no intervalo informado."""
    if not user_has_calendar(user):
        return []

    def _sync():
        try:
            service, creds, renewed = _build_service_sync(user)
            events_result = service.events().list(
                calendarId="primary",
                timeMin=time_min.isoformat() + "Z",
                timeMax=time_max.isoformat() + "Z",
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            return events_result.get("items", []), creds if renewed else None
        except Exception as e:
            logger.error(f"Erro ao listar eventos GCal (user {user.id}): {e}")
            return [], None

    loop = asyncio.get_event_loop()
    raw_events, new_creds = await loop.run_in_executor(None, _sync)

    if new_creds:
        from bot.database import crud
        await crud.update_google_tokens(
            user.id,
            new_creds.token,
            new_creds.refresh_token,
            new_creds.expiry,
        )

    result = []
    for ev in raw_events:
        start = ev.get("start", {})
        dt_start = start.get("dateTime") or start.get("date", "")

        meet_link = ""
        for ep in ev.get("conferenceData", {}).get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                meet_link = ep.get("uri", "")
                break

        attendees = [
            {"email": a.get("email", ""), "name": a.get("displayName", "")}
            for a in ev.get("attendees", [])
        ]

        result.append({
            "id": ev.get("id"),
            "title": ev.get("summary", "(sem título)"),
            "datetime_start": dt_start,
            "datetime_end": (ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date", "")),
            "location": ev.get("location", ""),
            "description": ev.get("description", ""),
            "organizer_email": ev.get("organizer", {}).get("email", ""),
            "attendees": attendees,
            "meet_link": meet_link,
            "status": ev.get("status", "confirmed"),
        })

    return result


async def create_event(
    user: "User",
    title: str,
    datetime_start: datetime,
    duration_minutes: int = 60,
    location: Optional[str] = None,
    description: Optional[str] = None,
    attendees: Optional[list] = None,
    create_meet_link: bool = False,
) -> dict:
    """Cria evento no Google Calendar. Retorna {google_event_id, event_link, meet_link}."""
    if not user_has_calendar(user):
        return {"google_event_id": None, "event_link": None, "meet_link": None}

    from datetime import timedelta
    import uuid

    end_dt = datetime_start + timedelta(minutes=duration_minutes)

    event_body: dict = {
        "summary": title,
        "start": {"dateTime": datetime_start.isoformat(), "timeZone": settings.TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": settings.TIMEZONE},
    }
    if location:
        event_body["location"] = location
    if description:
        event_body["description"] = description
    if attendees:
        event_body["attendees"] = [
            {"email": a} if isinstance(a, str) else a
            for a in attendees
        ]
    if create_meet_link:
        event_body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    def _sync():
        try:
            service, creds, renewed = _build_service_sync(user)
            kwargs = {"calendarId": "primary", "body": event_body, "sendUpdates": "all"}
            if create_meet_link:
                kwargs["conferenceDataVersion"] = 1
            created = service.events().insert(**kwargs).execute()

            meet_link = ""
            for ep in created.get("conferenceData", {}).get("entryPoints", []):
                if ep.get("entryPointType") == "video":
                    meet_link = ep.get("uri", "")
                    break

            return {
                "google_event_id": created.get("id"),
                "event_link": created.get("htmlLink"),
                "meet_link": meet_link,
            }, creds if renewed else None
        except Exception as e:
            logger.error(f"Erro ao criar evento GCal (user {user.id}): {e}")
            return {"google_event_id": None, "event_link": None, "meet_link": None}, None

    loop = asyncio.get_event_loop()
    result, new_creds = await loop.run_in_executor(None, _sync)

    if new_creds:
        from bot.database import crud
        await crud.update_google_tokens(user.id, new_creds.token, new_creds.refresh_token, new_creds.expiry)

    return result


async def delete_event(user: "User", google_event_id: str) -> bool:
    """Remove evento do Google Calendar. Retorna True se sucesso."""
    if not user_has_calendar(user):
        return False

    def _sync():
        import time as _time
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                service, creds, renewed = _build_service_sync(user)
                service.events().delete(
                    calendarId="primary",
                    eventId=google_event_id,
                    sendUpdates="all",
                ).execute()
                return True, creds if renewed else None
            except Exception as e:
                if attempt < max_retries:
                    _time.sleep(2)
                else:
                    logger.error(f"Erro ao deletar evento GCal {google_event_id}: {e}")
                    return False, None
        return False, None

    loop = asyncio.get_event_loop()
    success, new_creds = await loop.run_in_executor(None, _sync)

    if new_creds:
        from bot.database import crud
        await crud.update_google_tokens(user.id, new_creds.token, new_creds.refresh_token, new_creds.expiry)

    return success


async def list_pending_invites(user: "User") -> list[dict]:
    """Lista convites pendentes (needsAction) nos próximos 30 dias."""
    if not user_has_calendar(user):
        return []

    from datetime import timedelta
    now = datetime.utcnow()
    time_max = now + timedelta(days=30)

    def _sync():
        try:
            service, creds, renewed = _build_service_sync(user)
            events_result = service.events().list(
                calendarId="primary",
                timeMin=now.isoformat() + "Z",
                timeMax=time_max.isoformat() + "Z",
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            items = events_result.get("items", [])
            pending = []
            for ev in items:
                for att in ev.get("attendees", []):
                    if att.get("self") and att.get("responseStatus") == "needsAction":
                        start = ev.get("start", {})
                        dt_start = start.get("dateTime") or start.get("date", "")
                        pending.append({
                            "id": ev.get("id"),
                            "title": ev.get("summary", "(sem título)"),
                            "datetime_start": dt_start,
                            "organizer_email": ev.get("organizer", {}).get("email", ""),
                        })
                        break
            return pending, creds if renewed else None
        except Exception as e:
            logger.error(f"Erro ao listar convites pendentes (user {user.id}): {e}")
            return [], None

    loop = asyncio.get_event_loop()
    result, new_creds = await loop.run_in_executor(None, _sync)

    if new_creds:
        from bot.database import crud
        await crud.update_google_tokens(user.id, new_creds.token, new_creds.refresh_token, new_creds.expiry)

    return result


async def respond_to_invite(user: "User", event_id: str, response: str) -> bool:
    """Aceita ou recusa convite. response: 'accepted' ou 'declined'."""
    if not user_has_calendar(user):
        return False

    def _sync():
        try:
            service, creds, renewed = _build_service_sync(user)
            event = service.events().get(calendarId="primary", eventId=event_id).execute()
            for att in event.get("attendees", []):
                if att.get("self"):
                    att["responseStatus"] = response
                    break
            service.events().patch(
                calendarId="primary",
                eventId=event_id,
                body={"attendees": event.get("attendees", [])},
                sendUpdates="all",
            ).execute()
            return True, creds if renewed else None
        except Exception as e:
            logger.error(f"Erro ao responder convite {event_id}: {e}")
            return False, None

    loop = asyncio.get_event_loop()
    success, new_creds = await loop.run_in_executor(None, _sync)

    if new_creds:
        from bot.database import crud
        await crud.update_google_tokens(user.id, new_creds.token, new_creds.refresh_token, new_creds.expiry)

    return success
