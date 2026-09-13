"""User-facing Butovsky Admin error messages."""
from __future__ import annotations

from bot.services.butovsky_admin import ButovskyAdminError
from bot.utils.telegram_links import build_telegram_link
from bot.utils.text import escape_html


def _is_api_key_error(error: ButovskyAdminError) -> bool:
    """Return True when the hub rejected the configured API key."""
    technical_message = str(error).casefold()
    return (
        error.kind == "authentication"
        or error.status_code in {401, 403}
        or "invalid api_key" in technical_message
    )


def butovsky_admin_error_alert(error: ButovskyAdminError) -> str:
    """Build a short plain-text error for a Telegram callback alert."""
    if error.kind == "configuration":
        return (
            "Ключ Butovsky Admin повреждён. "
            "Замените его в настройках Butovsky Admin."
        )
    if _is_api_key_error(error):
        return (
            "Текущий ключ Butovsky Admin больше не подходит. "
            "Если вы меняли сервер, выпустите новый ключ в @butovskysup."
        )
    if error.user_message:
        return error.user_message[:180]
    return (
        "Хаб Butovsky Admin временно недоступен. Возможно, идёт техническое "
        "обслуживание или обновление. Попробуйте ещё раз чуть позже."
    )


def format_butovsky_admin_error(
    error: ButovskyAdminError,
    *,
    title: str | None = None,
) -> str:
    """Build a safe, actionable Telegram HTML error without hub internals."""
    if error.kind == "configuration":
        default_title = "Ключ Butovsky Admin повреждён"
        icon = "❌"
        body = (
            "Сохранённый ключ имеет некорректный формат и не может быть "
            "отправлен в Butovsky Admin.\n\n"
            "Замените его в настройках Butovsky Admin."
        )
    elif _is_api_key_error(error):
        default_title = "Ключ Butovsky Admin не принят"
        icon = "❌"
        bot_link = build_telegram_link("butovskysup")
        body = (
            "Текущий ключ доступа больше не подходит.\n\n"
            "Возможно, вы меняли сервер. Выпустите новый ключ в "
            f'<a href="{bot_link}">@butovskysup</a>, затем замените его '
            "в настройках Butovsky Admin."
        )
    elif error.kind == "maintenance":
        default_title = "Хаб на техническом обслуживании"
        icon = "⏳"
        body = escape_html(
            error.user_message
            or "Сервис временно на обслуживании. Попробуйте снова чуть позже."
        )
    elif error.kind == "service_unavailable":
        default_title = "Хаб временно недоступен"
        icon = "⏳"
        body = escape_html(
            error.user_message
            or "Возможно, идёт техническое обслуживание или обновление. "
            "Попробуйте снова через несколько минут."
        )
    elif error.user_message:
        default_title = "Запрос не выполнен"
        icon = "⚠️"
        body = escape_html(error.user_message)
    else:
        default_title = "Хаб временно недоступен"
        icon = "⏳"
        body = (
            "Сервис временно не отвечает. Возможно, на хабе идёт техническое "
            "обслуживание или обновление. Попробуйте ещё раз чуть позже."
        )

    return f"{icon} <b>{escape_html(title or default_title)}</b>\n\n{body}"
