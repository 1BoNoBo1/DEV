# DEV — Mon espace de développement

**But** : centraliser tous mes codes (R&D, prod, tests) dans un même dépôt **structuré par catégories** avec
des **conventions claires** (noms FR, scripts prêts à l'emploi, idempotence) et des **outils robustes**.

## 🔖 Catégories de code

| Dossier racine             | Contenu principal                                                                 |
|----------------------------|-----------------------------------------------------------------------------------|
| `trading/`                 | Données de marché, bots, connecteurs d'exchanges, backtests, indicateurs         |
| `osint/`                   | Scripts et pipelines OSINT, scrapers anonymes, ponts TOR, intégrations           |
| `devops/`                  | IaC, Docker, CI/CD, monitoring, infra RunPod/VPS                                 |
| `datascience/`             | Feature engineering, modèles (ML/DL), notebooks                                   |
| `securite/`                | Outils de sécurité, durcissement système, audit, post-install                    |
| `scripts/`                 | Outils CLI, utilitaires transverses (backup, maintenance, batch)                 |
| `docs/`                    | Documents, schémas d’archi, cahiers de tests, notes                              |

> Chaque sous-projet vit dans son **dossier dédié**, contient un `README.md` local, un `requirements.txt` (si Python)
> et des scripts **prêts à l’emploi** (bash/python) avec gestion des erreurs.

## 📁 Arborescence actuelle (extrait)

```
DEV/
├── trading/
│   └── ccxt_universel_v2/
│       ├── module_ccxt_fr.py
│       ├── module_ccxt_fr_v2.py
│       ├── ccxt_batch.yaml
│       ├── runner_ccxt_batch.py
│       └── requirements.txt
├── docs/
├── osint/
├── devops/
├── datascience/
├── securite/
└── scripts/
```

## 🧩 Conventions (FR)

- **Nommer les fonctions Python en français** (`verifier_entrees`, `telecharger_ohlcv`, etc.) pour distinguer clairement du code de lib.
- **Toujours** prévoir gestion d’erreurs (`try/except` en Python, `set -euo pipefail` + `trap` en bash).
- **Idempotence** : relancer un script ne doit pas casser l’état (reprise sur fichiers, UPSERT DB, écritures atomiques).
- **Logs** : niveau `INFO` par défaut, messages courts et exploitables. Aucun `print()` brut en prod.
- **Données** : ne pas versionner les outputs (CSV/Parquet/SQLite) → stocker dans `donnees/` (ignoré par Git).

## 🧪 Qualité & workflow Git

- Branches : `main` (stable), `dev` (intégration), `feat/*`, `fix/*`.
- Commits : `type(scope): message` — ex. `feat(trading): module ccxt v2 parquet/sqlite`.
- Tests/CI (optionnel) : PyTest + mypy + ruff/flake8 ; pre-commit recommandé.

## 🔌 Projet inclus : *ccxt_universel_v2* (REST + WebSockets)

Sous-dossier : `trading/ccxt_universel_v2`

- `module_ccxt_fr_v2.py` : base universelle CCXT/CCXT Pro, sorties CSV/Parquet/Feather/SQLite, **trades→OHLCV**, multisymboles.
- `ccxt_batch.yaml` : batch d’exemples (REST/stream).
- `runner_ccxt_batch.py` : exécute le YAML (sélection de tâches possible).
- `requirements.txt` : dépendances.

### Installation rapide

```bash
cd trading/ccxt_universel_v2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Exemples

```bash
# REST → Parquet
python module_ccxt_fr_v2.py --exchange binance --symbole BTC/USDT --timeframe 1m   --date-debut 2024-01-01 --date-fin 2024-02-01 --format parquet

# Stream trades → OHLCV
python module_ccxt_fr_v2.py --exchange bybit --type-marche future --sous-type inverse   --symbole BTC/USD:BTC --timeframe 1m --stream trades --trades-vers-ohlcv   --sortie donnees/bybit_btc_1m_ohlcv.csv --flush 50
```

## 🔐 Secrets & sécurité

- **Jamais** de secrets/clefs en dur dans le code. Utiliser `ENV`/`.env` (non versionné) + variables d’environnement.
- Ne pas committer de données sensibles (exports, CSV, SQLite). Les dépôts **publics** doivent rester neutres.

## 📜 Licence

Choisir une licence (MIT/Apache-2.0) selon le projet. Par défaut : MIT (modifiable au besoin).
