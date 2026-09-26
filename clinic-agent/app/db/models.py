"""SQLAlchemy 2.x models. All timestamps are stored as UTC."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.dev import clock


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on both Postgres and SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _now() -> datetime:
    return clock.now()


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime(), dict: JSON}


class TimestampMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class Patient(TimestampMixin, Base):
    __tablename__ = "patients"

    phone_hash: Mapped[str] = mapped_column(String(64), unique=True)
    phone_enc: Mapped[str | None] = mapped_column(Text)
    name_enc: Mapped[str | None] = mapped_column(Text)
    age: Mapped[int | None]
    preferred_language: Mapped[str] = mapped_column(String(2), default="en")
    consent_given_at: Mapped[datetime | None]
    consent_version: Mapped[str | None] = mapped_column(String(16))
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    last_inbound_at: Mapped[datetime | None]
    noshow_count: Mapped[int] = mapped_column(Integer, default=0)
    late_cancel_count: Mapped[int] = mapped_column(Integer, default=0)
    anonymised_at: Mapped[datetime | None]


class Appointment(TimestampMixin, Base):
    __tablename__ = "appointments"

    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"), index=True)
    start_utc: Mapped[datetime]
    end_utc: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(16), default="booked")
    visit_reason_short_enc: Mapped[str | None] = mapped_column(Text)
    gcal_event_id: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), default="whatsapp")
    confirmed_by_patient_at: Mapped[datetime | None]

    session_date: Mapped[date] = mapped_column(Date)
    session: Mapped[str] = mapped_column(String(16))
    token_no: Mapped[int] = mapped_column(Integer)
    token_label: Mapped[str] = mapped_column(String(32))
    is_walk_in: Mapped[bool] = mapped_column(Boolean, default=False)

    queue_status: Mapped[str] = mapped_column(String(20), default="scheduled")
    queue_position: Mapped[float] = mapped_column(Float)
    priority: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_in_at: Mapped[datetime | None]
    called_at: Mapped[datetime | None]
    consult_started_at: Mapped[datetime | None]
    consult_ended_at: Mapped[datetime | None]
    skipped_at: Mapped[datetime | None]
    displacement_count: Mapped[int] = mapped_column(Integer, default=0)
    patient_reported_late_minutes: Mapped[int | None]
    arrived_hint_at: Mapped[datetime | None]  # patient said "I've arrived" (hint only)
    last_eta_sent_local: Mapped[datetime | None]  # stored UTC; "local" in the spec's sense of what we told them
    needs_staff_decision: Mapped[str | None] = mapped_column(String(32))  # e.g. "late_overrun", "calendar_conflict"

    cancelled_at: Mapped[datetime | None]
    cancelled_by: Mapped[str | None] = mapped_column(String(16))
    cancel_reason_code: Mapped[str | None] = mapped_column(String(32))
    is_late_cancel: Mapped[bool] = mapped_column(Boolean, default=False)

    rescheduled_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id"))
    previous_token_label: Mapped[str | None] = mapped_column(String(32))
    token_version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        # Final guard against double booking. Walk-ins have no slot so they're excluded.
        Index(
            "uq_appt_booked_slot",
            "start_utc",
            unique=True,
            postgresql_where=text("status = 'booked' AND is_walk_in = false"),
            sqlite_where=text("status = 'booked' AND is_walk_in = 0"),
        ),
        # Cancelled / rescheduled appointments keep their label for display, so the
        # uniqueness applies to live tokens only (a re-booked slot reuses the same token).
        Index(
            "uq_appt_session_token",
            "session_date",
            "session",
            "token_label",
            unique=True,
            postgresql_where=text("status NOT IN ('cancelled', 'rescheduled')"),
            sqlite_where=text("status NOT IN ('cancelled', 'rescheduled')"),
        ),
        Index("ix_appt_session", "session_date", "session"),
        Index("ix_appt_start", "start_utc"),
    )


class Waitlist(TimestampMixin, Base):
    __tablename__ = "waitlist"

    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"), index=True)
    session_date: Mapped[date] = mapped_column(Date)
    session: Mapped[str] = mapped_column(String(16))  # morning | evening | any
    status: Mapped[str] = mapped_column(String(16), default="waiting")


class SlotOffer(TimestampMixin, Base):
    __tablename__ = "slot_offers"

    slot_start_utc: Mapped[datetime]
    session_date: Mapped[date] = mapped_column(Date)
    session: Mapped[str] = mapped_column(String(16))
    source_appointment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id"))
    offer_type: Mapped[str] = mapped_column(String(16))  # move_earlier | waitlist
    offered_to_appointment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id"))
    offered_to_waitlist_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("waitlist.id"))
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"))
    status: Mapped[str] = mapped_column(String(16), default="open")
    expires_at: Mapped[datetime]
    cascade_depth: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_offer_slot", "slot_start_utc", "status"),)


class OutboxMessage(TimestampMixin, Base):
    __tablename__ = "outbox_messages"

    kind: Mapped[str] = mapped_column(String(16))  # whatsapp | gcal
    payload_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=_now)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    last_error: Mapped[str | None] = mapped_column(String(300))


class QueueEvent(TimestampMixin, Base):
    __tablename__ = "queue_events"

    appointment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointments.id"), index=True)
    event: Mapped[str] = mapped_column(String(32))
    ts: Mapped[datetime] = mapped_column(default=_now)
    actor: Mapped[str] = mapped_column(String(64))
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)


class SessionState(TimestampMixin, Base):
    __tablename__ = "session_state"

    session_date: Mapped[date] = mapped_column(Date)
    session: Mapped[str] = mapped_column(String(16))
    doctor_started_at: Mapped[datetime | None]
    doctor_delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    delay_until: Mapped[datetime | None]  # doctor expected (back) at this time after a declared delay
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[datetime | None]
    cancelled_by_clinic: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("session_date", "session", name="uq_session_state"),)


class FollowUp(TimestampMixin, Base):
    __tablename__ = "follow_ups"

    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"), index=True)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id"))
    due_date: Mapped[date] = mapped_column(Date)
    note_for_patient: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), default="pending")


class ReminderLog(TimestampMixin, Base):
    __tablename__ = "reminder_log"

    appointment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id"))
    follow_up_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("follow_ups.id"))
    kind: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime | None]
    wa_message_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")

    __table_args__ = (
        UniqueConstraint("appointment_id", "kind", name="uq_reminder_appt_kind"),
        UniqueConstraint("follow_up_id", "kind", name="uq_reminder_fu_kind"),
    )


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="NEW")
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)
    handoff_active: Mapped[bool] = mapped_column(Boolean, default=False)


class ProcessedMessage(TimestampMixin, Base):
    __tablename__ = "processed_messages"

    wa_message_id: Mapped[str] = mapped_column(String(128), unique=True)


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_log"

    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(default=_now)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)


class FakeCalendarEvent(TimestampMixin, Base):
    """Backs dev/fake_calendar.py. Busy blocks have appointment_id = NULL."""

    __tablename__ = "fake_calendar_events"

    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    summary: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    start_utc: Mapped[datetime]
    end_utc: Mapped[datetime]
    appointment_id: Mapped[str | None] = mapped_column(String(64))
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
