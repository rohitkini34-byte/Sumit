"""Google Calendar v3 via a service account (scope: calendar.events only)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.calendar.base import CalendarError, CalendarEvent

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TZ_NAME = "Asia/Kolkata"


def _parse(ts: dict) -> datetime:
    raw = ts.get("dateTime") or ts.get("date")
    if "T" not in raw:  # all-day event
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class GoogleCalendar:
    def __init__(self, service, calendar_id: str):
        self._svc = service
        self._cal = calendar_id

    @classmethod
    def from_settings(cls) -> "GoogleCalendar":
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        from app.config import get_settings

        s = get_settings()
        creds = service_account.Credentials.from_service_account_file(
            s.GOOGLE_SERVICE_ACCOUNT_JSON_PATH, scopes=SCOPES
        )
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return cls(svc, s.GOOGLE_CALENDAR_ID)

    async def _run(self, req):
        try:
            return await asyncio.to_thread(req.execute, num_retries=2)
        except Exception as exc:  # googleapiclient.errors.HttpError and transport errors
            status = getattr(getattr(exc, "resp", None), "status", None)
            raise CalendarError(f"google calendar error status={status}") from exc

    async def _list(self, start_utc: datetime, end_utc: datetime) -> list[dict]:
        items, token = [], None
        while True:
            req = self._svc.events().list(
                calendarId=self._cal,
                timeMin=_iso(start_utc),
                timeMax=_iso(end_utc),
                singleEvents=True,
                showDeleted=False,
                maxResults=250,
                pageToken=token,
            )
            res = await self._run(req)
            items.extend(res.get("items", []))
            token = res.get("nextPageToken")
            if not token:
                return items

    async def freebusy(self, start_utc, end_utc):
        # events.list (not freebusy.query) so we can drop our own appointment events, which are
        # tracked in the DB, and so only the calendar.events scope is needed.
        out = []
        for ev in await self._list(start_utc, end_utc):
            if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                continue
            if ev.get("extendedProperties", {}).get("private", {}).get("appointment_id"):
                continue
            out.append((_parse(ev["start"]), _parse(ev["end"])))
        return out

    async def insert(self, appointment_id, summary, description, start_utc, end_utc):
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": _iso(start_utc), "timeZone": TZ_NAME},
            "end": {"dateTime": _iso(end_utc), "timeZone": TZ_NAME},
            "extendedProperties": {"private": {"appointment_id": appointment_id}},
        }
        res = await self._run(self._svc.events().insert(calendarId=self._cal, body=body))
        return res["id"]

    async def add_busy_block(self, start_utc, end_utc, summary: str = "Blocked by clinic") -> str:
        body = {
            "summary": summary,
            "start": {"dateTime": _iso(start_utc), "timeZone": TZ_NAME},
            "end": {"dateTime": _iso(end_utc), "timeZone": TZ_NAME},
        }
        res = await self._run(self._svc.events().insert(calendarId=self._cal, body=body))
        return res["id"]

    async def patch(self, event_id, start_utc, end_utc):
        body = {
            "start": {"dateTime": _iso(start_utc), "timeZone": TZ_NAME},
            "end": {"dateTime": _iso(end_utc), "timeZone": TZ_NAME},
        }
        await self._run(self._svc.events().patch(calendarId=self._cal, eventId=event_id, body=body))

    async def delete(self, event_id):
        try:
            await self._run(self._svc.events().delete(calendarId=self._cal, eventId=event_id))
        except CalendarError as exc:
            if "status=404" in str(exc) or "status=410" in str(exc):
                return
            raise

    async def get(self, event_id):
        try:
            ev = await self._run(self._svc.events().get(calendarId=self._cal, eventId=event_id))
        except CalendarError as exc:
            if "status=404" in str(exc) or "status=410" in str(exc):
                return None
            raise
        if ev.get("status") == "cancelled":
            return None
        return CalendarEvent(
            event_id=ev["id"],
            start_utc=_parse(ev["start"]),
            end_utc=_parse(ev["end"]),
            appointment_id=ev.get("extendedProperties", {}).get("private", {}).get("appointment_id"),
            summary=ev.get("summary", ""),
        )
