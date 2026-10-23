#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================================
#  ButovskyVPN — полная установка сервера (бот + сайт + 3x-ui + Nginx + SSL)
#
#  Запуск:   bash start_fill.sh
#
#  Скрипт запросит:
#    • Токен Telegram-бота и ID администратора
#    • Домен сайта (поддомен, например butovskyvpn.duckdns.org)
#    • Логин/пароль для панели 3x-ui
#
#  Что делает:
#    1. Устанавливает системные пакеты (python3, venv, nginx, certbot, git)
#    2. Устанавливает панель 3x-ui (MHSanaei/3x-ui) на порт 2053 (HTTPS)
#       -> по ПРЯМОМУ IP вы попадаете в панель: https://IP:2053/webpath
#    3. Клонирует/обновляет бот ButovskyVPN и сайт Butovsky
#    4. Генерирует конфигурацию сайта (общая SQLite база с ботом)
#    5. Настраивает Nginx: домен -> сайт (127.0.0.1:8090), IP -> 3x-ui
#    6. Получает Let's Encrypt сертификат для домена
#    7. Регистрирует systemd-сервисы butovsky-bot и butovsky-web (автозапуск 24/7)
# ============================================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

info()  { printf '%b\n' "${CYAN}[i]${NC} $1"; }
ok()    { printf '%b\n' "${GREEN}[✓]${NC} $1"; }
warn()  { printf '%b\n' "${YELLOW}[!]${NC} $1"; }
fail()  { printf '%b\n' "${RED}[✗]${NC} $1" >&2; exit 1; }

