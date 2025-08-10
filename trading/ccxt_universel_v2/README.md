# CCXT Universel V2

## 📋 Description

**CCXT Universel V2** est un système complet de collecte de données de trading basé sur la bibliothèque [CCXT](https://github.com/ccxt/ccxt). Il permet de récupérer des données de marché (OHLCV, trades, order book, ticker) depuis tous les exchanges supportés par CCXT, en mode REST et WebSocket.

### 🎯 Objectifs principaux

- **Universalité** : Fonctionne avec tous les exchanges CCXT (spot, futures, swap, margin, options)
- **Robustesse** : Gestion des erreurs, reconnexion automatique, déduplication
- **Flexibilité** : Multiples formats de sortie (CSV, Parquet, Feather, SQLite)
- **Temps réel** : Streaming WebSocket avec ccxt.pro
- **Simplicité** : Configuration par fichiers YAML pour le traitement par lots

## ✨ Fonctionnalités

### Mode REST
- Téléchargement OHLCV robuste avec pagination automatique
- Reprise sur fichiers existants (idempotent)
- Exclusion automatique de la bougie courante
- Gestion des limites de taux et retry avec backoff exponentiel
- Support des paramètres spécifiques aux exchanges (prix mark/index/last)

### Mode WebSocket (ccxt.pro)
- Streaming temps réel : `watch_ohlcv`, `watch_trades`, `watch_order_book`, `watch_ticker`
- Reconnexion automatique avec backoff + jitter
- Agrégation trades → OHLCV en temps réel
- Support multi-symboles
- Métriques et monitoring intégrés

### Formats de sortie
- **CSV** : Écriture atomique avec compression optionnelle
- **Parquet** : Format colonnaire haute performance (nécessite pyarrow)
- **Feather** : Format binaire rapide (nécessite pyarrow)
- **SQLite** : Base de données avec UPSERT et indexation

### Exchanges supportés
Tous les exchanges CCXT, incluant mais non limité à :
- Binance, OKX, Bybit, Bitget, Gate.io
- Kraken, Coinbase, FTX, Huobi
- Et 100+ autres exchanges

## 🔧 Installation

### Prérequis
- Python 3.9+
- pip ou conda

### Installation des dépendances

```bash
# Installation des dépendances de base
pip install -r requirements.txt

# Ou installation manuelle
pip install ccxt>=4.3.0 pandas>=2.2.0 PyYAML>=6.0.1 aiohttp>=3.9.0 websockets>=12.0

# Pour les formats Parquet/Feather (optionnel mais recommandé)
pip install pyarrow>=14.0.0
```

### Structure des fichiers
```
ccxt_universel_v2/
├── README.md                  # Ce fichier
├── requirements.txt           # Dépendances Python
├── ccxt_batch.yaml           # Exemple de configuration par lots
├── module_ccxt_fr.py         # Module de base CCXT
├── module_ccxt_fr_v2.py      # Module V2 avec fonctionnalités avancées
└── runner_ccxt_batch.py      # Exécuteur de tâches par lots
```

## 🚀 Utilisation

### 1. Utilisation programmée (Python)

#### Exemple REST - Téléchargement OHLCV

```python
import module_ccxt_fr_v2 as mod
from datetime import datetime

# Configuration de l'exchange
cfg = mod.ConfigurationEchange(
    id_exchange="binance",
    type_marche="spot",
    cles_api={}  # Optionnel pour données publiques
)

# Gestionnaire d'exchange
gest = mod.GestionnaireEchangeCCXT(cfg)

# Paramètres de téléchargement
params = mod.ParametresOHLCV(
    symbole="BTC/USDT",
    timeframe="1h",
    date_debut="2024-01-01",
    date_fin="2024-01-31",
    sortie=mod.ParametresSortie(
        format="parquet",
        chemin="donnees/btc_usdt_1h.parquet"
    )
)

# Téléchargement
df = gest.telecharger_ohlcv(params)
print(f"Téléchargé {len(df)} bougies")
```

#### Exemple WebSocket - Streaming temps réel

```python
import asyncio
import module_ccxt_fr_v2 as mod

async def main():
    cfg = mod.ConfigurationEchange(
        id_exchange="okx",
        type_marche="swap",
        sous_type="linear"
    )
    
    pro = mod.GestionnaireEchangeCCXTPro(cfg)
    
    params = mod.ParametresFlux(
        type_flux="ohlcv",
        symbole="BTC/USDT:USDT",
        timeframe="1m",
        duree_max_s=300,  # 5 minutes
        sortie=mod.ParametresSortie(
            format="csv",
            chemin="donnees/okx_btc_stream.csv"
        )
    )
    
    await pro.stream_donnees(params)

asyncio.run(main())
```

### 2. Utilisation par lots (YAML)

#### Configuration YAML

Créez un fichier `ma_config.yaml` :

```yaml
version: 1
defaults:
  timeout: 30000
  format: parquet
  output_dir: donnees

tasks:
  # Téléchargement REST OHLCV
  - name: binance_btc_1h_janvier
    mode: rest
    exchange: binance
    type_marche: spot
    symbole: BTC/USDT
    timeframe: 1h
    date_debut: 2024-01-01
    date_fin: 2024-02-01
    sortie: donnees/binance_btc_1h_jan2024.parquet
  
  # Streaming WebSocket multi-symboles
  - name: okx_multi_ohlcv
    mode: stream
    stream: ohlcv
    exchange: okx
    type_marche: swap
    sous_type: linear
    symbols: [BTC/USDT:USDT, ETH/USDT:USDT, SOL/USDT:USDT]
    timeframe: 1m
    duree: 600  # 10 minutes
    format: sqlite
    sqlite_table: ohlcv_1m
    sortie: donnees/okx_multi_stream.sqlite
  
  # Agrégation trades → OHLCV
  - name: bybit_trades_to_ohlcv
    mode: stream
    stream: trades
    exchange: bybit
    type_marche: future
    sous_type: inverse
    symbole: BTC/USD:BTC
    timeframe: 1m
    trades_vers_ohlcv: true
    duree: 300
    format: csv
    sortie: donnees/bybit_trades_ohlcv.csv
```

#### Exécution des tâches

```bash
# Exécuter toutes les tâches
python runner_ccxt_batch.py --yaml ma_config.yaml

# Exécuter des tâches spécifiques
python runner_ccxt_batch.py --yaml ma_config.yaml --taches binance_btc_1h_janvier,okx_multi_ohlcv

# Avec logs détaillés
python runner_ccxt_batch.py --yaml ma_config.yaml --debug
```

## ⚙️ Configuration

### Paramètres d'exchange

```python
cfg = mod.ConfigurationEchange(
    id_exchange="binance",           # ID de l'exchange CCXT
    type_marche="spot",              # spot, future, swap, margin, option
    sous_type="linear",              # linear, inverse (pour futures/swap)
    cles_api={                       # Clés API (optionnel pour données publiques)
        "apiKey": "votre_cle",
        "secret": "votre_secret",
        "sandbox": True              # Mode test
    },
    options_exchange={               # Options spécifiques à l'exchange
        "defaultType": "swap"
    },
    timeout_ms=30000,
    enable_rate_limit=True
)
```

### Types de flux WebSocket

- `ohlcv` : Bougies OHLCV
- `trades` : Transactions individuelles
- `order_book` : Carnet d'ordres
- `ticker` : Ticker (prix, volume 24h)

### Formats de sortie supportés

| Format | Extension | Dépendances | Avantages |
|--------|-----------|-------------|-----------|
| CSV | `.csv` | Aucune | Universel, lisible |
| Parquet | `.parquet` | pyarrow | Compact, rapide, schéma |
| Feather | `.feather` | pyarrow | Très rapide, préserve types |
| SQLite | `.sqlite` | Aucune | Requêtes SQL, UPSERT |

## 📊 Exemples d'utilisation

### Collecte de données historiques massives

```yaml
# Configuration pour télécharger 1 an de données BTC/USDT
- name: binance_btc_historical
  mode: rest
  exchange: binance
  symbole: BTC/USDT
  timeframe: 1m
  date_debut: 2023-01-01
  date_fin: 2024-01-01
  format: parquet
  compression: gzip
  sortie: donnees/btc_usdt_1m_2023.parquet.gz
```

### Surveillance temps réel multi-exchanges

```yaml
# Surveillance simultanée sur plusieurs exchanges
- name: multi_exchange_btc
  mode: stream
  stream: ticker
  symbols: [BTC/USDT]
  exchanges: [binance, okx, bybit]
  duree: 3600  # 1 heure
  format: sqlite
  sqlite_table: tickers
  sortie: donnees/multi_exchange_tickers.sqlite
```

### Analyse de microstructure (carnet d'ordres)

```yaml
- name: orderbook_analysis
  mode: stream
  stream: order_book
  exchange: binance
  symbole: BTC/USDT
  depth: 20  # 20 niveaux de prix
  duree: 300
  format: feather
  sortie: donnees/btc_orderbook.feather
```

## 🔍 Monitoring et métriques

Le système intègre des métriques automatiques :

- **Compteurs** : Messages reçus, erreurs, reconnexions
- **Latences** : Temps de traitement approximatifs
- **Logs périodiques** : État du streaming toutes les 60 secondes

```python
# Activation des métriques détaillées
params = mod.ParametresFlux(
    # ... autres paramètres
    intervalle_metrics_s=30,  # Logs toutes les 30 secondes
    flush_toutes_n=100       # Sauvegarde tous les 100 messages
)
```

## 🐛 Dépannage

### Problèmes courants

#### Erreur d'installation pyarrow
```bash
# Sur certains systèmes
pip install --no-cache-dir pyarrow
# Ou utiliser conda
conda install pyarrow
```

#### Erreur de connexion WebSocket
```python
# Vérifier que ccxt.pro est disponible
import ccxt.pro as ccxtpro
print(ccxtpro.exchanges)  # Liste des exchanges supportés en WebSocket
```

#### Limites de taux dépassées
```yaml
# Ajuster le timeout et les paramètres de limite
defaults:
  timeout: 60000
  enable_rate_limit: true
  rate_limit_sleep: 1000  # ms entre requêtes
```

#### Données manquantes ou incomplètes
```python
# Vérifier les timeframes supportés
exchange = ccxt.binance()
print(exchange.timeframes)  # Timeframes disponibles

# Vérifier les symboles
markets = exchange.load_markets()
print([s for s in markets.keys() if 'BTC' in s])
```

### Logs et debugging

```bash
# Activation des logs détaillés
export PYTHONPATH=/chemin/vers/ccxt_universel_v2
python -m logging --level DEBUG runner_ccxt_batch.py --yaml config.yaml
```

## 📚 Documentation API

### Classes principales

- `ConfigurationEchange` : Configuration d'un exchange
- `GestionnaireEchangeCCXT` : Gestionnaire REST
- `GestionnaireEchangeCCXTPro` : Gestionnaire WebSocket
- `ParametresOHLCV` : Paramètres de téléchargement OHLCV
- `ParametresFlux` : Paramètres de streaming
- `ParametresSortie` : Configuration de sortie

### Méthodes clés

- `telecharger_ohlcv()` : Téléchargement REST
- `stream_donnees()` : Streaming WebSocket
- `stream_donnees_multi_symboles()` : Streaming multi-symboles

## 🤝 Contribution

Les contributions sont les bienvenues ! Merci de :

1. Fork le projet
2. Créer une branche pour votre fonctionnalité
3. Commiter vos changements
4. Pousser vers la branche
5. Ouvrir une Pull Request

## 📄 Licence

Ce projet est distribué sous licence MIT. Voir le fichier `LICENSE` pour plus de détails.

## 🔗 Liens utiles

- [Documentation CCXT](https://docs.ccxt.com/)
- [CCXT Pro (WebSockets)](https://docs.ccxt.com/en/latest/pro.html)
- [Pandas Documentation](https://pandas.pydata.org/docs/)
- [PyArrow Documentation](https://arrow.apache.org/docs/python/)

---

**Note** : Ce projet est conçu pour un usage éducatif et de recherche. Assurez-vous de respecter les conditions d'utilisation des APIs des exchanges et les réglementations locales applicables.