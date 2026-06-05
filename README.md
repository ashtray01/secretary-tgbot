# 🤖 Secretary Bot

Telegram Business-ассистент, который следит, чтобы ни одно сообщение клиента не осталось без ответа.

## Как это работает

1. Подключаете бота в **Telegram Business → Чат-боты**
2. Когда клиент пишет вам — бот засекает таймер (по умолчанию 10 мин)
3. Если вы ответили клиенту — тред автоматически закрывается
4. Если нет — бот присылает **напоминание** с кнопками:
   - 💬 **Открыть чат** — перейти к диалогу
   - ✅ **Готово (ответил)** — закрыть вручную
   - ⏱ **Snooze** — отложить на 10/30 мин, 1/3 часа

Максимум — 6 напоминаний на одно сообщение, затем бот перестаёт беспокоить.

## Команды

- `/start` — приветствие и описание
- `/status` — статус подключения и количество неотвеченных
- `/pending` — сколько сейчас неотвеченных сообщений

## Установка

### Требования

- Python 3.10+
- Telegram Bot Token (получить у [@BotFather](https://t.me/BotFather))
- Telegram Premium с подключённым **Telegram Business**

### Настройка

1. Клонируйте репозиторий и перейдите в папку проекта:

```bash
cd ~/secretary-bot
```

2. Создайте виртуальное окружение и установите зависимости:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Создайте файл `.env`:

```bash
cp .env.example .env
```

4. Отредактируйте `.env`:

```env
TELEGRAM_TOKEN=your_bot_token_here
# PROXY_URL=http://proxy:8080    # опционально
# REMINDER_MINUTES=10             # интервал напоминания (мин)
# LOG_DIR=logs                    # директория логов
```

### Запуск

```bash
./start.sh
```

Или напрямую:

```bash
source venv/bin/activate
python bot.py
```

### Автозапуск (systemd)

```bash
sudo cp secretary-bot.service.example /etc/systemd/system/secretary-bot.service
# при необходимости отредактируйте пути
sudo systemctl daemon-reload
sudo systemctl enable --now secretary-bot
```

## Структура проекта

```
├── bot.py                     # Основной код бота
├── db.py                      # Работа с БД (SQLite + aiosqlite)
├── requirements.txt           # Зависимости Python
├── start.sh                   # Скрипт запуска
├── secretary-bot.service.example  # Пример systemd-юнита
├── .env                       # Конфигурация (токен, прокси)
└── logs/                      # Директория логов
```

## Прокси

Поддерживается любой прокси, совместимый с `aiohttp` (HTTP/HTTPS/SOCKS). При двух неудачных попытках подключения через прокси бот автоматически переключается на прямое соединение.

## Лицензия

MIT
