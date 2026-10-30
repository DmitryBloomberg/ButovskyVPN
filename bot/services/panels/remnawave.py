"""Remnawave panel adapter (official REST API, static Bearer token).

Docs: https://docs.remna.st — all requests go to the panel base URL with an
``Authorization: Bearer <api_token>`` header created in the panel's
"API Tokens" section.  There is no login/password or cookie session.

Key concepts used by this adapter:
  * Users replace 3x-ui clients (``username`` == managed panel email).
  * Internal squads replace inbound groups (``inbound_group_id`` -> squad uuid).
  * ``shortUuid`` replaces ``subId`` and forms the subscription link.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
import urllib.parse
import uuid as uuid_module
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import aiohttp

from config import RETRY_CONFIG

from .base import (
    BaseVPNClient,
    PanelClientDevice,
    PanelClientState,
    PanelDatabaseBackup,
    PanelErrorKind,
    PanelProvisionResult,
    PanelRejectedError,
    PanelRequestError,
    PanelServerSnapshot,
    VPNAPIError,
)

logger = logging.getLogger(__name__)

DEFAULT_PANEL_TIMEOUT_SECONDS = 15

# Remnawave REST prefix (Cabinet API exposed by the panel itself).
API_PREFIX = "/api/cabinet"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _iso_to_ms(value: Any) -> int:
    """Convert a Remnawave ISO-8601 timestamp into epoch milliseconds."""
    text = str(value or "").strip()
    if not text:
        return 0
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int(parsed.timestamp() * 1000))


def _ms_to_iso(value_ms: int) -> str:
    """Epoch milliseconds -> ISO-8601 UTC string expected by Remnawave."""
    seconds = max(0, int(value_ms)) / 1000.0
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class RemnawaveClient(BaseVPNClient):
    """Subscription-only client backed by the Remnawave Cabinet REST API."""

    def __init__(self, server: dict):
        self.server = server
        self.server_id = server.get("id")
        self.host = str(server["host"])
        self.port = int(server["port"])
        self.protocol = str(server.get("protocol") or "https")
        path = str(server.get("web_base_path") or "").strip("/")
        path = f"/{path}" if path else ""
        self.base_url = f"{self.protocol}://{self.host}:{self.port}{path}"
        self.api_token: Optional[str] = str(server.get("api_token") or "").strip() or None

        raw_inbound_group_id = server.get("inbound_group_id")
        if raw_inbound_group_id in (None, ""):
            self.inbound_group_id: Optional[int] = None
        else:
            self.inbound_group_id = int(raw_inbound_group_id)
            if self.inbound_group_id <= 0:
                raise ValueError("inbound_group_id must be a positive integer")

        # Optional explicit subscription page domain (SUB_PUBLIC_DOMAIN).
        sub_base = str(server.get("subscription_base_url") or "").strip()
        self.subscription_base_url = sub_base.rstrip("/") or None

        self.is_authenticated = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._squad_cache: Optional[List[Dict[str, Any]]] = None
        self._operation_metrics: contextvars.ContextVar[Optional[Dict[str, Any]]] = (
            contextvars.ContextVar(
                f"remnawave_operation_metrics_{id(self)}", default=None
            )
        )

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    timeout = aiohttp.ClientTimeout(total=DEFAULT_PANEL_TIMEOUT_SECONDS)
                    self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @staticmethod
    def _retry_policy(retry: bool) -> tuple[int, list[float]]:
        if retry:
            return 2, [0.5, 1.0]
        return 1, []

    def _headers(self) -> Dict[str, str]:
        if not self.api_token:
            raise PanelRequestError(
                PanelErrorKind.UNAUTHORIZED,
                endpoint=API_PREFIX,
                detail="Remnawave API token is not configured for this server",
            )
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }

    def _safe_detail(self, value: Any) -> str:
        text = str(value or "")[:300]
        if self.api_token:
            text = text.replace(self.api_token, "***")
        return text

    @staticmethod
    def _response_error_kind(status: int) -> PanelErrorKind:
        if status in (401,):
            return PanelErrorKind.UNAUTHORIZED
        if status in (403,):
            return PanelErrorKind.FORBIDDEN
        if status == 404:
            return PanelErrorKind.NOT_FOUND
        if 400 <= status < 500:
            return PanelErrorKind.CLIENT_ERROR
        if status >= 500:
            return PanelErrorKind.SERVER_ERROR
        return PanelErrorKind.INVALID_RESPONSE

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        retry: bool = True,
    ) -> Dict[str, Any]:
        attempts, delays = self._retry_policy(retry)
        last_error: Optional[PanelRequestError] = None
        session = await self._ensure_session()

        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(delays[min(attempt - 1, len(delays) - 1)])
            try:
                async with session.request(
                    method,
                    f"{self.base_url}{endpoint}",
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                ) as response:
                    status = response.status
                    body_text = await response.text()
                    payload: Dict[str, Any] = {}
                    if body_text:
                        try:
                            parsed = json.loads(body_text)
                            payload = parsed if isinstance(parsed, dict) else {
                                "response": parsed
                            }
                        except json.JSONDecodeError:
                            payload = {}

                    if status >= 400:
                        kind = self._response_error_kind(status)
                        detail = self._safe_detail(
                            payload.get("message") or payload.get("error") or body_text
                        )
                        last_error = PanelRequestError(
                            kind, endpoint=endpoint, status=status, detail=detail
                        )
                        if kind in (
                            PanelErrorKind.TIMEOUT,
                            PanelErrorKind.NETWORK,
                            PanelErrorKind.SERVER_ERROR,
                        ):
                            continue
                        raise last_error

                    if payload.get("success") is not True:
                        message = self._safe_detail(payload.get("message"))
                        raise PanelRejectedError(
                            endpoint=endpoint,
                            status=status,
                            detail=message or "panel rejected the request",
                        )

                    self.is_authenticated = True
                    return payload
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = PanelRequestError(
                    PanelErrorKind.TIMEOUT
                    if isinstance(exc, asyncio.TimeoutError)
                    else PanelErrorKind.NETWORK,
                    endpoint=endpoint,
                    detail=self._safe_detail(exc),
                )
                continue

        assert last_error is not None
        raise last_error

    @staticmethod
    def _unwrap(payload: Dict[str, Any]) -> Any:
        return payload.get("response")

    # ------------------------------------------------------------------
    # Auth / capabilities
    # ------------------------------------------------------------------

    async def login(self) -> bool:
        """Remnawave uses static bearer tokens; validate that it still works."""
        return await self.validate_connection()

    async def validate_connection(self) -> bool:
        payload = await self._request("GET", f"{API_PREFIX}/system/metadata")
        version = str(
            (_unwrap(payload) or {}).get("panelVersion")
            if isinstance(_unwrap(payload), dict)
            else ""
        )
        if version:
            self.server["panel_version"] = version
            self.panel_version = version
        else:
            self.panel_version = str(self.server.get("panel_version") or "")
        return True

    def supports_client_external_links(self) -> bool:
        return False

    def supports_client_hwids(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Squads (topology analogue of inbounds)
    # ------------------------------------------------------------------

    async def get_squads(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        if self._squad_cache is not None and not force_refresh:
            return self._squad_cache
        payload = await self._request("GET", f"{API_PREFIX}/internal-squads")
        response = self._unwrap(payload)
        rows: List[Dict[str, Any]] = []
        if isinstance(response, dict):
            rows = list(response.get("items") or response.get("squads") or [])
        elif isinstance(response, list):
            rows = list(response)
        self._squad_cache = rows
        return rows

    async def resolve_squad_uuid(self, group_id: Optional[int]) -> Optional[str]:
        """Map the stored numeric inbound-group marker onto a squad uuid."""
        if group_id in (None, ""):
            return None
        marker = f"--{int(group_id)}"
        for squad in await self.get_squads():
            name = str(squad.get("name") or "")
            if marker in name:
                squad_uuid = str(squad.get("uuid") or "")
                if squad_uuid:
                    return squad_uuid
        # No matching squad found: fall back to all squads (no restriction).
        return None

    async def get_inbounds(self, include_ignored: bool = False) -> List[Dict[str, Any]]:
        """Expose squads in the neutral inbound shape used by the facade."""
        squads = await self.get_squads()
        result: List[Dict[str, Any]] = []
        for index, squad in enumerate(squads, start=1):
            members = squad.get("members") or {}
            result.append(
                {
                    "id": _to_int(squad.get("id"), index),
                    "protocol": "squad",
                    "remark": str(squad.get("name") or ""),
                    "tag": str(squad.get("name") or ""),
                    "port": None,
                    "enable": True,
                    "total": _to_int(members.get("usersCount")),
                    "uuid": str(squad.get("uuid") or ""),
                }
            )
        return result

    # ------------------------------------------------------------------
    # Users helpers
    # ------------------------------------------------------------------

    async def _list_user_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start = 0
        size = 100
        while True:
            payload = await self._request(
                "GET",
                f"{API_PREFIX}/users",
                params={"start": start, "size": size},
            )
            response = self._unwrap(payload) or {}
            batch = list(response.get("items") or []) if isinstance(response, dict) else []
            rows.extend(batch)
            total = _to_int(response.get("totalCount") if isinstance(response, dict) else 0)
            start += size
            if not batch or (total and start >= total) or len(batch) < size:
                break
        return rows

    async def _get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        quoted = urllib.parse.quote(str(username or "").strip(), safe="")
        try:
            payload = await self._request(
                "GET", f"{API_PREFIX}/users/by-username/{quoted}", retry=False
            )
        except PanelRequestError as exc:
            if exc.kind is PanelErrorKind.NOT_FOUND:
                return None
            raise
        response = self._unwrap(payload)
        return response if isinstance(response, dict) else None

    @staticmethod
    def _user_status_enabled(user: Dict[str, Any]) -> bool:
        return str(user.get("status") or "").upper() != "DISABLED"

    # ------------------------------------------------------------------
    # Sync snapshot
    # ------------------------------------------------------------------

    async def get_sync_snapshot(self) -> PanelServerSnapshot:
        squads = await self.get_squads()
        squad_ids = {_to_int(item.get("id"), index) for index, item in enumerate(squads, 1)}
        snapshot = PanelServerSnapshot(
            inbounds=await self.get_inbounds(),
            clients={},
            unavailable_inbound_ids=set(),
        )
        for row in await self._list_user_rows():
            username = str(row.get("username") or "").strip()
            if not username:
                continue
            member_of = {
                _to_int(squad.get("id"), index)
                for index, squad in enumerate(squads, 1)
                if any(
                    str(candidate.get("username") or "").lower() == username.lower()
                    for candidate in (squad.get("members") or {}).get("users") or []
                )
            }
            traffic_used = _to_int(row.get("trafficUsed"))
            limit_mb = _to_int(row.get("trafficLimit"))
            snapshot.clients[username.lower()] = PanelClientState(
                email=username,
                client=dict(row),
                inbound_ids=member_of.intersection(squad_ids),
                traffic_used=traffic_used,
                traffic_known=True,
                total_gb=limit_mb * 1024 * 1024,
                expiry_time=_iso_to_ms(row.get("expireAt")),
                enable=self._user_status_enabled(row),
                sub_id=str(row.get("shortUuid") or ""),
                limit_ip=1,
                limit_hwid=_to_int(row.get("hwidDeviceLimit")),
                reset=0,
                details_complete=True,
            )
        return snapshot

    # ------------------------------------------------------------------
    # Provisioning (key issuing)
    # ------------------------------------------------------------------

    async def provision_client(
        self,
        *,
        email: str,
        total_gb: int = 0,
        total_gb_bytes: Optional[int] = None,
        expire_days: int = 0,
        expiry_time_ms: Optional[int] = None,
        limit_ip: int = 1,
        limit_hwid: int = 0,
        enable: bool = True,
        tg_id: str = "",
        sub_id: Optional[str] = None,
        inbound_ids: Optional[Iterable[int]] = None,
    ) -> PanelProvisionResult:
        async with self.operation_metrics("provision_client"):
            existing = await self._get_user_by_username(email)
            total_bytes = (
                max(0, int(total_gb_bytes))
                if total_gb_bytes is not None
                else max(0, int(total_gb)) * 1024 ** 3
            )
            expiry = (
                max(0, int(expiry_time_ms))
                if expiry_time_ms is not None
                else (
                    0
                    if int(expire_days) == 0
                    else int((time.time() + int(expire_days) * 86400) * 1000)
                )
            )
            squad_uuid = await self.resolve_squad_uuid(self.inbound_group_id)
            user_uuid = str(existing.get("uuid") or "") if existing else ""

            if existing and user_uuid:
                patch: Dict[str, Any] = {
                    "userType": "regular",
                    "status": "ACTIVE" if enable else "DISABLED",
                    "trafficLimitMiB": max(0, total_bytes // (1024 * 1024)),
                    "usedTrafficMiB": _to_int(existing.get("trafficUsed")),
                    "rehashShortUuid": False,
                }
                if expiry > 0:
                    patch["expireAt"] = _ms_to_iso(expiry)
                if int(limit_hwid) > 0:
                    patch["hwidDeviceLimit"] = int(limit_hwid)
                if squad_uuid:
                    patch["internalSquads"] = [
                        {"squadUuid": squad_uuid}
                    ]
                await self._request(
                    "PATCH", f"{API_PREFIX}/users/{user_uuid}", json_body=patch
                )
                short_uuid = str(existing.get("shortUuid") or "") or None
                refreshed = await self._get_user_by_username(email)
                if refreshed:
                    short_uuid = str(refreshed.get("shortUuid") or "") or short_uuid
                if not short_uuid:
                    raise PanelRequestError(
                        PanelErrorKind.INVALID_RESPONSE,
                        endpoint=f"{API_PREFIX}/users/by-username",
                        detail="user has no shortUuid after update",
                    )
                attached = {_to_int(item.get("id"), index) for index, item in enumerate(await self.get_squads(), 1)} if squad_uuid else set()
                return PanelProvisionResult(
                    email=email,
                    sub_id=short_uuid,
                    attached_inbound_ids=attached,
                    complete=True,
                )

            body: Dict[str, Any] = {
                "username": email,
                "status": "ACTIVE" if enable else "DISABLED",
                "userType": "regular",
                "trafficLimitMiB": max(0, total_bytes // (1024 * 1024)),
                "usedTrafficMiB": 0,
                "description": self._normalize_tg_id(tg_id),
                "expireAt": _ms_to_iso(expiry) if expiry > 0 else None,
                "HWIDDeviceLimit": max(0, int(limit_hwid)) or None,
                "shortUuid": uuid_module.uuid4().hex[:12],
            }
            if squad_uuid:
                body["internalSquads"] = [{"squadUuid": squad_uuid}]
            else:
                body["noSquadAccess"] = True

            try:
                payload = await self._request(
                    "POST", f"{API_PREFIX}/users", json_body=body
                )
            except PanelRejectedError as exc:
                # Race/duplicate: recover the already existing user instead.
                recovered = await self._get_user_by_username(email)
                if not recovered:
                    raise exc
                payload_user = recovered
            else:
                payload_user = self._unwrap(payload) or {}

            short_uuid = str(payload_user.get("shortUuid") or "")
            if not short_uuid:
                recovered = await self._get_user_by_username(email)
                short_uuid = str((recovered or {}).get("shortUuid") or "")
            if not short_uuid:
                raise PanelRequestError(
                    PanelErrorKind.INVALID_RESPONSE,
                    endpoint=f"{API_PREFIX}/users",
                    detail="created user response lacks shortUuid",
                )

            attached = (
                {_to_int(item.get("id"), index) for index, item in enumerate(await self.get_squads(), 1)}
                if squad_uuid
                else set()
            )
            return PanelProvisionResult(
                email=email,
                sub_id=short_uuid,
                attached_inbound_ids=attached,
                complete=True,
            )

    @staticmethod
    def _normalize_tg_id(tg_id: str) -> str:
        text = str(tg_id or "").strip().lstrip("@")
        return f"tg:{text}" if text.isdigit() else text

    # ------------------------------------------------------------------
    # Lifecycle mutations
    # ------------------------------------------------------------------

    async def delete_client(self, email: str) -> bool:
        user = await self._get_user_by_username(email)
        if not user:
            return True
        user_uuid = str(user.get("uuid") or "")
        if not user_uuid:
            return False
        await self._request("DELETE", f"{API_PREFIX}/users/{user_uuid}")
        return True

    async def reset_client_traffic(self, email: str) -> bool:
        user = await self._get_user_by_username(email)
        if not user:
            return False
        user_uuid = str(user.get("uuid") or "")
        await self._request(
            "POST", f"{API_PREFIX}/users/{user_uuid}/actions/reset-traffic"
        )
        return True

    async def update_client_limit(self, email: str, total_gb_bytes: int) -> bool:
        user = await self._get_user_by_username(email)
        if not user:
            return False
        user_uuid = str(user.get("uuid") or "")
        await self._request(
            "PATCH",
            f"{API_PREFIX}/users/{user_uuid}",
            json_body={
                "userType": "regular",
                "trafficLimitMiB": max(0, int(total_gb_bytes) // (1024 * 1024)),
                "usedTrafficMiB": _to_int(user.get("trafficUsed")),
                "rehashShortUuid": False,
            },
        )
        return True

    async def extend_client_expiry(self, email: str, days: int) -> bool:
        user = await self._get_user_by_username(email)
        if not user:
            return False
        user_uuid = str(user.get("uuid") or "")
        current_ms = _iso_to_ms(user.get("expireAt"))
        base_ms = max(current_ms, int(time.time() * 1000))
        new_expiry_ms = base_ms + int(days) * 86400 * 1000
        await self._request(
            "PATCH",
            f"{API_PREFIX}/users/{user_uuid}",
            json_body={
                "userType": "regular",
                "expireAt": _ms_to_iso(new_expiry_ms),
                "status": "ACTIVE",
                "trafficLimitMiB": _to_int(user.get("trafficLimit")),
                "usedTrafficMiB": _to_int(user.get("trafficUsed")),
                "rehashShortUuid": False,
            },
        )
        return True

    async def disable_client(self, email: str) -> bool:
        user = await self._get_user_by_username(email)
        if not user:
            return False
        user_uuid = str(user.get("uuid") or "")
        await self._request("POST", f"{API_PREFIX}/users/{user_uuid}/actions/disable")
        return True

    async def enable_client(self, email: str) -> bool:
        user = await self._get_user_by_username(email)
        if not user:
            return False
        user_uuid = str(user.get("uuid") or "")
        await self._request("POST", f"{API_PREFIX}/users/{user_uuid}/actions/enable")
        return True

    # ------------------------------------------------------------------
    # Stats & status
    # ------------------------------------------------------------------

    async def get_server_status(self) -> Dict[str, Any]:
        payload = await self._request("GET", f"{API_PREFIX}/system/stats")
        response = self._unwrap(payload) or {}
        return dict(response) if isinstance(response, dict) else {}

    async def get_nodes(self) -> List[Dict[str, Any]]:
        payload = await self._request("GET", f"{API_PREFIX}/nodes")
        response = self._unwrap(payload) or {}
        if isinstance(response, dict):
            return list(response.get("items") or [])
        return list(response) if isinstance(response, list) else []

    async def get_online_clients_count(self) -> int:
        try:
            status = await self.get_server_status()
        except VPNAPIError:
            return 0
        users = status.get("usersOnline") or status.get("onlineUsers")
        return _to_int(users)

    async def get_stats(self) -> Dict[str, Any]:
        try:
            snapshot = await self.get_sync_snapshot()
            status = await self.get_server_status()
            cpu_raw = (status.get("cpu") or {}).get("usedPercent") if isinstance(status.get("cpu"), dict) else status.get("cpu")
            try:
                cpu = int(float(cpu_raw)) if cpu_raw is not None else None
            except (TypeError, ValueError):
                cpu = None
            return {
                "total_clients": len(snapshot.clients),
                "active_clients": sum(1 for item in snapshot.clients.values() if item.enable),
                "online_clients": await self.get_online_clients_count(),
                "total_traffic_bytes": sum(item.traffic_used for item in snapshot.clients.values()),
                "cpu_percent": cpu,
                "online": True,
            }
        except VPNAPIError as exc:
            logger.warning("remnawave stats unavailable server_id=%s: %s", self.server_id, exc)
            return {
                "total_clients": 0,
                "active_clients": 0,
                "online_clients": 0,
                "total_traffic_bytes": 0,
                "cpu_percent": None,
                "online": False,
                "error": str(exc),
            }

    async def get_client_stats(self, email: str) -> Optional[Dict[str, Any]]:
        user = await self._get_user_by_username(email)
        if not user:
            return None
        return {
            "email": str(user.get("username") or email),
            "traffic": _to_int(user.get("trafficUsed")) * 1024 * 1024,
            "totalGB": _to_int(user.get("trafficLimit")) * 1024 * 1024,
            "expiryTime": _iso_to_ms(user.get("expireAt")),
            "enable": self._user_status_enabled(user),
            "subId": str(user.get("shortUuid") or ""),
        }

    # ------------------------------------------------------------------
    # HWID devices
    # ------------------------------------------------------------------

    async def get_client_devices(self, email: str) -> List[PanelClientDevice]:
        user = await self._get_user_by_username(email)
        if not user:
            return []
        user_uuid = str(user.get("uuid") or "")
        payload = await self._request(
            "GET", f"{API_PREFIX}/hwid/devices/{urllib.parse.quote(user_uuid, safe='')}"
        )
        response = self._unwrap(payload) or {}
        items = list(response.get("items") or []) if isinstance(response, dict) else list(response)
        devices: List[PanelClientDevice] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            devices.append(
                PanelClientDevice(
                    id=str(item.get("fingerprintHash") or item.get("id") or ""),
                    first_seen=_to_int(item.get("firstSeenAt")) or None,
                    last_seen=_to_int(item.get("lastSeenAt")) or None,
                    device_model=str(item.get("deviceModel") or ""),
                    device_os=str(item.get("osPlatform") or ""),
                    os_version=str(item.get("osVersion") or ""),
                )
            )
        return devices

    async def delete_client_device(self, email: str, device_id: str) -> bool:
        user = await self._get_user_by_username(email)
        if not user:
            return False
        await self._request(
            "POST",
            f"{API_PREFIX}/hwid/devices/delete",
            json_body={
                "userUuid": str(user.get("uuid") or ""),
                "fingerprintHash": str(device_id or ""),
            },
        )
        return True

    # ------------------------------------------------------------------
    # Subscription link (key delivery)
    # ------------------------------------------------------------------

    async def get_subscription_link(self, sub_id: str) -> Optional[str]:
        """Build the public subscription-page URL from a user's shortUuid."""
        short_uuid = str(sub_id or "").strip()
        if not short_uuid:
            return None
        base = self.subscription_base_url
        if not base:
            base = f"{self.protocol}://{self.host}"
        return f"{base.rstrip('/')}/profile/{short_uuid}"

    # ------------------------------------------------------------------
    # Unsupported on Remnawave
    # ------------------------------------------------------------------

    async def get_database_backup(self) -> PanelDatabaseBackup:
        raise PanelRequestError(
            PanelErrorKind.UNSUPPORTED_API,
            endpoint=f"{API_PREFIX}/system/backups",
            detail="database backups are managed outside the Remnawave API",
        )

    # ------------------------------------------------------------------
    # Facade-compatible helpers used by vpn_api / panel_sync
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def operation_metrics(self, operation: str):
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if elapsed_ms > 5000:
                logger.info(
                    "remnawave slow operation=%s server_id=%s took=%sms",
                    operation,
                    self.server_id,
                    elapsed_ms,
                )

    async def hydrate_client_state(
        self, state: PanelClientState
    ) -> PanelClientState:
        """Refresh one snapshot row through the by-username endpoint."""
        user = await self._get_user_by_username(state.email)
        if not user:
            raise PanelRequestError(
                PanelErrorKind.NOT_FOUND,
                endpoint=f"{API_PREFIX}/users/by-username",
                detail="user disappeared",
            )
        state.client = dict(user)
        state.traffic_used = _to_int(user.get("trafficUsed"))
        state.total_gb = _to_int(user.get("trafficLimit")) * 1024 * 1024
        state.expiry_time = _iso_to_ms(user.get("expireAt"))
        state.enable = self._user_status_enabled(user)
        state.sub_id = str(user.get("shortUuid") or "") or state.sub_id
        state.limit_hwid = _to_int(user.get("hwidDeviceLimit"))
        state.details_complete = True
        return state

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


__all__ = ["RemnawaveClient"]
