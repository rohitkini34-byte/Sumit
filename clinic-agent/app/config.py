"""Application settings (env vars) and clinic configuration (YAML)."""
from __future__ import annotations

from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: Literal["dev", "staging", "prod"] = "dev"
    WA_MODE: Literal["mock", "live"] = "mock"
    CALENDAR_MODE: Literal["fake", "google"] = "fake"
    LLM_MODE: Literal["live", "mock"] = "mock"
    DEV_ALLOWED_NUMBERS: str = ""
    PUBLIC_BASE_URL: str = "http://localhost:8000"
    ANTHROPIC_MONTHLY_BUDGET_NOTE: str = ""

    WA_PHONE_NUMBER_ID: str = ""
    WA_BUSINESS_ACCOUNT_ID: str = ""
    WA_ACCESS_TOKEN: str = ""
    WA_APP_SECRET: str = "dev-app-secret"
    WA_VERIFY_TOKEN: str = "dev-verify-token"
    WA_GRAPH_API_VERSION: str = "v21.0"

    ANTHROPIC_API_KEY: str = ""
    LLM_MODEL: str = "claude-haiku-4-5-20251001"

    GOOGLE_SERVICE_ACCOUNT_JSON_PATH: str = "/secrets/gcal-sa.json"
    GOOGLE_CALENDAR_ID: str = ""

    DATABASE_URL: str = "sqlite+aiosqlite:///./clinic_dev.db"
    # Dev-only fallback key so the app boots with zero config. Never used in prod (validated below).
    FERNET_KEY: str = "ZGV2LWZlcm5ldC1rZXktMzItYnl0ZXMtbG9uZyEhISE="
    PHONE_HASH_PEPPER: str = "dev-pepper"

    ADMIN_USERNAME: str = "admin"
    # bcrypt hash of "admin" (dev only)
    ADMIN_PASSWORD_HASH: str = "$2b$12$PXt3Vb4U/nwiqlNopy8ktO1oz/8b25zRuKEUHO2tKH2siVGAuvFMa"
    SESSION_SECRET: str = "dev-session-secret-change-me"
    QUEUE_DISPLAY_KEY: str = "dev-display-key"

    CLINIC_TIMEZONE: str = "Asia/Kolkata"
    STAFF_WHATSAPP_NUMBERS: str = ""
    CLINIC_CONFIG_PATH: str = str(APP_DIR / "clinic_config.yaml")
    ENABLE_SCHEDULER: bool = True

    @model_validator(mode="after")
    def _prod_guards(self) -> "Settings":
        if self.APP_ENV == "prod":
            problems = []
            if self.WA_MODE == "mock":
                problems.append("WA_MODE=mock")
            if self.CALENDAR_MODE == "fake":
                problems.append("CALENDAR_MODE=fake")
            if self.LLM_MODE == "mock":
                problems.append("LLM_MODE=mock")
            if self.DEV_ALLOWED_NUMBERS.strip():
                problems.append("DEV_ALLOWED_NUMBERS is set")
            if self.FERNET_KEY == Settings.model_fields["FERNET_KEY"].default:
                problems.append("FERNET_KEY is the dev default")
            if self.SESSION_SECRET == Settings.model_fields["SESSION_SECRET"].default:
                problems.append("SESSION_SECRET is the dev default")
            if self.PHONE_HASH_PEPPER == Settings.model_fields["PHONE_HASH_PEPPER"].default:
                problems.append("PHONE_HASH_PEPPER is the dev default")
            if problems:
                raise ValueError("Refusing to start in prod: " + ", ".join(problems))
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.CLINIC_TIMEZONE)

    @property
    def allowed_numbers(self) -> set[str]:
        return {n.strip() for n in self.DEV_ALLOWED_NUMBERS.split(",") if n.strip()}

    @property
    def staff_numbers(self) -> list[str]:
        return [n.strip() for n in self.STAFF_WHATSAPP_NUMBERS.split(",") if n.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")


# ---------------------------------------------------------------- clinic config


def _parse_hhmm(v: str) -> time:
    h, m = v.split(":")
    return time(int(h), int(m))


class SessionDef(BaseModel):
    prefix: str
    starts_before: time | None = None  # a slot starting before this time belongs to this session

    @field_validator("starts_before", mode="before")
    @classmethod
    def _t(cls, v):
        return _parse_hhmm(v) if isinstance(v, str) else v


class QueueCfg(BaseModel):
    sessions: dict[str, SessionDef]
    walk_in_prefix: str = "W"
    report_before_minutes: int = 10
    grace_minutes: int = 10
    late_reinsert_after: int = 2
    max_late_minutes: int = 45
    max_displacements_per_patient: int = 2
    eta_rolling_window: int = 5
    eta_round_minutes: int = 5
    eta_range_minutes: int = 10
    notify_when_patients_ahead: int = 3
    notify_eta_shift_minutes: int = 20
    noshow_warn_threshold: int = 2
    overrun_allowance_minutes: int = 15
    auto_close_after_minutes: int = 30


class OfferEarlierCfg(BaseModel):
    enabled: bool = True
    min_lead_minutes: int = 90
    offer_to_count: int = 3
    hold_minutes: int = 15
    max_cascade: int = 2


class WaitlistCfg(BaseModel):
    enabled: bool = True
    hold_minutes: int = 15
    max_per_patient: int = 2


class ChangesCfg(BaseModel):
    token_mode: Literal["slot", "sequential"] = "slot"
    token_freeze_time: time = time(20, 0)
    patient_cancel_cutoff_minutes: int = 0
    late_cancel_minutes: int = 120
    offer_earlier_slot: OfferEarlierCfg = Field(default_factory=OfferEarlierCfg)
    waitlist: WaitlistCfg = Field(default_factory=WaitlistCfg)

    @field_validator("token_freeze_time", mode="before")
    @classmethod
    def _t(cls, v):
        return _parse_hhmm(v) if isinstance(v, str) else v


class RemindersCfg(BaseModel):
    before_minutes: list[int] = [1440, 120]


class ClinicConfig(BaseModel):
    clinic_name: str
    doctor_name: str
    address: str = ""
    maps_link: str = ""
    phone: str = ""
    slot_minutes: int = 15
    buffer_minutes: int = 0
    min_lead_minutes: int = 60
    booking_horizon_days: int = 14
    max_active_bookings_per_patient: int = 2
    hours: dict[str, list[tuple[time, time]]]
    holidays: list[date] = []
    reminders: RemindersCfg = Field(default_factory=RemindersCfg)
    consultation_fee_text: str = ""
    queue: QueueCfg
    changes: ChangesCfg = Field(default_factory=ChangesCfg)

    @field_validator("hours", mode="before")
    @classmethod
    def _hours(cls, v):
        out = {}
        for day, ranges in (v or {}).items():
            out[day] = [(_parse_hhmm(a), _parse_hhmm(b)) for a, b in (ranges or [])]
        return out

    @field_validator("holidays", mode="before")
    @classmethod
    def _holidays(cls, v):
        return [date.fromisoformat(str(d)) for d in (v or [])]


def load_clinic_config(path: str | Path) -> ClinicConfig:
    with open(path, encoding="utf-8") as fh:
        return ClinicConfig.model_validate(yaml.safe_load(fh))


@lru_cache
def get_settings() -> Settings:
    return Settings()


_clinic_override: ClinicConfig | None = None


@lru_cache
def _load_default_clinic() -> ClinicConfig:
    return load_clinic_config(get_settings().CLINIC_CONFIG_PATH)


def get_clinic() -> ClinicConfig:
    return _clinic_override or _load_default_clinic()


def set_clinic_override(cfg: ClinicConfig | None) -> None:
    """Used by tests and the dev queue simulator to tune config values."""
    global _clinic_override
    _clinic_override = cfg
