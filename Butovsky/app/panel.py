# -*- coding: utf-8 -*-
"""Клиент панели 3x-UI для сайта.

Сайт обращается к той же панели, что и бот, используя логин/пароль из таблицы
`servers` базы данных бота (или из config.py). Протокол полностью совместим с
3x-ui (MHSanaei): /login по form-data + cookie-сессия, далее Clients API.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("butovsky.panel")


@dataclass
class ProvisionOutcome:
    ok: bool
    email: str
    sub_id: str
    subscription_url: str
    error: str = ""


class XUIColors:
    pass


class PanelClient:
    def __init__(self, base_url: str, login: str, password: str, verify_tls: bool = False):
        self.base_url = base_url.rstrip("/")
        self.login = login
        self.password = password
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(20.0),
            verify=verify_tls,
            follow_redirects=False,
        )
        self.authed = False

    async def close(self) -> None:
        await self._client.aclose()

    async def _ensure_login(self) -> bool:
        if self.authed:
            return True
        try:
            resp = await self._client.post("/login", data={"username": self.login, "password": self.password})
            if resp.status_code in (200, 302, 307):
                try:
                    body = resp.json()
                    if body.get("success") is False:
                        logger.error("Panel login failed: %s", body.get("msg"))
                        return False
                except Exception:
                    pass
                self.authed = True
                return True
            logger.error("Panel login HTTP %s", resp.status_code)
        except Exception as exc:
            logger.error("Panel login error: %s", exc)
        return False

    async def request(self, method: str, path: str, **kwargs) -> Optional[Dict[str, Any]]:
        if not await self._ensure_login():
            return None
        for attempt in (0, 1):
            try:
                resp = await self._client.request(method, path, **kwargs)
                if resp.status_code in (401, 403) and attempt == 0:
                    self.authed = False
                    if not await self._ensure_login():
                        return None
                    continue
                if resp.status_code >= 400:
                    logger.error("Panel %s %s -> HTTP %s", method, path, resp.status_code)
                    return None
                return resp.json()
            except Exception as exc:
                logger.error("Panel request error %s %s: %s", method, path, exc)
                return None
        return None

    # ------------------------------------------------------------- inbounds
    async def get_inbound_ids_for_group(self, group_id: Optional[int]) -> List[int]:
        """Возвращает id inbound'ов панели. Совпадение с группой тарифа — по tag'у (как в боте)."""
        data = await self.request("GET", "/panel/api/inbounds/list")
        if not data or not data.get("success"):
            return []
        result: List[int] = []
        for item in data.get("obj", []):
            tag = str(item.get("tag") or "")
            if group_id:
                # теги вида "group-2" / "g2" / содержащие номер группы
                tokens = tag.lower().replace("_", "-").split("-")
                if str(group_id) not in tokens and f"group{group_id}" not in tag.lower().replace("_", ""):
                    continue
            result.append(int(item["id"]))
        if not result:  # группа не найдена — берём все inbound'ы
            for item in data.get("obj", []):
                result.append(int(item["id"]))
        return result

    # ------------------------------------------------------------- provision
    async def provision_client(
        self,
        *,
        email: str,
        sub_id: str,
        total_bytes: int,
        expiry_ms: int,
        limit_ip: int = 1,
        enable: bool = True,
        tg_id: str = "",
    ) -> ProvisionOutcome:
        """Создаёт клиента во всех inbound'ах панели (endpoint /panel/api/inbounds/add/<id>)."""
        payload_client = {
            "uuid": str(uuid.uuid4()),
            "flow": "",
            "email": email,
            "enable": enable,
            "expiryTime": expiry_ms,
            "totalGB": total_bytes,
            "limitIp": limit_ip,
            "tgId": tg_id or "",
            "subId": sub_id,
            "reset": 0,
            "name": email,
        }
        inbound_ids = await self.get_inbound_ids_for_group(None)
        if not inbound_ids:
            return ProvisionOutcome(False, email, sub_id, "", "No inbounds found on panel")

        created_any = False
        last_error = ""
        for inbound_id in inbound_ids:
            detail = ""
            try:
                obj = self._build_detail_for_inbound(inbound_id, payload_client)
            except Exception as exc:
                last_error = str(exc)
                continue
            data = await self.request(
                "POST",
                f"/panel/api/inbounds/add/{inbound_id}",
                json={"settings": obj},
            )
            if data and data.get("success"):
                created_any = True
            else:
                last_error = str((data or {}).get("msg", "panel error"))
                # пробуем альтернативный современный эндпоинт clients/add
                data2 = await self.request(
                    "POST",
                    "/panel/api/clients/add",
                    json={**payload_client, "inboundIds": [inbound_id]},
                )
                if data2 and data2.get("success"):
                    created_any = True
                else:
                    last_error = str((data2 or {}).get("msg", last_error))

        if not created_any:
            return ProvisionOutcome(False, email, sub_id, "", last_error or "client creation failed")

        url = await self.build_subscription_url(sub_id) or ""
        return ProvisionOutcome(True, email, sub_id, url, "")

    @staticmethod
    def _build_detail_for_inbound(inbound_id: int, client: Dict[str, Any]) -> str:
        import json
        # старый формат: settings = JSON-строка {"clients": [...]}
        return json.dumps({"clients": [client]})

    async def build_subscription_url(self, sub_id: str) -> Optional[str]:
        data = await self.request("GET", "/panel/api/settings/list")
        settings: Dict[str, Any] = {}
        if data and data.get("success"):
            for row in data.get("obj", []):
                key = row.get("key")
                if key:
                    settings[key] = row.get("value")
        if not settings.get("subEnable"):
            # фолбэк: ссылка через саму панель
            return f"{self.base_url}/sub/{sub_id}"
        sub_uri = str(settings.get("subURI") or "").strip()
        if sub_uri:
            return f"{sub_uri.rstrip('/')}/{sub_id}"
        host = str(settings.get("subDomain") or "").strip()
        proto = self.base_url.split("://")[0]
        try:
            port = int(settings.get("subPort") or 0)
        except (TypeError, ValueError):
            port = 0
        path = "/" + str(settings.get("subPath") or "sub").strip("/")
        port_part = f":{port}" if port else ""
        domain = host or self.base_url.split("://", 1)[-1].split("/")[0].split(":")[0]
        scheme = "https" if str(settings.get("subCertFile") or "").strip() else proto
        return f"{scheme}://{domain}{port_part}{path}/{sub_id}"


