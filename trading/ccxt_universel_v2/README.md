# CCXT — Base universelle (FR) REST + WebSockets

Ce dossier contient une **implémentation universelle** autour de `ccxt` et `ccxt.pro` pour :
- **REST** : téléchargement d’**OHLCV** robustes (bornes strictes, reprise idempotente, déduplication, exclusion bougie courante),
- **WebSockets (ccxt.pro)** : `watch_ohlcv`, `watch_trades`, `watch_order_book`, `watch_ticker` avec **reconnexion/backoff + jitter**, déduplication,
- **Sorties pluggables** : `CSV` (atomique), `Parquet/Feather` (si `pyarrow`/`fastparquet`), `SQLite` (UPSERT + index unique),
- **Agrégation trades→OHLCV** (latence minimale) et **multi-symboles** (groupé si supporté, sinon parallélisme).

## Contenu

- `module_ccxt_fr_v2.py` : module principal (REST + WS) — **à utiliser en ligne de commande** et **comme librairie**.
- `module_ccxt_fr.py` : version plus simple (référence).
- `ccxt_batch.yaml` : **fichier de configuration batch** (voir ci-dessous).
- `runner_ccxt_batch.py` : **runner YAML** qui exécute des lots REST/stream.
- `requirements.txt` : dépendances Python.

---
## INSTALLATION avec venv: 

1. Créez un environnement virtuel :
   ```bash
   python -m venv venv
   ```

2. Activez l'environnement virtuel :
   - Sur Windows :
     ```bash
     venv\Scripts\activate
     ```
   - Sur macOS/Linux :
     ```bash
     source venv/bin/activate
     ```

3. Installez les dépendances :
   ```bash
   pip install -r requirements.txt
   ```


## Pourquoi cette mise en œuvre ?

- **Universalité** : fonctionne avec *tous* les exchanges supportés par `ccxt`, en tenant compte de leurs **particularités** (`limit` par page, `until`/`endTime`/`to` en ms/s, `price=mark/index/last`, modes `spot/swap/future/margin`, `linear/inverse`, etc.).
- **Robustesse** : retries + backoff exponentiel, **écriture atomique**, **idempotence** (reprise), **déduplication** fiable, **exclusion des bougies incomplètes**.
- **Performance** : formats colonne (`Parquet/Feather`), **SQLite** avec UPSERT et index unique, multi-symboles (moins de connexions WS).
- **Opérationnel** : CLI/runner prêts à l’emploi, logs clairs, configuration simple par YAML.

---

## Utilisation directe (CLI)

### REST (OHLCV)
```bash
python module_ccxt_fr_v2.py --exchange binance --symbole BTC/USDT --timeframe 1m   --date-debut 2024-01-01 --date-fin 2024-02-01 --format parquet
```

### WebSocket — OHLCV en temps réel
```bash
python module_ccxt_fr_v2.py --exchange okx --type-marche swap --sous-type linear   --symbole BTC/USDT:USDT --timeframe 1m --stream ohlcv --duree 600   --format sqlite --sqlite-table ohlcv_1m
```

### WebSocket — TRADES → OHLCV (latence minimale)
```bash
python module_ccxt_fr_v2.py --exchange bybit --type-marche future --sous-type inverse   --symbole BTC/USD:BTC --timeframe 1m --stream trades --trades-vers-ohlcv   --sortie donnees/bybit_btc_1m_ohlcv.csv --flush 50
```

---

## Le fichier `ccxt_batch.yaml` — à quoi ça sert ?

C’est un **fichier de configuration** qui décrit des **tâches** à exécuter en **batch** par le runner.  
Il permet de **centraliser** tes jobs : REST ou WebSocket, un ou plusieurs symboles, formats de sortie, etc.

### Structure

