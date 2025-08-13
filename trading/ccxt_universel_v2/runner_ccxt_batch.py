#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runner_ccxt_batch.py — Lance des tâches REST/WS décrites dans un YAML
Dépendances : pyyaml, pandas, ccxt (et ccxt.pro inclus), module_ccxt_fr_v2.py (dans le PYTHONPATH)

Usage rapide :
  python runner_ccxt_batch.py --yaml ccxt_batch.yaml
  python runner_ccxt_batch.py --yaml ccxt_batch.yaml --taches binance_btc_1m_jan2024,okx_multi_ohlcv

Notes :
- Les clés "defaults" du YAML sont fusionnées dans chaque tâche.
- Les champs non requis sont optionnels.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------- Normalisation sortie/format (multi & single) ----------
from pathlib import Path

def _normalize_sortie_format(task: dict) -> None:
    fmt = task.get("format")
    out = task.get("sortie")

    # Déduire le format depuis l’extension si fmt manquant et out est une string
    if not fmt and isinstance(out, str):
        s = out.lower()
        if s.endswith(".sqlite"):
            fmt = "sqlite"
        elif s.endswith(".parquet"):
            fmt = "parquet"
        elif s.endswith(".feather"):
            fmt = "feather"
        elif s.endswith(".csv"):
            fmt = "csv"

    # Valeurs par défaut si sortie absente (évite .endswith(None))
    if not out:
        name = (task.get("name") or "sortie").replace(" ", "_")
        ext = {
            "sqlite": ".sqlite",
            "parquet": ".parquet",
            "feather": ".feather",
            "csv": ".csv",
        }.get(fmt or "csv", ".csv")
        out = f"donnees/{name}{ext}"

    # Table par défaut pour SQLite si absente
    if (fmt or "").lower() == "sqlite" and not task.get("sqlite_table"):
        tf = task.get("timeframe") or "1m"
        task["sqlite_table"] = f"ohlcv_{tf}"

    # Normalisation finale
    task["format"] = (fmt or "csv").lower()
    task["sortie"] = out

# Import YAML (PyYAML requis)
try:
    import yaml
except Exception as e:
    print("❌ PyYAML est requis. Installez-le : pip install pyyaml", file=sys.stderr)
    raise

# Import du module V2
try:
    import module_ccxt_fr_v2 as mod
except Exception as e:
    print("❌ Impossible d'importer module_ccxt_fr_v2.py. Placez ce fichier à côté du runner ou dans PYTHONPATH.", file=sys.stderr)
    raise

LOGGER = logging.getLogger("runner_ccxt_batch")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ------------------------------ Outils YAML -------------------------------

def charger_yaml(chemin: Path) -> Dict[str, Any]:
    """Charge et valide le YAML."""
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("Le fichier YAML doit définir un mapping au niveau racine.")
        if "tasks" not in data or not isinstance(data["tasks"], list):
            raise ValueError("Le YAML doit contenir une clé 'tasks' avec une liste de tâches.")
        data.setdefault("defaults", {})
        return data
    except Exception as exc:
        LOGGER.error("Erreur de lecture YAML : %s", exc)
        raise

