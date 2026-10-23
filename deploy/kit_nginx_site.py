#!/usr/bin/env python3
"""Keep the 3X-UI KIT domain fallback proxying to the ButovskyVPN web app."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


NGINX_CONFIG = Path("/etc/nginx/conf.d/kit.conf")
PATCH_MARKER = "# ButovskyVPN site reverse proxy"
STATIC_LOCATION = re.compile(
    r"location\s+/\s*\{\s*root\s+/var/www/kit;\s*index\s+index\.html;\s*\}",
    re.MULTILINE,
)


def server_blocks(text: str):
    """Yield source offsets for top-level Nginx server blocks."""
    for match in re.finditer(r"(?m)^server\s*\{", text):
        end = re.search(r"(?m)^}", text[match.end() :])
        if end is None:
            continue
        block_end = match.end() + end.end()
        yield match.start(), block_end, text[match.start() : block_end]


def proxy_location(upstream: int) -> str:
    return f"""location / {{
        {PATCH_MARKER}
        proxy_pass http://127.0.0.1:{upstream};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }}"""


def patch_text(text: str, domain: str, upstream: int) -> str:
    candidates = []
    for start, end, block in server_blocks(text):
        if not re.search(
            r"(?m)^\s*listen\s+127\.0\.0\.1:\d+\s+ssl(?:\s|;)",
            block,
        ):
            continue
        if not re.search(
            rf"(?m)^\s*server_name\s+{re.escape(domain)};\s*(?:#.*)?$",
            block,
        ):
            continue
        if not re.search(
            r"(?m)^\s*ssl_certificate\s+/root/cert/domain/fullchain\.pem;",
            block,
        ):
            continue
        candidates.append((start, end, block))

    if len(candidates) != 1:
        raise ValueError(
            "Не найден единственный self-domain блок 3X-UI KIT; "
            "конфигурация Nginx оставлена без изменений."
        )

    start, end, block = candidates[0]
    if PATCH_MARKER in block:
        return text
    updated, count = STATIC_LOCATION.subn(proxy_location(upstream), block, count=1)
    if count != 1:
        raise ValueError(
            "В блоке домена не найден ожидаемый статический location 3X-UI KIT; "
            "конфигурация Nginx оставлена без изменений."
        )
    return text[:start] + updated + text[end:]


def atomic_write(path: Path, content: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, path.stat().st_mode & 0o777)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def apply_patch(domain: str, upstream: int) -> None:
    if not re.fullmatch(
        r"([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}",
        domain,
    ):
        raise ValueError("Недопустимый домен.")
    if not 1 <= upstream <= 65535:
        raise ValueError("Недопустимый порт приложения.")
    if not NGINX_CONFIG.is_file():
        raise FileNotFoundError("Конфигурация 3X-UI KIT пока не создана.")

    original = NGINX_CONFIG.read_text(encoding="utf-8")
    patched = patch_text(original, domain, upstream)
    if patched != original:
        atomic_write(NGINX_CONFIG, patched)

    result = subprocess.run(
        ["/usr/sbin/nginx", "-t", "-q"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if patched != original:
            atomic_write(NGINX_CONFIG, original)
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Проверка Nginx не прошла: {detail[:600]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--upstream", required=True, type=int)
    args = parser.parse_args()
    try:
        apply_patch(args.domain, args.upstream)
    except Exception as exc:
        print(f"butovskyvpn-kit-nginx-site: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
