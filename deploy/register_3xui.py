#!/usr/bin/env python3
"""Register the local 3X-UI KIT panel in the bot database using its Bearer token."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Any

from database.db_servers import add_server, delete_server, get_all_servers, update_server
from database.migrations import run_migrations
from bot.services.vpn_api import test_server_connection


def read_panel_env(path: Path) -> dict[str, str]:
    allowed = {"XUI_PANEL_PORT", "XUI_WEB_BASE_PATH", "XUI_API_TOKEN"}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in allowed:
            values[key] = value
    missing = allowed.difference(values)
    if missing:
        raise ValueError("Временная конфигурация панели неполная.")

    if not re.fullmatch(r"[0-9]{1,5}", values["XUI_PANEL_PORT"]):
        raise ValueError("Неверный локальный порт панели.")
    port = int(values["XUI_PANEL_PORT"])
    if not 1 <= port <= 65535:
        raise ValueError("Неверный локальный порт панели.")
    path_value = values["XUI_WEB_BASE_PATH"].strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", path_value):
        raise ValueError("Неверный секретный путь панели.")
    if not values["XUI_API_TOKEN"] or "\n" in values["XUI_API_TOKEN"]:
        raise ValueError("API token панели отсутствует.")

    return {
        "port": str(port),
        "web_base_path": f"/{path_value}/",
        "api_token": values["XUI_API_TOKEN"],
    }


async def validate(server: dict[str, Any]) -> bool:
    logging.disable(logging.CRITICAL)
    try:
        result = await test_server_connection(server)
        return bool(result.get("success"))
    finally:
        logging.disable(logging.NOTSET)


def same_endpoint(server: dict[str, Any], port: int, base_path: str) -> bool:
    return (
        str(server.get("protocol") or "").lower() == "https"
        and str(server.get("host") or "").lower() == "127.0.0.1"
        and int(server.get("port") or 0) == port
        and str(server.get("web_base_path") or "").strip("/") == base_path.strip("/")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("panel_env", type=Path)
    args = parser.parse_args()

    try:
        settings = read_panel_env(args.panel_env)
        port = int(settings["port"])
        base_path = settings["web_base_path"]
        run_migrations()

        server_data: dict[str, Any] = {
            "name": "ButovskyVPN 3X-UI",
            "host": "127.0.0.1",
            "port": port,
            "web_base_path": base_path,
            "protocol": "https",
            "login": "",
            "password": "",
            "api_token": settings["api_token"],
        }

        if not asyncio.run(validate(server_data)):
            print("Не удалось проверить API 3X-UI по локальному адресу.", file=sys.stderr)
            return 1

        existing = next(
            (
                server
                for server in get_all_servers()
                if same_endpoint(server, port, base_path)
            ),
            None,
        )
        if existing:
            server_id = int(existing["id"])
            update_server(server_id, api_token=settings["api_token"], is_active=1)
            server_data["id"] = server_id
            message = "Существующая запись локальной панели обновлена."
        else:
            server_id = add_server(
                name="ButovskyVPN 3X-UI",
                host="127.0.0.1",
                port=port,
                web_base_path=base_path,
                login="",
                password="",
                protocol="https",
                group_id=1,
                api_token=settings["api_token"],
            )
            server_data["id"] = server_id
            message = "Локальная 3X-UI добавлена в основную группу бота."

        if not asyncio.run(validate(server_data)):
            if existing:
                update_server(
                    server_id,
                    api_token=existing.get("api_token"),
                    is_active=existing.get("is_active", 1),
                )
            else:
                delete_server(server_id)
            print("Проверка сохранённой панели в базе бота не прошла.", file=sys.stderr)
            return 1
        print(message)
        return 0
    except Exception as exc:
        print(f"Не удалось зарегистрировать 3X-UI: {type(exc).__name__}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
