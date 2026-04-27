# Selectel Floating IP Helper

Утилита для подбора floating IP в Selectel по локальным спискам IP/CIDR.

Скрипт проверяет уже существующие floating IP проекта, создает новые адреса при необходимости, сверяет их с локальными списками и удаляет неподходящие адреса. Найденный подходящий IP можно подтвердить через Telegram.

## Возможности

- Чтение списков `*.txt` из папки `ip`.
- Проверка существующих floating IP проекта.
- Создание нового floating IP, если подходящего адреса нет.
- Сверка адресов по локальным IP/CIDR спискам.
- Автоматическое удаление неподходящих адресов.
- Повторы запросов к API Selectel с backoff.
- Логи запусков в папке `logs`.
- Подтверждение найденного IP через Telegram-кнопки.

## Структура

```text
.
├── selectel_floating_ip.py     # основная CLI-утилита
├── find-ip.cmd                 # быстрый запуск на Windows
├── .env.example                # пример настроек
└── ip/                         # списки разрешенных IP/CIDR
```

## Быстрый старт

Скопируйте пример настроек:

```bash
cp .env.example .env
```

На Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Заполните в `.env` минимум:

```env
SELECTEL_X_TOKEN=your_selectel_token
SELECTEL_PROJECT_ID=your_project_id
SELECTEL_REGION=ru-2
SELECTEL_IP_LIST_DIR=ip/strict
```

Запустите проверку авторизации:

```bash
python3 selectel_floating_ip.py auth-check
```

На Windows:

```powershell
python .\selectel_floating_ip.py auth-check
```

## Команды

**Основная команда запуска**

Linux/macOS:

```bash
python3 selectel_floating_ip.py find --local-list
```

Windows PowerShell:

```powershell
.\find-ip.cmd
```

Дополнительные команды:

Linux/macOS:

```bash
python3 selectel_floating_ip.py list
python3 selectel_floating_ip.py find --local-list
python3 selectel_floating_ip.py create --dry-run
python3 selectel_floating_ip.py create
python3 selectel_floating_ip.py delete --ip 111.88.228.214 --dry-run
```

Windows PowerShell:

```powershell
python .\selectel_floating_ip.py list
python .\selectel_floating_ip.py find --local-list
python .\selectel_floating_ip.py create --dry-run
python .\selectel_floating_ip.py create
python .\selectel_floating_ip.py delete --ip 111.88.228.214 --dry-run
```

## Telegram-подтверждение

Чтобы включить подтверждение найденного подходящего IP через Telegram, добавьте в `.env`:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
SELECTEL_TELEGRAM_CONFIRM_MATCH=1
SELECTEL_TELEGRAM_CONFIRM_TIMEOUT_SECONDS=600
SELECTEL_TELEGRAM_CONFIRM_DEFAULT_ACTION=keep_stop
```

Доступные действия:

- `keep_continue` - оставить IP и продолжить поиск.
- `keep_stop` - оставить IP и остановить скрипт.
- `delete_continue` - удалить найденный IP и продолжить поиск.

Если ответа в Telegram нет до таймаута, применяется действие из `SELECTEL_TELEGRAM_CONFIRM_DEFAULT_ACTION`.

## Настройки

| Переменная | Назначение |
| --- | --- |
| `SELECTEL_X_TOKEN` | X-Token Selectel. |
| `SELECTEL_PROJECT_ID` | ID проекта Selectel. |
| `SELECTEL_REGION` | Регион floating IP, например `ru-2`. |
| `SELECTEL_IP_LIST_DIR` | Папка со списками IP/CIDR. |
| `SELECTEL_MAX_ATTEMPTS` | Максимум попыток поиска, `0` без ограничения. |
| `SELECTEL_DELAY_SECONDS` | Базовая задержка между попытками. |
| `SELECTEL_DELAY_JITTER_SECONDS` | Случайный разброс задержки. |
| `SELECTEL_API_RETRIES` | Количество повторов запросов к API. |
| `SELECTEL_BACKOFF_BASE_SECONDS` | Начальная задержка backoff. |
| `SELECTEL_BACKOFF_CAP_SECONDS` | Максимальная задержка backoff. |
| `SELECTEL_HTTP_TIMEOUT_SECONDS` | Таймаут HTTP-запросов. |
| `SELECTEL_OUTPUT_MODE` | Режим вывода, например `compact`. |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота. |
| `TELEGRAM_CHAT_ID` | Разрешенный Telegram chat id. |
| `SELECTEL_TELEGRAM_CONFIRM_MATCH` | Включить Telegram-подтверждение найденного IP. |
| `SELECTEL_TELEGRAM_CONFIRM_TIMEOUT_SECONDS` | Сколько ждать ответ в Telegram. |
| `SELECTEL_TELEGRAM_CONFIRM_DEFAULT_ACTION` | Действие по таймауту: `keep_continue`, `keep_stop` или `delete_continue`. |
