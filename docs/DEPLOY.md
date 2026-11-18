# Деплой на Hetzner

Покроковий запуск бота на сервері: Docker Compose (`bot` + `db`), long polling, без домену й webhook.
Усі команди виконуються на сервері, якщо не сказано інше.

## 0. Що знадобиться

| Що | Де взяти |
|---|---|
| Сервер Hetzner Cloud | Достатньо **CX22** (2 vCPU, 4 ГБ): навантаження на 200 учасників див. розділ 12 |
| Образ ОС | Ubuntu 24.04 |
| Токен бота | @BotFather → `/newbot` (**бойовий** бот, не тестовий) |
| Ваш числовий Telegram ID (і всіх організаторів) | @userinfobot |
| Доступ до репозиторію | HTTPS-посилання або SSH-ключ сервера як deploy key |

> **Один токен = один запущений бот.** Long polling не дозволяє двом процесам слухати той самий токен:
> другий отримає `Conflict: terminated by other getUpdates request`. Перед запуском на сервері
> зупиніть локальний `docker compose` зі своїм ноутбука або використовуйте для сервера окремий токен.

## 1. Сервер і Docker

Створіть сервер у Hetzner Cloud, додайте свій SSH-ключ і підключіться:

```bash
ssh root@<IP-сервера>

apt update && apt -y upgrade
curl -fsSL https://get.docker.com | sh          # Docker Engine + compose plugin
systemctl enable --now docker                   # автозапуск після перезавантаження сервера
docker compose version                          # має вивести версію v2.x
```

Рекомендовано: окремий користувач замість root.

```bash
adduser --disabled-password --gecos "" quest
usermod -aG docker quest
mkdir -p /home/quest/.ssh && cp ~/.ssh/authorized_keys /home/quest/.ssh/ && chown -R quest:quest /home/quest/.ssh
su - quest
```

## 2. Клонування

```bash
cd ~
git clone <URL-репозиторію> fit-quest
cd fit-quest
git log --oneline | head -3        # переконайтеся, що це потрібна версія
```

## 3. Налаштування `.env`

```bash
cp .env.example .env
chmod 600 .env                      # токен бота не повинен читатись іншими користувачами
nano .env
```

Заповніть:

```dotenv
BOT_TOKEN=123456:ABC...             # від @BotFather
ADMIN_IDS=111111111,222222222       # усі організатори, через кому, без пробілів
ACCESS_CODE=                        # можна лишити порожнім: кодове слово задасте командою /setcode
BOARD_TOKEN=                        # довгий випадковий рядок, див. нижче
TZ=Europe/Kyiv
LOG_LEVEL=INFO
BOARD_PUBLIC_URL=http://<IP-сервера>:8080
BOARD_BIND=0.0.0.0                  # див. розділ 5; 127.0.0.1, якщо ставите Caddy/nginx
```

`DATABASE_URL` у `.env` можна не чіпати: у compose бот завжди ходить у БД за іменем сервісу `db`.

Згенерувати токен табло:

```bash
openssl rand -hex 24
```

> Порожній `ACCESS_CODE` означає, що **ніхто не зможе зареєструватись**, доки адмін не виконає `/setcode`.
> Порожній `BOARD_TOKEN` вимикає живе табло.

## 4. Запуск

```bash
docker compose up -d --build
docker compose ps                   # db: healthy, bot: running
docker compose logs -f bot
```

У логах мають з'явитись (у цьому порядку):

```
Контент: 5 раундів, 55 питань
У контенті 16 незаповнених питань (TODO): ...     ← поки TODO не закрито, це очікувано
Етап квесту: DRAFT
Бот запущено: @<ім'я_бота>
Recovery: питань у грі 0, не надісланих 0
Табло: порт 8080 (/board?token=…)
```

Якщо бот одразу виходить, дивіться розділ 11. Перевірка табло з самого сервера:

```bash
curl -s http://127.0.0.1:8080/healthz                                  # → ok
curl -s "http://127.0.0.1:8080/api/board?token=$(grep BOARD_TOKEN .env | cut -d= -f2)" | head -c 300
```

