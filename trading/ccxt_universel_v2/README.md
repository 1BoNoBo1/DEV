                                      EN TEST

# ccxt_universel_v2

Orchestrateur **REST + WebSocket** (ccxt/ccxt.pro) piloté par **YAML**, pour collecter de l’OHLCV et des trades depuis plusieurs exchanges (ex. **OKX, Binance, Bybit, Kraken**), avec :

- **Streaming multi-symboles** résilient _(fallback automatique “groupé → split”)_
- Écritures **SQLite** en **UPSERT** (idempotent, zéro doublon) et/ou **Parquet**
- **Entrypoint** ergonomique (validation YAML, filtrage de tâches)
- Mode **prod** via `docker-compose.prod.yml` (services parallèles, healthchecks, restart, log rotation)

---

## ✨ Points forts

- **YAML unique** pour décrire des _tâches_ :

  - `mode: rest` (historique OHLCV) **reprenable**
  - `mode: stream` (OHLCV ou trades→OHLCV)

- **Multi-symboles**: tente d’abord le flux groupé (ccxt.pro), **bascule automatiquement** en “split” concurrent si l’API n’est pas disponible.
- **SQLite upsert**: clé unique `(symbole, timestamp)`, `ON CONFLICT … DO UPDATE` → _reprises/retards sans doublons_.
- **Parquet**: écriture en _parts_ tournants pour les streams, concaténables en un fichier compact/analytiques (pandas/polars).
- **Prod**: 2 services parallèles (**BTC+ETH**) vers la même base (`WAL`) avec `restart: unless-stopped` & healthchecks.

---

## 🧭 Structure du repo (principaux fichiers)

```
.
├─ Dockerfile
├─ docker-compose.yml
├─ docker-compose.prod.yml        # (prod) services okx_btc / okx_eth / (optionnel) export_parquet
├─ entrypoint.sh                  # QoL: validation YAML, filtre TASKS, exécution runner
├─ runner_ccxt_batch.py           # CLI: --yaml <fichier> --taches <liste>
├─ module_ccxt_fr_v2.py           # cœur: fetch/stream, fallback groupé→split, writers SQLite/Parquet/CSV
├─ ccxt_batch.yaml                # YAML par défaut (exemples de tâches)
├─ donnees/                       # sorties (SQLite, Parquet, CSV…)
├─ *exemples YAML*
│  ├─ okx_btc_prod.yaml
│  ├─ okx_eth_prod.yaml
│  ├─ okx_multi_sqlite_split.yaml
│  ├─ okx_multi_prod_sqlite_split.yaml
│  ├─ rest_binance_btc_1m_jan24.yaml
│  └─ ...
└─ requirements.lock.txt (optionnel, si tu pin les versions)
```

---

## 🚀 Prérequis

- **Docker** et **Docker Compose plugin**
- Accès Internet sortant (WebSockets pour les streams).
- (Optionnel) Si tu veux **pinner** les versions: `requirements.lock.txt` + Dockerfile adapté (section “Pin des versions”).

---

## ⚡️ Quickstart (dev)

```bash
# 1) Construire l'image
docker compose build

# 2) Lancer le YAML par défaut (valider + exécuter)
docker compose run --rm -e VALIDATE=1 ccxt_universel

# 3) Aide (du runner)
docker compose run --rm --entrypoint python ccxt_universel - <<'PY'
import sys
print("usage: runner_ccxt_batch.py --yaml <FICHIER> [--taches <noms,virgules>]")
PY
```

### Filtrer les tâches via `TASKS`

```bash
docker compose run --rm -e VALIDATE=1 \
  -e TASKS="binance_btc_1m_jan2024,okx_btc_swap_mark_1m_10min,okx_multi_ohlcv" \
  ccxt_universel
```

### Utiliser un autre YAML (monté)

