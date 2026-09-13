#!/usr/bin/env bash
set -Eeuo pipefail

# Первичный запуск ButovskyVPN из уже клонированного репозитория:
#   git clone https://github.com/DmitryBloomberg/ButovskyVPN.git
#   cd ButovskyVPN
#   bash start.sh

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
CONFIG_FILE="$PROJECT_DIR/config.py"

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

fail() {
    printf '%b\n' "${RED}[✗]${NC} $1" >&2
    exit 1
}

printf '%b\n' "${CYAN}========================================${NC}"
printf '%b\n' "${CYAN}       ButovskyVPN — первый запуск${NC}"
printf '%b\n\n' "${CYAN}========================================${NC}"

command -v python3 >/dev/null 2>&1 || fail "Не найден Python 3. Установите python3 и python3-venv."

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

if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR" || fail "Не удалось создать виртуальное окружение. Установите python3-venv."
fi

"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"

export BUTOVSKY_BOT_TOKEN="$BOT_TOKEN"
export BUTOVSKY_ADMIN_ID="$ADMIN_ID"
PROJECT_DIR="$PROJECT_DIR" CONFIG_FILE="$CONFIG_FILE" \
    "$VENV_DIR/bin/python" - <<'PY'
import os
from pathlib import Path

template = Path(os.environ["PROJECT_DIR"]) / "config.py.example"
config_path = Path(os.environ["CONFIG_FILE"])
text = template.read_text(encoding="utf-8")
text = text.replace('BOT_TOKEN = "ВАШ_ТОКЕН_БОТА"', f"BOT_TOKEN = {os.environ['BUTOVSKY_BOT_TOKEN']!r}")
text = text.replace("    12345678,  # Замените на ваш реальный ID", f"    {int(os.environ['BUTOVSKY_ADMIN_ID'])},")
config_path.write_text(text, encoding="utf-8")
PY

chmod 600 "$CONFIG_FILE"
printf '%b\n' "${GREEN}[✓]${NC} Конфигурация сохранена в config.py"
printf '%b\n' "${GREEN}[✓]${NC} Запускаю ButovskyVPN. Сервер 3x-ui добавляется в админ-меню бота."
cd "$PROJECT_DIR"
exec "$VENV_DIR/bin/python" main.py