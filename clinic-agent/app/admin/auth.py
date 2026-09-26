"""Admin login (bcrypt), session cookie and CSRF tokens."""
from __future__ import annotations

import secrets
from pathlib import Path

import bcrypt
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.config import get_clinic, get_settings
from app.ratelimit import RateLimiter

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
login_limiter = RateLimiter(per_minute=10)


class LoginRequired(Exception):
    pass


def check_password(username: str, password: str) -> bool:
    s = get_settings()
    if not secrets.compare_digest(username.encode(), s.ADMIN_USERNAME.encode()):
        bcrypt.checkpw(b"x", s.ADMIN_PASSWORD_HASH.encode())  # constant-ish time
        return False
    try:
        return bcrypt.checkpw(password.encode(), s.ADMIN_PASSWORD_HASH.encode())
    except ValueError:
        return False


def current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise LoginRequired()
    return user


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def verify_csrf(request: Request) -> None:
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf")
    if not sent or not secrets.compare_digest(str(sent), request.session.get("csrf", "")):
        raise HTTPException(status_code=403, detail="CSRF check failed")


def render(request: Request, name: str, **ctx):
    ctx.setdefault("user", request.session.get("user"))
    ctx.setdefault("clinic", get_clinic())
    ctx.setdefault("env", get_settings().APP_ENV)
    ctx["csrf"] = csrf_token(request)
    return templates.TemplateResponse(request, name, ctx)