## 5. Порт для живого табло

Табло — це сторінка з токеном у посиланні (`/board?token=…`). Оберіть **один** варіант.

### Варіант A. Напряму, порт 8080 (простіше, без домену)

1. У `.env`: `BOARD_BIND=0.0.0.0` і `BOARD_PUBLIC_URL=http://<IP-сервера>:8080`, потім `docker compose up -d`.
2. Відкрийте порт у **Hetzner Cloud Firewall** (Cloud Console → Firewalls → Inbound):
   - TCP **22** (SSH), краще лише з вашої IP;
   - TCP **8080** (табло).
3. Не покладайтесь на `ufw` для порту 8080: опубліковані порти Docker обходять правила `ufw`.
   Саме тому закривати й відкривати порт треба в Hetzner Cloud Firewall.

Токен має бути довгим (`openssl rand -hex 24`), бо адреса відкрита всьому інтернету; трафік іде по HTTP,
тож посилання не публікуйте в чаті Zoom: показуйте екран, а не посилання.

### Варіант B. За Caddy з HTTPS (потрібен домен)

1. Направте A-запис домену (наприклад, `board.example.com`) на IP сервера.
2. У `.env`: `BOARD_BIND=127.0.0.1`, `BOARD_PUBLIC_URL=https://board.example.com`.
3. Встановіть Caddy (`apt install -y caddy`) і пропишіть `/etc/caddy/Caddyfile`:

   ```
   board.example.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```

4. `systemctl reload caddy`. У Hetzner Cloud Firewall відкрийте TCP **80** і **443** (не 8080).

Після будь-якого варіанту: у боті від адміна команда `/board` повертає готове посилання.

## 6. Налаштування бота в Telegram (разово)

У @BotFather для бойового бота:

- `/setname`, `/setuserpic` (лого ФІТ / студпарламенту), `/setdescription`, `/setabouttext`;
- `/setcommands` → лише `start` (адмін-команди учасникам показувати не треба);
- `/setprivacy` → Enable (бот працює лише в особистих повідомленнях).

## 7. Перший запуск у боті (адмін)

```
/validate        # контент: помилки й список TODO; квест не стартує, поки є TODO
/warmup          # завантажити картинки в Telegram і закешувати file_id
/board           # посилання на живе табло
/admin           # панель: етап, лічильники, кнопки
```

Якщо `/validate` показує TODO, їх треба закрити в `content/quest.yaml` (див. `TODO.md`) і виконати
`/reload_content`. Для генерального прогону можна стартувати попри TODO: `/start_quest force`.

Після зміни картинок: `python scripts/prepare_images.py --update-yaml` (потрібен `pip install pillow`),
потім `/reload_content` і `/warmup`.

## 8. Генеральний прогін (19.11)

Повний сценарій з командою в Zoom, реальний бот на сервері:

```
/setcode <слово>  →  /open_reg  →  (учасники реєструються)  →  /start_quest force
/close_in 15      →  (гра)      →  /close_quest  →  /top 5  →  /publish_results  →  /export
```

Після прогону поверніть усе до чистого стану: **`/reset_all`** (двічі підтвердіть). Команда видаляє учасників,
спроби й відгуки та повертає етап у DRAFT; кодове слово лишається, адмінів не чіпає. Працює в усіх етапах,
крім RUNNING.

## 9. День події

| Коли | Що |
|---|---|
| За день | Прогін, `/reset_all`, `/validate`, `/warmup`, перевірити `docker compose ps` і місце на диску (`df -h`) |
| За годину | `docker compose logs --tail 50 bot` (без помилок), відкрити `/board` у браузері |
| На початку | `/setcode <слово>`, `/open_reg` |
| +3–5 хв | `/start_quest`, `/close_in 15` |
| Під час гри | Показати екран із табло; `/stats`, `/top` за потреби; `docker compose logs -f bot` в окремому вікні |
| −2 хв | `/broadcast Лишилось 2 хвилини!` |
| Кінець | `/close_quest`, `/top 5`; потім `/publish_results`, `/export` |