```yaml
version: 1
defaults:
  timeout: 30000
  format: parquet
  output_dir: donnees

tasks:
  - name: binance_btc_1m_jan2024
    mode: rest
    exchange: binance
    type_marche: spot
    symbole: BTC/USDT
    timeframe: 1m
    date_debut: 2024-01-01
    date_fin: 2024-02-01
    format: parquet
    sortie: donnees/binance_btcusdt_1m_202401.parquet

  - name: okx_btc_swap_mark_1m_10min
    mode: stream
    stream: ohlcv
    exchange: okx
    type_marche: swap
    sous_type: linear
    symbole: BTC/USDT:USDT
    timeframe: 1m
    prix_ohlcv: mark
    duree: 600
    format: sqlite
    sqlite_table: ohlcv_1m
    sortie: donnees/okx_stream_ohlcv_1m.sqlite

  - name: bybit_btc_trades_to_ohlcv
    mode: stream
    stream: trades
    exchange: bybit
    type_marche: future
    sous_type: inverse
    symbole: BTC/USD:BTC
    timeframe: 1m
    trades_vers_ohlcv: true
    flush: 50
    format: csv
    sortie: donnees/bybit_btc_trades_ohlcv.csv

  - name: okx_multi_ohlcv
    mode: stream
    stream: ohlcv
    exchange: okx
    type_marche: swap
    sous_type: linear
    symbols: [BTC/USDT:USDT, ETH/USDT:USDT]
    timeframe: 1m
    duree: 300
    format: parquet
    sortie: donnees/okx_multi_ohlcv.parquet
```

### Clés importantes

- **`defaults`** : bloc de valeurs par défaut appliquées à chaque tâche (écrasées si redéfinies dans la tâche).
- **`mode`** : `rest` (historique OHLCV) ou `stream` (temps réel via WebSockets).
- **`stream`** (si `mode: stream`) : `ohlcv`, `trades`, `ticker`, `orderbook`.
- **`symbole`** vs **`symbols`** : mono-symbole vs multi-symboles (si l’exchange supporte les abonnements groupés, ils sont utilisés, sinon parallélisme).
- **`format`** & **`sortie`** : choix du format (csv/parquet/feather/sqlite) et chemin du fichier de sortie.
- **`prix_ohlcv`** : `mark`/`index`/`last` si supporté par l’exchange (voir presets internes).
- **Bornes** : `date_debut`, `date_fin` (exclue) — bornage **strict** côté client.
- **Options marché** : `type_marche`, `sous_type` (`linear`/`inverse`), `mode_marge` (`isolated`/`cross`) selon support de l’exchange.
- **Streaming** : `duree` (en s), `flush` (fréquence de flush disque), `trades_vers_ohlcv` pour l’agrégation en direct.

---

## Le runner `runner_ccxt_batch.py` — pourquoi et comment ?

**Pourquoi** : orchestrer **automatiquement** une liste de tâches depuis un YAML, sans répéter la CLI. Idéal pour **cron**, **tmux**, **CI/CD**.

**Ce qu’il fait** :
1. Charge le YAML, fusionne `defaults` → chaque `task`,
2. Construit les paramètres d’exchange (avec options marché et credentials si fournis via env),
3. Exécute **REST** (téléchargement OHLCV) **ou STREAM** (WebSockets), mono ou multi-symboles,
4. Gère la sortie (formats, UPSERT SQLite, dédup), log les métriques et erreurs, renvoie un **code global**.

**Utilisation** :
```bash
pip install -r requirements.txt
python runner_ccxt_batch.py --yaml ccxt_batch.yaml
# Exécuter certaines tâches uniquement :
python runner_ccxt_batch.py --yaml ccxt_batch.yaml --taches binance_btc_1m_jan2024,okx_multi_ohlcv
```

---

## Sécurité & secrets

- Ne jamais committer de clés API. Préférer des **variables d’environnement** (`API_KEY`, `API_SECRET`, etc.) et/ou un fichier `.env` **non versionné**.
- Les fichiers de **données** (`donnees/`) et sorties (`.csv`, `.parquet`, `.sqlite`) sont ignorés par Git via le `.gitignore` racine.

---

## Dépannage rapide

- `ccxt.pro indisponible` → mettre à jour `ccxt` (import `ccxt.pro` requis), vérifier `aiohttp`/`websockets`.
- `parquet/feather` indisponible → installer `pyarrow` ou `fastparquet`.
- `SQLite ON CONFLICT` → nécessite SQLite ≥ 3.24 (versions modernes OK).

---

## Licence

Par défaut MIT (modifiable).
