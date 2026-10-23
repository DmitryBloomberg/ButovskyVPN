# -*- coding: utf-8 -*-
"""Регистрация, вход, привязка Telegram ID."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from .common import render, current_user
from ..database import (
    create_web_user, get_web_user_by_email, verify_password,
    get_web_user_by_link_token, bind_telegram,
)

router = APIRouter()


@router.get("/register")
async def register_page(request: Request):
    return render(request, "register.html")


@router.post("/register")
async def register(request: Request, email: str = Form(...), password: str = Form(...),
                   password2: str = Form(...)):
    if len(password) < 6:
        return render(request, "register.html", {"error": "Пароль должен быть не короче 6 символов"})
    if password != password2:
        return render(request, "register.html", {"error": "Пароли не совпадают"})
    user = create_web_user(email, password)
    if not user:
        return render(request, "register.html", {"error": "Email уже зарегистрирован"})
    request.session["web_user_id"] = user["id"]
    return RedirectResponse("/account", status_code=302)


@router.get("/login")
async def login_page(request: Request):
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = get_web_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        return render(request, "login.html", {"error": "Неверный email или пароль"})
    request.session["web_user_id"] = user["id"]
    return RedirectResponse("/account", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


@router.get("/link-telegram")
async def link_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    from ..settings import bot_username
    deep = f"https://t.me/{bot_username}?start=weblink_{user['link_token']}"
    return render(request, "link.html", {"user": user, "deep_link": deep})


@router.get("/telegram-bind/{token}")
async def telegram_bind(token: str, request: Request):
    """Страница-подтверждение после возврата из бота (?tg=<id>)."""
    user = get_web_user_by_link_token(token)
    if not user:
        return render(request, "link_result.html", {"ok": False, "msg": "Ссылка не найдена или истекла"})
    tg = request.query_params.get("tg")
    if not tg or not tg.isdigit():
        return render(request, "link_result.html", {"ok": False, "msg": "Отсутствует Telegram ID"})
    bind_telegram(user["id"], int(tg), request.query_params.get("u", ""))
    request.session["web_user_id"] = user["id"]
    return render(request, "link_result.html", {"ok": True, "msg": f"Telegram @{int(tg)} привязан к аккаунту"})