```bash
docker compose run --rm -e VALIDATE=1 \
  --volume="$(pwd)/rest_binance_btc_1m_jan24.yaml:/app/rest_binance_btc_1m_jan24.yaml:ro" \
  ccxt_universel /app/rest_binance_btc_1m_jan24.yaml
```

### Shell dans le conteneur

```bash
docker compose run --rm ccxt_universel bash
```

---

## 🧩 Schéma YAML (ccxt_batch.yaml)

Clés courantes par **tâche** :

| clé                       | valeur                                   |
| ------------------------- | ---------------------------------------- |
| `name`                    | nom unique de la tâche                   |
| `mode`                    | `rest` ou `stream`                       |
| `stream`                  | `ohlcv` ou `trades` (si mode `stream`)   |
| `exchange`                | `okx`, `binance`, `bybit`, `kraken`, …   |
| `type_marche`             | ex. `spot`, `swap`                       |
| `sous_type`               | ex. `linear`                             |
| `symbole`                 | `BTC/USDT:USDT`                          |
| `symboles`                | liste de symboles (multi)                |
| `timeframe`               | `1m`, `5m`, …                            |
| `duree`                   | secondes (`0`/`None` = **infini**)       |
| `prix_ohlcv`              | **`mark`** (recommandé), `last`, `index` |
| `exclure_bougie_courante` | `true/false`                             |
| `format`                  | `sqlite`, `parquet`, `csv`               |
| `sqlite_table`            | nom de table (ex. `ohlcv_1m`)            |
| `sortie`                  | chemin de sortie dans `/app/donnees/...` |

> **Tip**: pour `stream` multi-symboles (`symboles: [...]`), le runner tente la fonction _groupée_ de ccxt.pro; si non disponible → **fallback split** (1 socket par symbole) **en concurrence**.

### Exemples

**REST — Binance, janvier 2024, 1m → Parquet**

```yaml
version: 1
tasks:
  - name: binance_btc_1m_jan2024
    mode: rest
    exchange: binance
    symbole: BTC/USDT
    timeframe: 1m
    date_debut: 2024-01-01
    date_fin: 2024-02-01
    format: parquet
    sortie: donnees/binance_btcusdt_1m_202401.parquet
```

**STREAM — OKX BTC+ETH 1m → SQLite (prod-ready)**

```yaml
version: 1
tasks:
  - name: okx_multi_ohlcv
    mode: stream
    stream: ohlcv
    exchange: okx
    type_marche: swap
    sous_type: linear
    symboles: [BTC/USDT:USDT, ETH/USDT:USDT]
    timeframe: 1m
    duree: 300
    prix_ohlcv: mark
    exclure_bougie_courante: true
    format: sqlite
    sqlite_table: ohlcv_1m
    sortie: donnees/okx_multi_ohlcv.sqlite
```

**STREAM — Bybit trades → OHLCV 1m → CSV**

```yaml
version: 1
tasks:
  - name: bybit_btc_trades_to_ohlcv
    mode: stream
    stream: trades
    exchange: bybit
    symbole: BTC/USD:BTC
    timeframe: 1m
    duree: 120
    format: csv
    sortie: donnees/bybit_btc_trades_ohlcv.csv
```

---

## 🧠 Entrypoint (qualité de vie)

- **Sans argument** → utilise `/app/ccxt_batch.yaml`
- **Avec un chemin** → exécute ce YAML
- `VALIDATE=1` → valide le YAML avant exécution (affiche warnings)
- `TASKS="name1,name2"` → exécute uniquement ces tâches (sinon toutes)

Exemples :

```bash
# par défaut (YAML embarqué)
docker compose run --rm -e VALIDATE=1 ccxt_universel

# YAML externe + filtre
docker compose run --rm -e VALIDATE=1 -e TASKS="okx_multi_ohlcv" \
  --volume="$(pwd)/okx_multi_sqlite_split.yaml:/app/okx_multi_sqlite_split.yaml:ro" \
  ccxt_universel /app/okx_multi_sqlite_split.yaml
```