def fusionner_defaults(tache: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Merge superficiel defaults -> tache (la tâche a priorité)."""
    out = dict(defaults or {})
    out.update(tache or {})
    return out

# ----------------------------- Exécution tâche ----------------------------

def construire_cfg_echange(d: Dict[str, Any]) -> mod.ParametresEchange:
    """Construit ParametresEchange depuis dict fusionné."""
    try:
        return mod.ParametresEchange(
            id_exchange=d.get("exchange", "binance"),
            gerer_rate_limit=True,
            timeout_ms=int(d.get("timeout", 30000)),
            sandbox=bool(d.get("sandbox", False)),
            api_key=d.get("api_key"),
            secret=d.get("secret"),
            password=d.get("password"),
            uid=d.get("uid"),
            options=d.get("options", {}) or {},
            type_marche=d.get("type_marche", "spot"),
            sous_type=d.get("sous_type"),
            mode_marge=d.get("mode_marge"),
        )
    except Exception as exc:
        raise ValueError(f"Paramètres exchange invalides : {exc}") from exc

def construire_sortie(d: Dict[str, Any], stream: bool) -> mod.ParametresSortie:
    """Construit ParametresSortie avec chemin auto si absent."""
    fmt = d.get("format", "csv")
    chemin = d.get("sortie")
    if chemin is None:
        base = "flux" if stream else f"ohlcv_{(d.get('symbole','multi')).replace('/','-').replace(':','-')}_{d.get('timeframe','1m')}"
        ext = {"csv":"csv","parquet":"parquet","feather":"feather","sqlite":"sqlite"}[fmt]
        chemin = mod.DOSSIER_DONNEES / f"{d.get('exchange','exch')}_{base}_{mod._utc_now_str()}.{ext}"
    return mod.ParametresSortie(
        format=fmt,
        chemin=Path(chemin),
        table=d.get("sqlite_table", "ohlcv"),
        compression=d.get("compression"),
    )

def executer_tache_rest(d: Dict[str, Any]) -> None:
    """Exécute une tâche REST OHLCV."""
    _normalize_sortie_format(d)
    cfg = construire_cfg_echange(d)
    gest = mod.GestionnaireEchangeCCXT(cfg)
    sortie = construire_sortie(d, stream=False)

    p = mod.ParametresOHLCV(
        symbole=d["symbole"],
        timeframe=d.get("timeframe","1m"),
        date_debut=d.get("date_debut"),
        date_fin=d.get("date_fin"),
        limite_par_requete=d.get("limite"),
        prix_ohlcv=d.get("prix_ohlcv"),
        exclure_bougie_courante=not d.get("no_exclure_bougie_courante", False),
        strict_bornes=not d.get("no_bornes_strictes", False),
        sortie=sortie,
        params_additionnels=d.get("params", {}) or {},
    )
    LOGGER.info("→ REST %s %s %s", cfg.id_exchange, p.symbole, p.timeframe)
    df = gest.telecharger_ohlcv(p)
    LOGGER.info("✔ REST terminé : %d lignes → %s", len(df), sortie.chemin)

async def _executer_tache_stream_async(d: Dict[str, Any]) -> None:
    """Exécute une tâche stream (async)."""
    _normalize_sortie_format(d)
    cfg = construire_cfg_echange(d)
    pro = mod.GestionnaireEchangeCCXTPro(cfg)
    sortie = construire_sortie(d, stream=True)
    params_add = d.get("params", {}) or {}
    symboles = d.get("symboles") or d.get("symbols")
    if isinstance(symboles, str):
        symboles = [s.strip() for s in symboles.split(",") if s.strip()]

    pf = mod.ParametresFlux(
        type_flux=d["stream"],
        symbole=d.get("symbole") or (symboles[0] if symboles else None),
        timeframe=d.get("timeframe","1m"),
        prix_ohlcv=d.get("prix_ohlcv"),
        profondeur=d.get("depth"),
        params_additionnels=params_add,
        duree_max_s=d.get("duree"),
        sortie=sortie,
        flush_toutes_n=int(d.get("flush", 20)),
        exclure_bougie_courante=not d.get("no_exclure_bougie_courante", False),
        symboles=symboles,
        intervalle_metrics_s=int(d.get("metrics_interval", 60)),
        mode_trades_vers_ohlcv=bool(d.get("trades_vers_ohlcv", False)),
    )
    
    symb_log = ",".join(symboles) if symboles else (d.get("symbole") or "?")
    LOGGER.info("→ STREAM %s %s %s", cfg.id_exchange, pf.type_flux, symb_log)

    if pf.symboles:
        await pro.flux_multisymboles(pf)
    else:
        if pf.type_flux == "ohlcv":
            await pro.flux_ohlcv(pf)
        elif pf.type_flux == "trades":
            await pro.flux_trades(pf)
        elif pf.type_flux == "ticker":
            await pro.flux_ticker(pf)
        elif pf.type_flux == "orderbook":
            await pro.flux_orderbook(pf)
    LOGGER.info("✔ STREAM terminé → %s", sortie.chemin)

def executer_tache_stream(d: Dict[str, Any]) -> None:
    """Wrapper sync pour exécuter la tâche stream async."""
    asyncio.run(_executer_tache_stream_async(d))

# ------------------------------- Programme --------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Runner YAML pour module_ccxt_fr_v2")
    ap.add_argument("--yaml", required=True, type=Path, help="Chemin du fichier YAML (batch).")
    ap.add_argument("--taches", default=None, help="Liste de noms de tâches (séparés par des virgules) à exécuter, sinon toutes.")
    args = ap.parse_args()

    try:
        conf = charger_yaml(args.yaml)
        defaults = conf.get("defaults", {})
        tasks: List[Dict[str, Any]] = conf["tasks"]
        selection = None
        if args.taches:
            selection = {s.strip() for s in args.taches.split(",")}
    except Exception:
        return 2

    code_retour = 0
    for t in tasks:
        try:
            if selection and t.get("name") not in selection:
                continue
            d = fusionner_defaults(t, defaults)
            d["exchange"] = d.get("exchange", "binance")
            d["mode"] = d.get("mode", "rest")
            if d["mode"] == "rest":
                executer_tache_rest(d)
            elif d["mode"] == "stream":
                executer_tache_stream(d)
            else:
                LOGGER.error("Mode inconnu pour la tâche %s : %s", t.get("name"), d["mode"])
                code_retour = 2
        except KeyboardInterrupt:
            LOGGER.warning("Interrompu par l'utilisateur.")
            return 130
        except Exception as exc:
            LOGGER.error("Tâche '%s' échouée : %s", t.get("name"), exc)
            code_retour = 1

    return code_retour

if __name__ == "__main__":
    sys.exit(main())
