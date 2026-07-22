# Образ для завжди-ввімкненого хостингу (Fly.io тощо).
# Той самий код, що й локально — лише інші env через оточення.
FROM python:3.11-slim

WORKDIR /app

# Спершу залежності — краще кешується
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Сервер: слухати ззовні, без відкриття браузера, БД — на постійному томі
ENV BMM_HOST=0.0.0.0 \
    BMM_PORT=8080 \
    BMM_OPEN_BROWSER=false \
    BMM_DB_PATH=/data/monitor.sqlite3

EXPOSE 8080

CMD ["python", "-m", "app"]