run_as_root() {
    if [[ "$(id -u)" -eq 0 ]]; then "$@"; else
        command -v sudo >/dev/null 2>&1 || fail "Нужны права root или sudo."
        sudo "$@"
    fi
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

printf '%b\n' "${CYAN}============================================================${NC}"
printf '%b\n' "${CYAN}   ButovskyVPN: бот + сайт продаж + панель 3x-ui — установка ${NC}"
printf '%b\n' "${CYAN}============================================================${NC}\n"

[[ "$(id -u)" -eq 0 ]] || command -v sudo >/dev/null 2>&1 || fail "Запустите от root или установите sudo."

command -v systemctl >/dev/null 2>&1 || fail "Не найден systemd — автоматический запуск 24/7 невозможен."

# ----------------------------- Вопросы -------------------------------------
while true; do
    read -r -p "Токен Telegram-бота (от @BotFather): " BOT_TOKEN
    [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] && break
    warn "Проверьте формат токена (123456:ABC-DEF...)."
done

while true; do
    read -r -p "Telegram ID администратора (цифры): " ADMIN_ID
    [[ "$ADMIN_ID" =~ ^[0-9]+$ ]] && break
    warn "ID должен состоять только из цифр."
done

read -r -p "Username бота без @ [ButovskyVPN_bot]: " BOT_USERNAME
BOT_USERNAME="${BOT_USERNAME:-ButovskyVPN_bot}"

while true; do
    read -r -p "Домен сайта (например butovskyvpn.duckdns.org): " DOMAIN
    DOMAIN="$(echo "$DOMAIN" | tr '[:upper:]' '[:lower:]' | sed 's|^https\?://||; s|/$||')"
    [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] && break
    warn "Некорректное имя домена."
done

read -r -p "Email для Let's Encrypt сертификата: " SSL_EMAIL
while [[ ! "$SSL_EMAIL" =~ ^[^@]+@[^@]+\.[^@]+$ ]]; do
    warn "Некорректный email."
    read -r -p "Email для Let's Encrypt сертификата: " SSL_EMAIL
done

read -r -p "Логин панели 3x-ui [admin]: " PANEL_LOGIN
PANEL_LOGIN="${PANEL_LOGIN:-admin}"
while true; do
    read -r -s -p "Пароль панели 3x-ui (не отображается): " PANEL_PASSWORD; echo
    read -r -s -p "Повторите пароль панели 3x-ui: " PANEL_PASSWORD2; echo
    [[ ${#PANEL_PASSWORD} -ge 6 ]] || { warn "Минимум 6 символов."; continue; }
    [[ "$PANEL_PASSWORD" == "$PANEL_PASSWORD2" ]] && break
    warn "Пароли не совпадают."
done
# Экранируем кавычки/обратный слэш — пароль будет записан в config.py как Python-строка
esc_py() {
    local s="$1"
    s="${s//\\/\\\\}"   # \  -> \\
    s="${s//\"/\\\"}"   # "  -> \"
    printf '%s' "$s"
}
PANEL_PASSWORD_ESCAPED="$(esc_py "$PANEL_PASSWORD")"
PANEL_PATH="$(tr -dc 'a-z0-9' < /dev/urandom | head -c 12)"  # секретный путь входа в панель

WEB_PORT=8090
PANEL_PORT=2053
BOT_DIR="/opt/butovsky-bot"
WEB_DIR="/opt/butovsky-web"

echo
info "Параметры установки:"
printf '  Сайт (домен):   https://%s\n' "$DOMAIN"
printf '  Панель 3x-ui:   https://ВАШ_IP:%s/%s\n' "$PANEL_PORT" "$PANEL_PATH"
printf '  Порт сайта:     127.0.0.1:%s (только через Nginx)\n' "$WEB_PORT"
echo
read -r -p "Начать установку? [Y/n]: " ANSWER
[[ "${ANSWER,,}" == "n" ]] && fail "Установка прервана пользователем."

# --------------------------- Системные пакеты ------------------------------
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
    info "Обновление списков пакетов и установка зависимостей..."
    run_as_root apt-get update -qq
    run_as_root apt-get install -y -qq curl wget git python3 python3-venv python3-pip \
        nginx openssl tar gzip qrencode cron sqlite3 >/dev/null
    ok "Системные пакеты установлены."
elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    PM=$(command -v dnf || command -v yum)
    info "Установка зависимостей через $PM ..."
    run_as_root "$PM" install -y -q curl wget git python3 nginx openssl tar gzip sqlite >/dev/null
    ok "Системные пакеты установлены."
else
    fail "Поддерживаются только apt (Debian/Ubuntu) и dnf/yum (RHEL/CentOS)."
fi

# ------------------------- Проверка IP домена (duckdns) --------------------
PUBLIC_IP="$(curl -fsS --max-time 10 https://ifconfig.me || curl -fsS --max-time 10 https://api.ipify.org || true)"
[[ -n "$PUBLIC_IP" ]] || fail "Не удалось определить публичный IP сервера."
info "Публичный IP сервера: $PUBLIC_IP"

RESOLVED_IP="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -n1 || true)"
if [[ -z "$RESOLVED_IP" ]]; then
    warn "Домен $DOMAIN пока НЕ указывает на этот сервер (A-запись не найдена)."
    warn "Для duckdns выполните обновление A-записи на ваш IP: $PUBLIC_IP"
    warn "Пример:  curl \"https://www.duckdns.org/update?domains=<ваш_домен_без_зоны>&token=<токен_duckdns>&ip\""
    warn "Без корректной DNS-записи Let's Encrypt не выдаст сертификат."
    read -r -p "Продолжить установку всё равно? [Y/n]: " CONT
    [[ "${CONT,,}" == "n" ]] && fail "Остановлено. Настройте DNS и запустите скрипт заново."
elif [[ "$RESOLVED_IP" != "$PUBLIC_IP" ]]; then
    warn "Домен $DOMAIN указывает на $RESOLVED_IP, а сервер — на $PUBLIC_IP."
    warn "Проверьте DNS — сертификат может не выдаться."
else
    ok "Домен $DOMAIN корректно указывает на этот сервер."
fi

# --------------------------- Установка 3x-ui -------------------------------
XUI_FRESH_INSTALL="no"
if systemctl is-active --quiet x-ui 2>/dev/null || [[ -d /usr/local/x-ui ]]; then
    ok "Панель 3x-ui уже установлена — пропускаю установку."
    EXIST_USER=$(/usr/local/x-ui/x-ui setting -show 2>/dev/null | grep -i '^username:' | awk '{print $2}' || true)
    [[ -n "$EXIST_USER" ]] && warn "Внимание: используется существующая панель, логин: $EXIST_USER (в config сайта будут записаны введённые вами данные — при несовпадении обновите их вручную)."
else
    info "Установка панели 3x-ui (MHSanaei/3x-ui)..."
    bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) <<< "1
${PANEL_LOGIN}
${PANEL_PASSWORD}
${PANEL_PORT}
/${PANEL_PATH}
" || fail "Не удалось установить 3x-ui. Запустите вручную: bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)"
    XUI_FRESH_INSTALL="yes"
    ok "3x-ui установлена."
fi

# Фиксируем актуальные настройки панели (на случай если установка была пропущена).
# Пароль/логин меняем только для свежеустановленной панели (иначе собьём существующий вход).
if [[ "$XUI_FRESH_INSTALL" == "yes" ]]; then
    run_as_root /usr/local/x-ui/x-ui setting -username "$PANEL_LOGIN" >/dev/null 2>&1 || true
    run_as_root /usr/local/x-ui/x-ui setting -password "$PANEL_PASSWORD" >/dev/null 2>&1 || true
fi
run_as_root /usr/local/x-ui/x-ui setting -port "$PANEL_PORT" >/dev/null 2>&1 || true
run_as_root /usr/local/x-ui/x-ui setting -webBasePath "/${PANEL_PATH}/" >/dev/null 2>&1 || true
run_as_root systemctl restart x-ui >/dev/null 2>&1 || true
sleep 3
run_as_root systemctl enable x-ui >/dev/null 2>&1 || true

# ----------------------- Клонирование репозиториев --------------------------
clone_or_update() {
    local repo="$1" dir="$2"
    if [[ -d "$dir/.git" ]]; then
        info "Обновление $dir ..."
        run_as_root git -C "$dir" remote set-url origin "$repo"
        if run_as_root git -C "$dir" fetch --depth 1 origin 2>/dev/null; then
            run_as_root git -C "$dir" reset --hard FETCH_HEAD 2>/dev/null || \
                run_as_root git -C "$dir" pull --ff-only || warn "Не удалось обновить $dir — использую текущую версию."
        fi
    else
        info "Клонирование $repo -> $dir ..."
        run_as_root git clone --depth 1 "$repo" "$dir" || fail "git clone не удался для $repo"
    fi
}

# Если скрипт лежит внутри уже клонированного репозитория бота — используем его
if [[ -f "$SCRIPT_DIR/main.py" && -d "$SCRIPT_DIR/bot" ]]; then
    BOT_SRC="$SCRIPT_DIR"
    info "Репозиторий бота уже здесь: $BOT_SRC"
    if [[ "$BOT_SRC" != "$BOT_DIR" ]]; then
        clone_or_update "https://github.com/DmitryBloomberg/ButovskyVPN.git" "$BOT_DIR"
    fi
else
    clone_or_update "https://github.com/DmitryBloomberg/ButovskyVPN.git" "$BOT_DIR"
fi
clone_or_update "https://github.com/DmitryBloomberg/Butovsky.git" "$WEB_DIR"

# Живой конфиг сайта никогда не должен затирааться/клонироваться из примера —
# он генерируется ниже скриптом заново только при первом установочном прогоне.
if [[ -f "$WEB_DIR/config.py" ]]; then
    KEEP_WEB_CONFIG="yes"
    ok "Конфигурация сайта уже существует — буду бережно её обновлять (секреты сохранятся)."
else
    KEEP_WEB_CONFIG="no"
fi

# --------------------------- Виртуальные окружения --------------------------
info "Создание виртуальных окружений и установка Python-зависимостей..."
run_as_root python3 -m venv "$BOT_DIR/venv"
run_as_root python3 -m venv "$WEB_DIR/venv"
run_as_root "$BOT_DIR/venv/bin/pip" install --disable-pip-version-check -q --upgrade pip
run_as_root "$BOT_DIR/venv/bin/pip" install --disable-pip-version-check -q -r "$BOT_DIR/requirements.txt"
run_as_root "$WEB_DIR/venv/bin/pip" install --disable-pip-version-check -q --upgrade pip
run_as_root "$WEB_DIR/venv/bin/pip" install --disable-pip-version-check -q -r "$WEB_DIR/requirements.txt"
ok "Зависимости установлены."

# --------------------------- Конфигурация бота ------------------------------
SECRET_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SYNC_SECRET="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"

info "Настройка бота (config.py)..."
if [[ -f "$BOT_DIR/config.py" ]]; then
    ok "config.py бота уже существует — оставляю без изменений (токен будет перезаписан ниже только если это пример)."
fi
if [[ ! -f "$BOT_DIR/config.py" ]] || grep -q 'ВАШ_ТОКЕН_БОТА' "$BOT_DIR/config.py" 2>/dev/null; then
    run_as_root env \
        BUTOVSKY_BOT_TOKEN="$BOT_TOKEN" BUTOVSKY_ADMIN_ID="$ADMIN_ID" \
        CONFIG_TEMPLATE="$BOT_DIR/config.py.example" CONFIG_TARGET="$BOT_DIR/config.py" \
        "$BOT_DIR/venv/bin/python" - <<'PY'
import os
from pathlib import Path
template = Path(os.environ["CONFIG_TEMPLATE"])
target = Path(os.environ["CONFIG_TARGET"])
text = template.read_text(encoding="utf-8")
text = text.replace('BOT_TOKEN = "ВАШ_ТОКЕН_БОТА"', f'BOT_TOKEN = {os.environ["BUTOVSKY_BOT_TOKEN"]!r}')
text = text.replace("    12345678,  # Замените на ваш реальный ID", f'    {int(os.environ["BUTOVSKY_ADMIN_ID"])},')
target.write_text(text, encoding="utf-8")
PY
    ok "config.py бота создан."
else
    ok "config.py бота уже настроен."
fi
run_as_root chmod 600 "$BOT_DIR/config.py"

# --------------------------- Конфигурация сайта -----------------------------
info "Генерация конфигурации сайта ($WEB_DIR/config.py)..."
if [[ "$KEEP_WEB_CONFIG" == "yes" ]]; then
    # Сохраняем уже сгенерированные секреты и настройки, чтобы не рвать активные сессии
    OLD_SECRET_KEY="$(grep -oP '(?<=^SECRET_KEY = ")[^"]+' "$WEB_DIR/config.py" || true)"
    OLD_SYNC_SECRET="$(grep -oP '(?<=^BOT_SYNC_SECRET = ")[^"]+' "$WEB_DIR/config.py" || true)"
    OLD_PANEL_PATH="$(grep -oP '(?<=^PANEL_URL = "https://127.0.0.1:2053/)[^"]+' "$WEB_DIR/config.py" || true)"
    [[ -n "$OLD_SECRET_KEY" ]] && SECRET_KEY="$OLD_SECRET_KEY"
    [[ -n "$OLD_SYNC_SECRET" ]] && SYNC_SECRET="$OLD_SYNC_SECRET"
    [[ -n "$OLD_PANEL_PATH" ]] && PANEL_PATH="$OLD_PANEL_PATH"
fi
run_as_root tee "$WEB_DIR/config.py" >/dev/null <<EOF
# -*- coding: utf-8 -*-
# Сгенерировано start_fill.sh $(date '+%F %T'). Не коммитьте в git!
import os

BOT_DB_PATH = os.getenv("BOT_DB_PATH", "$BOT_DIR/database/vpn_bot.db")
SECRET_KEY = "$SECRET_KEY"
BOT_TOKEN = "$BOT_TOKEN"
BOT_USERNAME = "$BOT_USERNAME"
SITE_URL = "https://$DOMAIN"
WEB_PORT = $WEB_PORT

# Platega (app.platega.io) — если пусто, берутся из настроек бота (админ-панель)
PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID", "")
PLATEGA_SECRET = os.getenv("PLATEGA_SECRET", "")

# 3x-ui панель (доступ с localhost через Nginx-проксированный HTTPS)
PANEL_URL = "https://127.0.0.1:$PANEL_PORT/$PANEL_PATH"
PANEL_LOGIN = "$PANEL_LOGIN"
PANEL_PASSWORD = "$PANEL_PASSWORD_ESCAPED"

BOT_SYNC_SECRET = "$SYNC_SECRET"
EOF
run_as_root chmod 600 "$WEB_DIR/config.py"
ok "Конфигурация сайта создана."

mkdir -p "$WEB_DIR/database"
run_as_root touch "$BOT_DIR/database/vpn_bot.db"

# --------------------------- systemd-сервисы --------------------------------
info "Регистрация systemd-сервисов..."
run_as_root tee /etc/systemd/system/butovsky-bot.service >/dev/null <<EOF
[Unit]
Description=ButovskyVPN Telegram Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python $BOT_DIR/main.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

run_as_root tee /etc/systemd/system/butovsky-web.service >/dev/null <<EOF
[Unit]
Description=ButovskyVPN Web Store (FastAPI)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$WEB_DIR
ExecStart=$WEB_DIR/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $WEB_PORT --proxy-headers --forwarded-allow-ips 127.0.0.1
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

run_as_root systemctl daemon-reload
run_as_root systemctl enable butovsky-bot butovsky-web >/dev/null
run_as_root systemctl restart butovsky-bot butovsky-web
sleep 4
run_as_root systemctl is-active --quiet butovsky-bot || { run_as_root journalctl -u butovsky-bot -n 30 --no-pager; fail "Бот не запустился."; }
run_as_root systemctl is-active --quiet butovsky-web || { run_as_root journalctl -u butovsky-web -n 30 --no-pager; fail "Сайт не запустился."; }
ok "Сервисы butovsky-bot и butovsky-web активны."

# ----------------------------- Nginx ----------------------------------------
info "Настройка Nginx..."
NGINX_CONF=/etc/nginx/sites-available/butovskyvpn
run_as_root tee "$NGINX_CONF" >/dev/null <<EOF
# ============================================
# ButovskyVPN: домен -> сайт, прямой IP -> 3x-ui
# ============================================

# Дефолтный сервер по IP: редирект HTTP -> HTTPS панели 3x-ui
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    # ACME-challenge нужен и для домена — отдаём общую директорию
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 302 https://\$server_addr:$PANEL_PORT/;
    }
}

