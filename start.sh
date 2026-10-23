#!/usr/bin/env bash
set -Eeuo pipefail

# Bootstrap ButovskyVPN, 3X-UI KIT, the website and the Telegram bot.
# Run from the cloned repository: bash start.sh

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
CONFIG_FILE="$PROJECT_DIR/config.py"
SERVICE_NAME="butovsky-vpn.service"
EXPECTED_PUBLIC_IP="144.31.166.120"
DEFAULT_DOMAIN="butovskyvpn.duckdns.org"
KIT_URL="https://raw.githubusercontent.com/itsnotkubrick/3X-UI_KIT/v1.2.1/scripts/3x-ui.sh"
KIT_SHA256="e5ab4d53a6dce3e0d6aa5fe6ac9e43c1d13408e8556091a7940980f1352ef5c2"
KIT_TMP=""
PATCHED_KIT_TMP=""
PANEL_ENV_TMP=""
SERVICE_TMP=""
NGINX_DROPIN_TMP=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

fail() {
    printf '%b\n' "${RED}[✗]${NC} $1" >&2
    exit 1
}

run_as_root() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    else
        command -v sudo >/dev/null 2>&1 || fail "Для системной установки нужны права root или команда sudo."
        sudo "$@"
    fi
}

cleanup() {
    [[ -z "$KIT_TMP" ]] || rm -f -- "$KIT_TMP"
    [[ -z "$PATCHED_KIT_TMP" ]] || rm -f -- "$PATCHED_KIT_TMP"
    [[ -z "$SERVICE_TMP" ]] || rm -f -- "$SERVICE_TMP"
    [[ -z "$NGINX_DROPIN_TMP" ]] || rm -f -- "$NGINX_DROPIN_TMP"
    if [[ -n "$PANEL_ENV_TMP" ]]; then
        run_as_root rm -f -- "$PANEL_ENV_TMP" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

printf '%b\n' "${CYAN}========================================${NC}"
printf '%b\n' "${CYAN} ButovskyVPN — сайт, Telegram-бот и 3X-UI${NC}"
printf '%b\n\n' "${CYAN}========================================${NC}"

[[ -t 0 ]] || fail "Запустите bash start.sh в интерактивном терминале: установщик запросит настройки."
command -v apt-get >/dev/null 2>&1 || fail "Поддерживаются Debian/Ubuntu с apt."
command -v systemctl >/dev/null 2>&1 || fail "На сервере не найден systemd/systemctl."
[[ "$PROJECT_DIR" != *" "* ]] || fail "Переместите проект в путь без пробелов, например ~/ButovskyVPN."
if [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    fail "Для установки 3X-UI, Nginx и systemd нужны права root или команда sudo."
fi

read -r -p "Домен сайта и сертификата [${DEFAULT_DOMAIN}]: " WEB_DOMAIN
WEB_DOMAIN="${WEB_DOMAIN:-$DEFAULT_DOMAIN}"
[[ "$WEB_DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]] \
    || fail "Укажите домен без https:// и без пути, например ${DEFAULT_DOMAIN}."
WEB_DOMAIN="${WEB_DOMAIN,,}"

PANEL_ALREADY_INSTALLED=0
if [[ -x /usr/local/x-ui/x-ui && -f /etc/x-ui/install-result.env ]]; then
    PANEL_ALREADY_INSTALLED=1
    printf '%b\n' "${YELLOW}[!]${NC} Найдена существующая установка 3X-UI KIT — повторно менять панель не буду."
elif [[ -e /usr/local/x-ui ]]; then
    fail "На сервере уже есть другая установка 3X-UI. Скрипт остановлен, чтобы не менять существующую панель."
fi

PANEL_WEB_PATH=""
if [[ "$PANEL_ALREADY_INSTALLED" -eq 0 ]]; then
    read -r -s -p "Секретный путь панели 3x-ui (Enter — сгенерировать новый): " PANEL_WEB_PATH
    printf '\n'
    PANEL_WEB_PATH="${PANEL_WEB_PATH#/}"
    PANEL_WEB_PATH="${PANEL_WEB_PATH%/}"
    if [[ -n "$PANEL_WEB_PATH" && ! "$PANEL_WEB_PATH" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
        fail "Путь панели должен содержать только латинские буквы, цифры, _ или - (до 64 символов)."
    fi
fi

if [[ -f "$CONFIG_FILE" ]]; then
    read -r -p "Уже есть config.py. Перезаписать после создания резервной копии? [y/N]: " RECONFIGURE
    if [[ ! "$RECONFIGURE" =~ ^[YyДд]$ ]]; then
        printf '%b\n' "${YELLOW}[!]${NC} Существующий config.py оставлен без изменений."
        KEEP_CONFIG=1
    else
        CONFIG_BACKUP="${CONFIG_FILE}.backup.$(date +%Y%m%d%H%M%S)"
        cp -p -- "$CONFIG_FILE" "$CONFIG_BACKUP"
        chmod 600 "$CONFIG_BACKUP"
        printf '%b\n' "${GREEN}[✓]${NC} Резервная копия настроек создана."
        KEEP_CONFIG=0
    fi
else
    KEEP_CONFIG=0
fi

if [[ "$KEEP_CONFIG" -eq 0 ]]; then
    while true; do
        read -r -s -p "Токен Telegram-бота от @BotFather: " BOT_TOKEN
        printf '\n'
        if [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
            break
        fi
        printf '%b\n' "${RED}[✗]${NC} Проверьте формат токена Telegram-бота."
    done

    while true; do
        read -r -p "Telegram ID администратора: " ADMIN_ID
        if [[ "$ADMIN_ID" =~ ^[0-9]+$ ]]; then
            break
        fi
        printf '%b\n' "${RED}[✗]${NC} ID администратора должен состоять только из цифр."
    done

    read -r -p "Имя бота без @ (Enter — пропустить): " BOT_USERNAME
    BOT_USERNAME="${BOT_USERNAME#@}"
    [[ -z "$BOT_USERNAME" || "$BOT_USERNAME" =~ ^[A-Za-z0-9_]{5,32}$ ]] \
        || fail "Имя Telegram-бота должно содержать 5–32 латинских букв, цифр или _."
fi

run_as_root env DEBIAN_FRONTEND=noninteractive apt-get update
run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip python3-dev build-essential \
    git curl ca-certificates iproute2

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "Нужен Python 3.11 или новее. Используйте Ubuntu 24.04 или Debian 12+."
command -v ss >/dev/null 2>&1 || fail "Не удалось установить iproute2/команду ss."

SERVER_IP="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null | tr -d '[:space:]' || true)"
if [[ ! "$SERVER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    fail "Не удалось определить внешний IPv4 сервера. Проверьте сеть и запустите снова."
fi
if [[ "$SERVER_IP" != "$EXPECTED_PUBLIC_IP" ]]; then
    printf '%b\n' "${YELLOW}[!]${NC} Найден внешний IP ${SERVER_IP}; в запросе указан ${EXPECTED_PUBLIC_IP}."
    read -r -p "Продолжить с найденным IP ${SERVER_IP}? [Y/n]: " USE_DETECTED_IP
    [[ "$USE_DETECTED_IP" =~ ^[NnНн]$ ]] && fail "Остановлено. Направьте домен на нужный сервер и повторите запуск."
fi

if [[ "$PANEL_ALREADY_INSTALLED" -eq 0 ]]; then
    if ss -H -ltn 'sport = :80 or sport = :443' | grep -q .; then
        fail "Порт 80 или 443 уже занят. Не останавливаю существующий сайт/службу; сначала освободите порты."
    fi
    if [[ -d /etc/nginx/sites-enabled ]]; then
        while IFS= read -r -d '' site; do
            [[ "$(basename "$site")" == "default" ]] \
                || fail "Найдена сторонняя конфигурация Nginx. Сначала проверьте её и освободите порты 80/443."
        done < <(find /etc/nginx/sites-enabled -mindepth 1 -maxdepth 1 -print0 2>/dev/null || true)
    fi
    if [[ -d /etc/nginx/conf.d ]]; then
        while IFS= read -r -d '' conf; do
            [[ "$(basename "$conf")" == "kit.conf" ]] \
                || fail "Найдена сторонняя конфигурация Nginx. Скрипт не будет перезаписывать её."
        done < <(find /etc/nginx/conf.d -mindepth 1 -maxdepth 1 -type f -name '*.conf' -print0 2>/dev/null || true)
    fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR" || fail "Не удалось создать venv. Проверьте пакет python3-venv."
fi
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"

if [[ "$KEEP_CONFIG" -eq 0 ]]; then
    PROJECT_DIR="$PROJECT_DIR" CONFIG_FILE="$CONFIG_FILE" \
    BUTOVSKY_BOT_TOKEN="$BOT_TOKEN" BUTOVSKY_ADMIN_ID="$ADMIN_ID" \
    BUTOVSKY_BOT_USERNAME="$BOT_USERNAME" BUTOVSKY_WEB_DOMAIN="$WEB_DOMAIN" \
        "$VENV_DIR/bin/python" - <<'PY'
import os
from pathlib import Path

project = Path(os.environ["PROJECT_DIR"])
template = project / "config.py.example"
config_path = Path(os.environ["CONFIG_FILE"])
text = template.read_text(encoding="utf-8")
text += (
    "\n\n# Values configured by start.sh; keep this file private (mode 600).\n"
    f"BOT_TOKEN = {os.environ['BUTOVSKY_BOT_TOKEN']!r}\n"
    f"BOT_USERNAME = {os.environ['BUTOVSKY_BOT_USERNAME']!r}\n"
    f"ADMIN_IDS = [{int(os.environ['BUTOVSKY_ADMIN_ID'])}]\n"
    "WEB_HOST = '127.0.0.1'\n"
    "WEB_PORT = 8080\n"
    f"WEB_BASE_URL = {'https://' + os.environ['BUTOVSKY_WEB_DOMAIN']!r}\n"
    "WEB_COOKIE_SECURE = True\n"
)
config_path.write_text(text, encoding="utf-8")
PY
    chmod 600 "$CONFIG_FILE"
    unset BOT_TOKEN ADMIN_ID BOT_USERNAME
else
    "$VENV_DIR/bin/python" - <<'PY' || fail "Текущий config.py не содержит рабочий токен, ADMIN_IDS или заданный WEB_BASE_URL."
import config
from urllib.parse import urlparse

assert getattr(config, "BOT_TOKEN", "") and "ВАШ_ТОКЕН" not in str(config.BOT_TOKEN)
assert getattr(config, "ADMIN_IDS", [])
assert urlparse(str(getattr(config, "WEB_BASE_URL", ""))).hostname
PY
    CONFIG_DOMAIN="$("$VENV_DIR/bin/python" - <<'PY'
import config
from urllib.parse import urlparse
print(urlparse(str(getattr(config, "WEB_BASE_URL", ""))).hostname or "")
PY
)"
    [[ "${CONFIG_DOMAIN,,}" == "$WEB_DOMAIN" ]] \
        || fail "WEB_BASE_URL в сохранённом config.py не совпадает с ${WEB_DOMAIN}. Перенастройте config.py и запустите снова."
    chmod 600 "$CONFIG_FILE"
fi

if [[ "$PANEL_ALREADY_INSTALLED" -eq 0 ]]; then
    KIT_TMP="$(mktemp /tmp/butovsky-3x-ui.XXXXXX)"
    PATCHED_KIT_TMP="$(mktemp /tmp/butovsky-3x-ui-patched.XXXXXX)"
    curl -fsSL --retry 3 "$KIT_URL" -o "$KIT_TMP" \
        || fail "Не удалось загрузить 3X-UI KIT v1.2.1."
    ACTUAL_KIT_SHA256="$(sha256sum "$KIT_TMP" | awk '{print $1}')"
    [[ "$ACTUAL_KIT_SHA256" == "$KIT_SHA256" ]] \
        || fail "SHA-256 установщика 3X-UI KIT не совпал с проверенной версией; запуск отменён."

    PANEL_WEB_PATH="$PANEL_WEB_PATH" \
        python3 - "$KIT_TMP" "$PATCHED_KIT_TMP" <<'PY'
import os
import sys
from pathlib import Path

source_path, output_path = map(Path, sys.argv[1:3])
text = source_path.read_text(encoding="utf-8")

def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"Ожидался один участок установщика, найдено: {count}")
    text = text.replace(old, new, 1)

# Avoid the upstream optional scan of neighboring IP addresses for SNI candidates.
replace_once('SNI2=""; SNI3=""', 'SNI2="www.amazon.com"; SNI3="www.samsung.com"')
# With -y and --panel-on ip, skip the optional panel location prompt.
replace_once(
    '[[ $PANEL_ON == domain ]] && return 0',
    '[[ $PANEL_ON == domain || $yes == yes ]] && return 0',
)

panel_path = os.environ.get("PANEL_WEB_PATH", "")
if panel_path:
    if not panel_path.isascii() or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in panel_path):
        raise SystemExit("Недопустимые символы в пути панели.")
    replace_once("panel_path=$(rand_str 18)", f"panel_path='{panel_path}'")

output_path.write_text(text, encoding="utf-8")
output_path.chmod(0o700)
PY

    printf '%b\n' "${CYAN}Устанавливаю 3X-UI KIT v1.2.1 и создаю подключения...${NC}"
    printf '%b\n' "${YELLOW}Установщик настраивает UFW/Nginx/systemd; домен должен указывать на ${SERVER_IP}, порты 80/443 должны быть доступны.${NC}"
    run_as_root bash "$PATCHED_KIT_TMP" -y \
        --host "$SERVER_IP" \
        --domain "$WEB_DOMAIN" \
        --panel-on ip
fi

command -v nginx >/dev/null 2>&1 || fail "3X-UI не установила Nginx."
[[ -f /etc/x-ui/install-result.env ]] || fail "Не найден файл результата 3X-UI; проверьте журнал установки панели."

SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"
"$VENV_DIR/bin/python" - <<'PY'
from database.migrations import run_migrations
run_migrations()
PY

PANEL_ENV_TMP="$(mktemp /tmp/butovsky-panel.XXXXXX)"
run_as_root bash -c '
set -e
source /etc/x-ui/install-result.env
: "${XUI_PANEL_PORT:?missing panel port}"
: "${XUI_WEB_BASE_PATH:?missing panel path}"
: "${XUI_API_TOKEN:?missing API token}"
printf "XUI_PANEL_PORT=%s\nXUI_WEB_BASE_PATH=%s\nXUI_API_TOKEN=%s\n" \
    "$XUI_PANEL_PORT" "$XUI_WEB_BASE_PATH" "$XUI_API_TOKEN" > "$1"
chown "$2:$3" "$1"
chmod 600 "$1"
' _ "$PANEL_ENV_TMP" "$SERVICE_USER" "$SERVICE_GROUP"
"$VENV_DIR/bin/python" "$PROJECT_DIR/deploy/register_3xui.py" "$PANEL_ENV_TMP" \
    || fail "Не удалось проверить и зарегистрировать 3X-UI в базе бота."
run_as_root rm -f -- "$PANEL_ENV_TMP"
PANEL_ENV_TMP=""

run_as_root install -o root -g root -m 755 \
    "$PROJECT_DIR/deploy/kit_nginx_site.py" \
    /usr/local/sbin/butovskyvpn-kit-nginx-site
run_as_root install -d -o root -g root -m 755 \
    /etc/systemd/system/nginx.service.d
NGINX_DROPIN_TMP="$(mktemp)"
cat > "$NGINX_DROPIN_TMP" <<EOF
[Service]
ExecStartPre=/usr/local/sbin/butovskyvpn-kit-nginx-site --domain $WEB_DOMAIN --upstream 8080
EOF
run_as_root install -o root -g root -m 644 "$NGINX_DROPIN_TMP" \
    /etc/systemd/system/nginx.service.d/50-butovskyvpn-site.conf
run_as_root systemctl daemon-reload

SERVICE_TMP="$(mktemp)"
cat > "$SERVICE_TMP" <<EOF
[Unit]
Description=ButovskyVPN website and Telegram bot
Wants=network-online.target
After=network-online.target x-ui.service nginx.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/python $PROJECT_DIR/run_all.py
Environment=PYTHONUNBUFFERED=1
Environment=WEB_HOST=127.0.0.1
Environment=WEB_PORT=8080
UMask=0077
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
run_as_root install -o root -g root -m 644 "$SERVICE_TMP" \
    "/etc/systemd/system/$SERVICE_NAME"
run_as_root systemctl daemon-reload
run_as_root systemctl enable "$SERVICE_NAME" >/dev/null
run_as_root systemctl restart "$SERVICE_NAME"
sleep 3
if ! run_as_root systemctl is-active --quiet "$SERVICE_NAME"; then
    printf '%b\n' "${RED}[✗]${NC} Сайт и бот не запустились."
    run_as_root journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true
    exit 1
fi

run_as_root systemctl restart nginx
if ! run_as_root systemctl is-active --quiet nginx; then
    printf '%b\n' "${RED}[✗]${NC} Nginx не запустился после настройки сайта."
    run_as_root journalctl -u nginx -n 50 --no-pager || true
    exit 1
fi

if ! curl -kfsS --connect-timeout 4 --max-time 12 \
    --resolve "$WEB_DOMAIN:443:127.0.0.1" "https://$WEB_DOMAIN/" -o /dev/null; then
    printf '%b\n' "${RED}[✗]${NC} Внутренняя проверка сайта через HTTPS не прошла."
    run_as_root journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
    run_as_root journalctl -u nginx -n 30 --no-pager || true
    exit 1
fi

PANEL_PATH_RUNTIME="$(run_as_root bash -c 'source /etc/x-ui/install-result.env; printf "%s" "$XUI_WEB_BASE_PATH"')"
PANEL_PATH_RUNTIME="${PANEL_PATH_RUNTIME#/}"
PANEL_PATH_RUNTIME="${PANEL_PATH_RUNTIME%/}"

printf '\n%b\n' "${GREEN}[✓] Установка завершена.${NC}"
printf 'Сайт:        https://%s\n' "$WEB_DOMAIN"
printf 'Панель 3x-ui: https://%s/%s/\n' "$SERVER_IP" "$PANEL_PATH_RUNTIME"
printf 'Бот и сайт:   systemctl status %s\n' "$SERVICE_NAME"
printf 'Логи бота:    journalctl -u %s -f\n' "$SERVICE_NAME"
printf 'Перезапуск:   systemctl restart %s\n' "$SERVICE_NAME"
printf '%b\n' "${YELLOW}Если сертификат панели для IP самоподписанный, браузер покажет предупреждение; сертификат сайта на домене выпускается отдельно.${NC}"
printf '%b\n' "${YELLOW}QR-код подключения выводит установщик 3X-UI KIT во время установки.${NC}"
