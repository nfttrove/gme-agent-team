FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y supervisor && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects $PORT; default to 8765 for local docker runs
ENV LOGGER_PORT=${PORT:-8765}

EXPOSE ${PORT:-8765}

CMD ["supervisord", "-c", "supervisord.conf"]
