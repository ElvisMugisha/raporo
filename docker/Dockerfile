FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m raporo && chown -R raporo:raporo /app
USER raporo

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
