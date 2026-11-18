# FIT Quest Bot

Telegram-бот онлайн-квесту до Дня ФІТ КНУ (20.11). Учасники в Zoom отримують посилання й кодове слово,
а далі проходять 5 раундів у боті у власному темпі.

## Документація

| Для кого | Файл |
|---|---|
| Учасники | [docs/USER_GUIDE.md](docs/USER_GUIDE.md): як зареєструватись і грати |
| Адміни | [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md): усі команди, сценарій дня, типові ситуації |
| Деплой | [docs/DEPLOY.md](docs/DEPLOY.md): сервер Hetzner, `.env`, порт табло, бекапи |
| Навантаження | [docs/LOAD_TEST.md](docs/LOAD_TEST.md): виміри для 200 учасників |
| Розробка | [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md), [docs/DESIGN.md](docs/DESIGN.md), [CLAUDE.md](CLAUDE.md) |

## Швидкий старт

```bash
cp .env.example .env            # заповни BOT_TOKEN, ADMIN_IDS, BOARD_TOKEN
python scripts/preflight.py     # перевірка перед запуском
docker compose up -d --build
docker compose logs -f bot
```

Далі в боті від адміна: `/validate`, `/warmup`, `/setcode <слово>`, `/open_reg`, `/admin`.

## Тести

```bash
docker run -d --name fq-test-pg -e POSTGRES_USER=quest -e POSTGRES_PASSWORD=quest \
    -e POSTGRES_DB=quest -p 127.0.0.1:55432:5432 postgres:16
pip install -e ".[dev,images]"
pytest
```

Контент квесту: `content/quest.yaml` (питання, тексти, налаштування раундів). Код не містить текстів питань.