---

## 🗃️ Sorties (où regarder)

- **SQLite** (stream prod) :
  `donnees/okx_live.sqlite` → table **`ohlcv_1m`**
- **Parquet** (stream) :
  `donnees/<task_name>.part-YYYYMMDD_HHMMSS.parquet` (parts), à **concaténer** si besoin
- **Parquet** (rest) :
  `donnees/<exchange>_<pair>_<tf>_<période>.parquet`
- **CSV** : selon `sortie`

---

## 🔬 Lire les données (pandas)

```bash
# Parquet
docker compose run --rm -T --entrypoint python ccxt_universel - <<'PY'
import pandas as pd
df=pd.read_parquet('donnees/binance_btcusdt_1m_202401.parquet')
print(df.head().to_string()); print("rows:", len(df))
PY

# SQLite
docker compose run --rm -T --entrypoint python ccxt_universel - <<'PY'
import sqlite3, pandas as pd
con=sqlite3.connect('donnees/okx_live.sqlite')
print(pd.read_sql_query("SELECT symbole, COUNT(*) n FROM ohlcv_1m GROUP BY symbole", con))
print(pd.read_sql_query("SELECT * FROM ohlcv_1m ORDER BY timestamp DESC LIMIT 5", con))
con.close()
PY
```

### Concaténer des _parts_ Parquet

```bash
docker compose run --rm -T --entrypoint python ccxt_universel - <<'PY'
import glob, pandas as pd
parts=sorted(glob.glob('donnees/okx_multi_ohlcv.part-*.parquet'))
df=pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
df.to_parquet('donnees/okx_multi_ohlcv_full.parquet', compression='zstd', index=False)
print("rows:", len(df), "parts:", len(parts))
PY
```

---

## 🏭 Mode production

Un compose “prod” lance **BTC** et **ETH** **en parallèle** vers la **même** SQLite :

```yaml
# docker-compose.prod.yml (extrait)
services:
  okx_btc:
    build: .
    container_name: okx_btc
    restart: unless-stopped
    command: /app/okx_btc_prod.yaml
    volumes:
      - ./donnees:/app/donnees
      - ./okx_btc_prod.yaml:/app/okx_btc_prod.yaml:ro
    mem_limit: 512m
    cpus: "1.0"
    logging: { driver: json-file, options: { max-size: "10m", max-file: "3" } }
    healthcheck:
      test: ["CMD-SHELL", "test -s /app/donnees/okx_live.sqlite || exit 1"]
      interval: 60s; timeout: 10s; retries: 3; start_period: 120s

  okx_eth:
    build: .
    container_name: okx_eth
    restart: unless-stopped
    command: /app/okx_eth_prod.yaml
    volumes:
      - ./donnees:/app/donnees
      - ./okx_eth_prod.yaml:/app/okx_eth_prod.yaml:ro
    mem_limit: 512m
    cpus: "1.0"
    logging: { driver: json-file, options: { max-size: "10m", max-file: "3" } }
    healthcheck: *idem*
```

YAML “prod” (exemple **endless** 1m, `mark`, exclusion bougie courante) :

```yaml
version: 1
tasks:
  - name: okx_btc_live_sqlite
    mode: stream
    stream: ohlcv
    exchange: okx
    type_marche: swap
    sous_type: linear
    symbole: BTC/USDT:USDT
    timeframe: 1m
    duree: 0
    prix_ohlcv: mark
    exclure_bougie_courante: true
    format: sqlite
    sqlite_table: ohlcv_1m
    sortie: donnees/okx_live.sqlite
```

**Démarrer :**

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker logs -f okx_btc &
docker logs -f okx_eth &
```

---

## 🧱 Pin des versions (builds reproductibles)

**Option lock** (recommandée) :

```bash
# geler l’env en prod (okx_btc) et récupérer sur l’hôte
docker compose -f docker-compose.prod.yml exec -T okx_btc bash -lc 'pip freeze | sed "/^-e /d"' > requirements.lock.txt

