#!/usr/bin/env bash
set -e

cd "/home/mkbot/secretary-bot/"

# Активируем виртуальное окружение
source venv/bin/activate

# Запускаем бота
exec python bot.py