# Основной сайт по домену
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:$WEB_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
EOF

run_as_root ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/butovskyvpn
[[ -d /etc/nginx/conf.d ]] && run_as_root rm -f /etc/nginx/conf.d/default.conf.bak
run_as_root mkdir -p /var/www/html

# --------------------------- Let's Encrypt ----------------------------------
if [[ ! -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
    info "Получение SSL-сертификата Let's Encrypt для $DOMAIN ..."
    if command -v certbot >/dev/null 2>&1; then
        CERTBOT="certbot"
    elif run_as_root apt-get install -y -qq certbot python3-certbot-nginx >/dev/null 2>&1; then
        CERTBOT="certbot"
    else
        CERTBOT=""
    fi
    if [[ -n "$CERTBOT" ]]; then
        # Сначала временный self-signed, чтобы nginx стартовал
        run_as_root openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
            -keyout /etc/letsencrypt/ssl_tmp.key -out /etc/letsencrypt/ssl_tmp.crt \
            -subj "/CN=$DOMAIN" >/dev/null 2>&1 || true
        run_as_root mkdir -p "/etc/letsencrypt/live/$DOMAIN"
        run_as_root cp -f /etc/letsencrypt/ssl_tmp.key "/etc/letsencrypt/live/$DOMAIN/privkey.pem" 2>/dev/null || true
        run_as_root cp -f /etc/letsencrypt/ssl_tmp.crt "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" 2>/dev/null || true
        run_as_root nginx -t && run_as_root systemctl reload nginx || warn "nginx test failed"
        if run_as_root "$CERTBOT" --certonly --webroot -w /var/www/html \
                --non-interactive --agree-tos -m "$SSL_EMAIL" --domain "$DOMAIN"; then
            ok "Сертификат Let's Encrypt получен."
            run_as_root "$CERTBOT" renew --dry-run >/dev/null 2>&1 && ok "Автопродление сертификата проверено." || true
        else
            warn "Let's Encrypt не выдал сертификат (проверьте DNS). Используется временный самоподписанный."
        fi
    else
        warn "certbot не установлен — используйте самоподписанный сертификат или установите certbot вручную."
        run_as_root mkdir -p "/etc/letsencrypt/live/$DOMAIN"
        run_as_root openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
            -keyout "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
            -out "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" \
            -subj "/CN=$DOMAIN" >/dev/null 2>&1
    fi
else
    ok "Сертификат для $DOMAIN уже существует — пропускаю выпуск."
fi

run_as_root nginx -t || fail "Конфигурация Nginx некорректна."
run_as_root systemctl reload nginx
ok "Nginx настроен и перезапущен."

# --------------------------- Итог --------------------------------------------
SERVER_MAIN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/{print $7; exit}' || hostname -I | awk '{print $1}')"
cat <<EOF

${GREEN}============================================================${NC}
${GREEN}   Установка завершена успешно!${NC}
${GREEN}============================================================${NC}

  ${CYAN}Сайт продаж VPN:${NC}
      https://${DOMAIN}
      (регистрация, тарифы, оплата Platega / Telegram Stars,
       ключи дублируются с ботом при привязке Telegram ID)

  ${CYAN}Панель 3x-ui (доступ по IP):${NC}
      https://${SERVER_MAIN_IP}:${PANEL_PORT}/${PANEL_PATH}
      логин:  ${PANEL_LOGIN}
      пароль: ${PANEL_PASSWORD}
      ${YELLOW}(сохраните эти данные!)${NC}

  ${CYAN}Управление сервисами:${NC}
      Бот:   systemctl status butovsky-bot
      Сайт:  systemctl status butovsky-web
      Логи:  journalctl -u butovsky-web -f
             journalctl -u butovsky-bot -f

  ${CYAN}Дальнейшие шаги:${NC}
      1. Откройте сайт, зарегистрируйтесь и привяжите Telegram
         (кнопка «Привязать Telegram» отправит deep-link в бота).
      2. В админке бота настройте сервер 3x-ui (если ещё не добавлен):
         адрес панели укажите как https://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}
      3. Для оплаты Platega укажите merchant_id/secret в админке бота
         или в ${WEB_DIR}/config.py.
      4. Синхронизация ключей бот->сайт выполняется автоматически
         после привязки Telegram ID (секрет: BOT_SYNC_SECRET в config.py сайта).
EOF
