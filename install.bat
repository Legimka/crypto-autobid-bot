@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r requirements.txt
if not exist config.py (
  copy config.example.py config.py >nul
  echo.
  echo Создан config.py - открой его Блокнотом и впиши API_ID и API_HASH.
)
echo.
echo Для записи мини-приложений нужен Edge или Chrome (Edge в Windows уже есть).
echo Если их нет: python -m playwright install chromium
pause
