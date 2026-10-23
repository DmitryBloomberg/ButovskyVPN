# -*- coding: utf-8 -*-
"""Общая логика шаблонов и helpers."""
from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..database import get_web_user

templates = Jinja2Templates(directory="app/templates")


def current_user(request: Request):
    uid = request.session.get("web_user_id")
    if not uid:
        return None
    return get_web_user(uid)


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = dict(context or {})
    ctx.setdefault("user", current_user(request))
    ctx.setdefault("request", request)
    from ..settings import bot_username
    ctx.setdefault("bot_username", bot_username())
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
