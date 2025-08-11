FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Paris

# tzdata + bash pour l’entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends tzdata bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dépendances
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Code + entrypoint
COPY . /app
RUN chmod +x /app/entrypoint.sh

VOLUME ["/app/donnees"]

ENTRYPOINT ["/app/entrypoint.sh"]
# Pas de CMD: tout est piloté par l’entrypoint (YAML/TASKS/args)
