FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Залежності окремим шаром: перезбирається лише при зміні pyproject.toml.
COPY pyproject.toml .
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > requirements.txt \
    && pip install -r requirements.txt

COPY alembic.ini .
COPY bot ./bot
COPY content ./content

# Міграції, потім бот. Падіння міграції = контейнер не стартує (restart повторить спробу).
CMD ["sh", "-c", "alembic upgrade head && exec python -m bot"]