# ------------------------------------------------------------------ helpers
def generate_site_email(telegram_id: Optional[int]) -> str:
    prefix = f"user_{telegram_id}_" if telegram_id else "user_web_"
    return f"{prefix}{secrets.token_hex(3)}"


async def provision_key(server: Dict[str, Any], tariff: Dict[str, Any],
                        telegram_id: Optional[int]) -> ProvisionOutcome:
    """Полный цикл: логин к панели по данным сервера -> создание клиента."""
    protocol = server.get("protocol") or "https"
    host = server["host"]
    port = server["port"]
    path = str(server.get("web_base_path") or "").strip("/")
    base_url = f"{protocol}://{host}:{port}" + (f"/{path}" if path else "")
    panel = PanelClient(base_url, str(server.get("login") or ""), str(server.get("password") or ""))
    try:
        email = generate_site_email(telegram_id)
        sub_id = uuid.uuid4().hex[:12]
        days = int(tariff.get("duration_days") or 30)
        expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
        total_bytes = int(tariff.get("traffic_limit_gb") or 0) * (1024 ** 3)
        if total_bytes <= 0:
            total_bytes = 1024 ** 4  # безлимит как у бота (1TB)
        return await panel.provision_client(
            email=email,
            sub_id=sub_id,
            total_bytes=total_bytes,
            expiry_ms=expiry_ms,
            limit_ip=int(tariff.get("max_ips") or 1),
            tg_id=str(telegram_id or ""),
        )
    finally:
        await panel.close()