Рестарт бота посеред гри безпечний: стан живе в БД, після старту `Recovery` закриває прострочені питання
й перепланує таймери. Учасник побачить «⏰ Час вийшов» і наступне питання.

## 10. Експлуатація

```bash
docker compose logs -f bot                 # журнал бота
docker compose restart bot                 # перезапуск (гра відновиться)
docker compose ps                          # стан контейнерів
docker stats --no-stream                   # навантаження CPU/RAM
```

**Оновлення коду** (не під час гри, хоча рестарт і безпечний):

```bash
git pull
docker compose up -d --build               # міграції БД виконуються автоматично при старті
```

**Контент без перезбирання образу:** `content/` змонтовано в контейнер. Відредагували `quest.yaml` → у боті
`/validate`, потім `/reload_content`. Під час RUNNING перечитування заборонене, якщо файл змінився після
старту квесту.

**Резервна копія БД:**

```bash
docker compose exec -T db pg_dump -U quest quest | gzip > backup-$(date +%F-%H%M).sql.gz
# відновлення:
gunzip -c backup-….sql.gz | docker compose exec -T db psql -U quest quest
```

Зробіть копію перед подією й одразу після `/close_quest`.

## 11. Якщо щось не так

| Симптом | Причина й дія |
|---|---|
| Бот одразу виходить, у логу `Telegram відхилив BOT_TOKEN` | Невірний токен у `.env`; `docker compose up -d` після виправлення |
| `Conflict: terminated by other getUpdates` | Той самий токен запущено ще деінде (ноутбук, старий контейнер). Зупиніть зайвий |
| `Контент: … помилок` і вихід | Помилка в `quest.yaml`: `docker compose exec bot python -m bot.content.loader` покаже, де |
| Бот мовчить на `/start` | `docker compose logs bot`; перевірте, що контейнер `running`, а `ADMIN_IDS` без пробілів |
| «Реєстрацію зараз закрито» | Адмін не виконав `/open_reg` або не задано `/setcode` |
| Табло відповідає `403` | Невірний/відсутній `token` у посиланні: візьміть свіже через `/board` |
| Табло не відкривається ззовні | `BOARD_BIND=0.0.0.0`, відкритий порт у Hetzner Cloud Firewall, `docker compose ps` показує `0.0.0.0:8080->8080` |
| `db` не стає healthy | `docker compose logs db`; перевірте вільне місце (`df -h`) |
| Питання приходять із запізненням | Перевірте навантаження `docker stats`; Telegram може обмежувати: у логах шукайте `RetryAfter` |

## 12. Навантажувальний тест

Перед подією перевірте запас потужності сервера. **Скрипт стирає дані**, тому запускайте його на окремій БД,
а не на бойовій (він відмовляється ходити на нелокальні хости):

```bash
docker run -d --name fq-test-pg -e POSTGRES_USER=quest -e POSTGRES_PASSWORD=quest \
    -e POSTGRES_DB=quest -p 127.0.0.1:55432:5432 postgres:16
pip install -e .                                           # у venv
python scripts/load_test.py --players 200 --think-ms 8000  # реальний темп: ~25 відповідей/с, ~9 хв
python scripts/load_test.py --players 200 --think-ms 800   # стрес: темп ×10, ~2,5 хв
docker rm -f fq-test-pg
```

Тест перевіряє p95 відповіді < 1 с (NFR-2), що всі дійшли до кінця, а подвійні натискання не зараховуються двічі.
Результати вимірювань на розробницькій машині див. в `docs/LOAD_TEST.md`.

## 13. Після події (NFR-8)

1. `/export` → збережіть `participants.csv`, `answers.csv`, `feedback.csv` (для Hillel: цитати з `feedback.csv`).
2. `/stats` → скріншот для звіту партнерам.
3. Зробіть резервну копію БД (розділ 10).
4. Видаліть персональні дані: `/reset_all`, а щоб стерти ще й БД повністю: `docker compose down -v`.
5. Відкличте токен бота в @BotFather (`/revoke`), якщо бот більше не потрібен.
