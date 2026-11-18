# FIT Quest Bot

Telegram-бот онлайн-квесту до Дня ФІТ КНУ (20.11). Учасники в Zoom отримують посилання й кодове слово, а далі проходять 5 раундів у боті у власному темпі.

## Читати перед роботою
- `docs/REQUIREMENTS.md` — погоджені бізнес-вимоги (FR-x / NFR-x).
- `docs/DESIGN.md` — архітектура, схема БД, ігровий цикл, адмін-команди. **Це джерело правди для реалізації.**
- `content/quest.yaml` — увесь контент. Код не повинен містити текстів питань.
- `TODO.md` — що ще не готово в контенті.

## Стек
Python 3.12, aiogram 3, PostgreSQL 16 + SQLAlchemy 2 async + Alembic, pydantic / pydantic-settings, aiohttp (живе табло), Docker Compose, long polling. Деплой на Hetzner.

## Принципи
- Ігровий стан зберігається в таблиці `attempts`, а не в FSM aiogram. Рестарт не повинен ламати гру.
- Прийом відповіді й таймаут працюють через атомарний `UPDATE … WHERE cursor = :cursor` (див. DESIGN §6.4). Не послаблювати.
- Таймер на сервері — джерело правди; asyncio-таймер лише «будильник» + сторож + recovery при старті.
- Масові розсилки — тільки через `services/broadcast.py` (rate limit).
- Усі тексти для учасників — у `quest.yaml → messages`.
- Мова інтерфейсу — українська, розмітка HTML.

## Команди
```
docker compose up -d --build
docker compose logs -f bot
docker compose exec bot python -m bot.content.loader   # валідація контенту
pytest
```