# Dockerfile (extrait)
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --no-cache-dir -r /tmp/requirements.lock.txt

# rebuild & up
docker compose -f docker-compose.prod.yml build --no-cache
docker compose -f docker-compose.prod.yml up -d
```

---

## 🛠️ Exploitation & maintenance

**Anti-doublons (doit être 0)** :

```bash
docker compose -f docker-compose.prod.yml exec -T okx_btc python - <<'PY'
import sqlite3, pandas as pd
con=sqlite3.connect('donnees/okx_live.sqlite')
dup = pd.read_sql_query("""
SELECT symbole, timestamp, COUNT(*) n
FROM ohlcv_1m
GROUP BY symbole, timestamp
HAVING n>1
""", con)
print("doublons:", len(dup)); print(dup.head().to_string() if len(dup) else "(aucun)")
con.close()
PY
```

**Audit des “trous” (minutes manquantes)** :

```bash
docker compose -f docker-compose.prod.yml exec -T okx_btc python - <<'PY'
import sqlite3, pandas as pd
con=sqlite3.connect('donnees/okx_live.sqlite')
for s in pd.read_sql_query("SELECT DISTINCT symbole FROM ohlcv_1m", con)['symbole']:
    df=pd.read_sql_query("SELECT timestamp FROM ohlcv_1m WHERE symbole=? ORDER BY timestamp", con, params=(s,))
    ts=pd.to_datetime(df['timestamp'], utc=True)
    rng=pd.date_range(ts.iloc[0], ts.iloc[-1], freq='T', tz='UTC')
    missing=sorted(set(rng)-set(ts))
    print(f"{s}: rows={len(ts)} missing={len(missing)}")
con.close()
PY
```

**Entretien SQLite (checkpoint WAL + VACUUM)** :

```bash
docker compose -f docker-compose.prod.yml exec -T okx_btc python - <<'PY'
import sqlite3
con=sqlite3.connect('donnees/okx_live.sqlite'); cur=con.cursor()
cur.execute("PRAGMA wal_checkpoint(TRUNCATE)"); con.commit()
cur.execute("VACUUM;"); con.commit()
cur.execute("PRAGMA journal_mode=WAL;"); con.commit()
con.close()
print("Checkpoint + VACUUM OK")
PY
```

**Nettoyer les orphelins** :

```bash
docker compose down --remove-orphans
```

---

## 🧯 Dépannage

- `the input device is not a TTY`
  → Ajoute **`-T`** sur `docker compose run/exec` quand tu utilises un _heredoc_.

- `watch_ohlcv_for_symbols() ... takes ... arguments` (OKX groupé)
  → Normal : **fallback split** s’active automatiquement. Les deux symboles sont streamés **en concurrence**.

- Avertissement `prix_ohlcv: mark`
  → Recommandé pour homogénéité, surtout en dérivés/swap (`mark price`).

- Warning `version` obsolète dans `docker-compose.yml`
  → Supprime la clé `version:` (Compose V2 la déduit automatiquement).

---

## 🔒 Notes sécurité / API keys

- Les tâches publiques (OHLCV/trades) ne nécessitent pas de clés.
- Si tu ajoutes des endpoints privés : utilise des **variables d’environnement** et **ne commit jamais** tes clés.

---

## 📄 Licence

À compléter selon ton choix (MIT/Apache-2.0/GPL/etc.).

---

## ✅ Checklist “Prod OK”

- [x] Streams BTC+ETH en parallèle, **healthy**
- [x] **UPSERT** SQLite (index unique `symbole,timestamp`)
- [x] **Fallback split** validé
- [x] **Zéro doublon** sur reprise
- [x] **Log rotation** + **restart**
- [x] **Pin** des versions (si `requirements.lock.txt` adopté)
