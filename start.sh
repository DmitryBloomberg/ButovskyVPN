#!/usr/bin/env bash
set -Eeuo pipefail

# Первый запуск ButovskyVPN из уже клонированного репозитория:
#   git clone https://github.com/DmitryBloomberg/ButovskyVPN.git
#   cd ButovskyVPN
#   bash start.sh
#
# Скрипт устанавливает проект как systemd-сервис. Поэтому бот:
# - запускается в фоне, а не занимает текущий терминал;
# - стартует автоматически после перезагрузки сервера;
# - перезапускается systemd при аварийном завершении.

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
CONFIG_FILE="$PROJECT_DIR/config.py"
SERVICE_NAME="butovsky-vpn.service"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"
WEB_DOMAIN=""
LETSENCRYPT_EMAIL=""
BOT_USERNAME=""

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
        command -v sudo >/dev/null 2>&1 || fail "Нужны права root или установленная команда sudo."
        sudo "$@"
    fi
}

printf '%b\n' "${CYAN}========================================${NC}"
printf '%b\n' "${CYAN}       ButovskyVPN — установка${NC}"
printf '%b\n\n' "${CYAN}========================================${NC}"

command -v python3 >/dev/null 2>&1 || fail "Не найден Python 3. Установите python3 и python3-venv."
command -v systemctl >/dev/null 2>&1 || fail "На сервере не найден systemd/systemctl — автоматический запуск 24/7 невозможен."

if [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    fail "Для создания systemd-сервиса нужны права root или установленная команда sudo."
fi

while true; do
    read -r -p "Токен Telegram-бота (от @BotFather): " BOT_TOKEN
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

read -r -p "Имя бота без @ (для Stars и привязки аккаунта, Enter — пропустить): " BOT_USERNAME
read -r -p "Домен сайта (Enter — настроить HTTPS позже): " WEB_DOMAIN
if [[ -n "$WEB_DOMAIN" ]]; then
    [[ "$WEB_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || fail "Укажите домен без схемы https:// и без пути."
    read -r -p "Email для Let's Encrypt: " LETSENCRYPT_EMAIL
    [[ "$LETSENCRYPT_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || fail "Укажите корректный email."
fi

if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR" || fail "Не удалось создать виртуальное окружение. Установите python3-venv."
fi

"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"

export BUTOVSKY_BOT_TOKEN="$BOT_TOKEN"
export BUTOVSKY_ADMIN_ID="$ADMIN_ID"
export BUTOVSKY_BOT_USERNAME="$BOT_USERNAME"
export BUTOVSKY_WEB_DOMAIN="$WEB_DOMAIN"
PROJECT_DIR="$PROJECT_DIR" CONFIG_FILE="$CONFIG_FILE" \
    "$VENV_DIR/bin/python" - <<'PY'
import os
from pathlib import Path

template = Path(os.environ["PROJECT_DIR"]) / "config.py.example"
config_path = Path(os.environ["CONFIG_FILE"])
text = template.read_text(encoding="utf-8")
text += (
    "\n\n# Values configured by start.sh; keep config.py private (mode 600).\n"
    f"BOT_TOKEN = {os.environ['BUTOVSKY_BOT_TOKEN']!r}\n"
    f"BOT_USERNAME = {os.environ['BUTOVSKY_BOT_USERNAME']!r}\n"
)
text = text.replace(
    "    12345678,  # Замените на ваш реальный ID",
    f"    {int(os.environ['BUTOVSKY_ADMIN_ID'])},",
)
domain = os.environ.get("BUTOVSKY_WEB_DOMAIN", "").strip()
if domain:
    text += (
        "\nWEB_HOST = '127.0.0.1'\n"
        "WEB_PORT = 8080\n"
        f"WEB_BASE_URL = {'https://' + domain!r}\n"
        "WEB_COOKIE_SECURE = True\n"
    )
config_path.write_text(text, encoding="utf-8")
PY

chmod 600 "$CONFIG_FILE"
printf '%b\n' "${GREEN}[✓]${NC} Конфигурация сохранена в config.py"

SERVICE_TMP="$(mktemp)"
trap 'rm -f "$SERVICE_TMP"' EXIT
SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"

cat > "$SERVICE_TMP" <<EOF
[Unit]
Description=ButovskyVPN Web Storefront and Telegram Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/python $PROJECT_DIR/run_all.py
Environment=PYTHONUNBUFFERED=1
Environment=WEB_HOST=127.0.0.1
Environment=WEB_PORT=8080
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

run_as_root install -m 644 "$SERVICE_TMP" "$SERVICE_FILE"
run_as_root systemctl daemon-reload
run_as_root systemctl enable "$SERVICE_NAME" >/dev/null
run_as_root systemctl restart "$SERVICE_NAME"
sleep 2

if ! run_as_root systemctl is-active --quiet "$SERVICE_NAME"; then
    printf '%b\n' "${RED}[✗]${NC} Бот не запустился как systemd-сервис."
    printf '%b\n' "${YELLOW}Последние логи:${NC}"
    run_as_root journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true
    exit 1
fi

if [[ -n "$WEB_DOMAIN" ]]; then
    if ! command -v nginx >/dev/null 2>&1 || ! command -v certbot >/dev/null 2>&1; then
        run_as_root apt-get update
        run_as_root apt-get install -y nginx certbot python3-certbot-nginx
    fi
    run_as_root install -d -m 755 /etc/nginx/sites-available /etc/nginx/sites-enabled
    run_as_root sed "s/__DOMAIN__/$WEB_DOMAIN/g" \
        "$PROJECT_DIR/deploy/nginx-http.conf.example" \
        > /tmp/butovsky-vpn-nginx.conf
    run_as_root install -m 644 /tmp/butovsky-vpn-nginx.conf \
        /etc/nginx/sites-available/butovsky-vpn
    rm -f /tmp/butovsky-vpn-nginx.conf
    run_as_root ln -sfn /etc/nginx/sites-available/butovsky-vpn \
        /etc/nginx/sites-enabled/butovsky-vpn
    run_as_root nginx -t
    run_as_root systemctl enable nginx >/dev/null
    run_as_root systemctl restart nginx
    run_as_root certbot --nginx --non-interactive --agree-tos \
        --redirect --email "$LETSENCRYPT_EMAIL" -d "$WEB_DOMAIN"
fi

printf '\n%b\n' "${GREEN}[✓] Сайт и бот запущены в фоне через systemd.${NC}"
printf '%b\n' "Статус:  systemctl status $SERVICE_NAME"
printf '%b\n' "Логи:    journalctl -u $SERVICE_NAME -f"
printf '%b\n' "Остановить: systemctl stop $SERVICE_NAME"
printf '%b\n' "Перезапустить: systemctl restart $SERVICE_NAME"
if [[ -n "$WEB_DOMAIN" ]]; then
    printf '%b\n' "Сайт:    https://$WEB_DOMAIN"
else
    printf '%b\n' "Сайт:    http://127.0.0.1:8080 (для HTTPS настройте Nginx и сертификат)."
fi
printf '%b\n' "Сервер 3x-ui добавляется в боте: админ-панель → Сервера → Добавить сервер."