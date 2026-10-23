@echo off
setlocal
cd /d "%~dp0"

if not exist "config.py" (
  copy "config.py.example" "config.py" >nul
  echo Создан config.py из примера. Для Telegram Stars заполните BOT_TOKEN и BOT_USERNAME.
)

where py >nul 2>nul
if %errorlevel% equ 0 (
  set "PYTHON=py -3"
) else (
  set "PYTHON=python"
)

%PYTHON% -c "import aiohttp, aiogram" >nul 2>nul
if errorlevel 1 (
  echo Устанавливаю зависимости проекта...
  %PYTHON% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Не удалось установить зависимости. Проверьте Python и подключение к интернету.
    pause
    exit /b 1
  )
)

if not exist "logs" mkdir logs
echo Запускается сайт и бот, если BOT_TOKEN настроен.
echo Локальный адрес сайта: http://127.0.0.1:8080
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8080"
%PYTHON% run_all.py
echo Сервер остановлен.
pause
endlocal
