# Python slim + timezone
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Paris

# Paquets système minimaux (tzdata pour TZ)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dépendances Python
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Code
COPY . /app

# Persistance des sorties (CSV/Parquet/SQLite)
VOLUME ["/app/donnees"]

# Commande par défaut : exécute le batch YAML fourni
CMD ["python", "runner_ccxt_batch.py", "--yaml", "ccxt_batch.yaml"]
