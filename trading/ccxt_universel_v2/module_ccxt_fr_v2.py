#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
module_ccxt_fr_v2.py — Base universelle CCXT (FR) + CCXT Pro (WebSockets)
Version 2 — sorties Parquet/Feather/SQLite, agrégation trades→OHLCV, multisymboles, métriques.

Objectifs clés
--------------
- REST : OHLCV robuste (bornes, reprise idempotente, dédup, exclusion bougie courante).
- WebSockets : watch_ohlcv / watch_trades / watch_order_book / watch_ticker
  + reconnexion/backoff + jitter + dédup + persistance optionnelle.
- Sorties pluggables : CSV (atomique), Parquet/Feather (si pyarrow dispo), SQLite (UPSERT + index).
- Agrégation temps réel **trades → OHLCV** (latence plus faible) avec flush des bougies closes.
- Multi-symboles : abonnement de groupe si supporté (watch_*_for_symbols), sinon parallélisme.
- Métriques : compteurs, latences approximatives, logs périodiques.

Dépendances : ccxt, pandas
Optionnelles : pyarrow (Parquet/Feather)
Compatibilité : Python 3.9+
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Literal

import ccxt
import pandas as pd

# Import optionnel de ccxt.pro (inclus dans ccxt récents)
try:
    import ccxt.pro as ccxtpro
    HAS_CCXTPRO = True
except Exception:
    ccxtpro = None
    HAS_CCXTPRO = False

# ----------------------------- Journalisation -----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
LOGGER = logging.getLogger("module_ccxt_fr_v2")

# ------------------------------ Stockage ----------------------------------

DOSSIER_DONNEES = Path(__file__).parent / "donnees"
DOSSIER_DONNEES.mkdir(parents=True, exist_ok=True)

# ------------------------- Préréglages d'exchanges ------------------------
# Valeurs par défaut *sécurisées* si non listé : limite 1000, pas de param 'price', pas de 'until'.
EXCHANGE_PRESETS: Dict[str, Dict[str, Any]] = {
    "binance": {"ohlcv_limit_max": 1000, "until_param": "endTime", "price_param": None, "supports_margin_mode": True},
    "bybit":   {"ohlcv_limit_max": 1000, "until_param": None, "price_param": "price", "price_values": {"mark","index","last"}, "supports_margin_mode": True},
    "okx":     {"ohlcv_limit_max": 300,  "until_param": "to", "price_param": "price", "price_values": {"mark","index"}, "supports_margin_mode": True},
    "kucoin":  {"ohlcv_limit_max": 1500, "until_param": None, "price_param": None, "supports_margin_mode": True},
    "kraken":  {"ohlcv_limit_max": 720,  "until_param": None, "price_param": None, "supports_margin_mode": False},
    "gateio":  {"ohlcv_limit_max": 1000, "until_param": "to", "until_in_seconds": True, "price_param": None, "supports_margin_mode": True},
    "*":       {"ohlcv_limit_max": 1000, "until_param": None, "price_param": None, "supports_margin_mode": False},
}

# ------------------------------- Helpers ----------------------------------

def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def _parse_date_utc(txt: Optional[str]) -> Optional[datetime]:
    if not txt:
        return None
    t = txt.strip()
    # Accepte suffixe 'Z' (UTC) pour Python 3.9+
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    return _ensure_utc(dt.astimezone(timezone.utc) if dt.tzinfo else dt)

def _timeframe_delta(timeframe: str) -> timedelta:
    tf = timeframe.strip()
    table = {
        "15s": 15, "30s": 30,
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200,
        "1d": 86400, "3d": 259200, "1w": 604800, "1M": 2592000
    }
    if tf not in table:
        raise ValueError(f"Timeframe inconnu/non géré: {timeframe!r}")
    return timedelta(seconds=table[tf])

def _atomic_write_csv(df: pd.DataFrame, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(out)
    LOGGER.info("✅ Sauvegardé : %s", out)

def _append_csv(df: pd.DataFrame, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    exists = out.exists()
    df.to_csv(out, index=False, mode="a", header=not exists)
    LOGGER.info("✅ Append CSV : %s (+%d lignes)", out, len(df))

def _concat_dedup(df_list: List[pd.DataFrame], subset: Iterable[str] = ("timestamp",)) -> pd.DataFrame:
    if not df_list:
        return pd.DataFrame()
    df = pd.concat(df_list, ignore_index=True)
    df.drop_duplicates(subset=list(subset), keep="last", inplace=True)
    df.sort_values(list(subset), inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def _get_preset(exchange_id: str) -> Dict[str, Any]:
    return EXCHANGE_PRESETS.get(exchange_id, EXCHANGE_PRESETS["*"])

def build_params_ohlcv_for_exchange(exchange: Any, preset: Dict[str, Any], prix_ohlcv: Optional[str],
                                    until_ms: Optional[int], params_add: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = dict(params_add or {})
    if prix_ohlcv:
        pkey = preset.get("price_param")
        pvals = preset.get("price_values")
        if pkey:
            if pvals and prix_ohlcv not in pvals:
                raise ValueError(f"prix_ohlcv={prix_ohlcv!r} non supporté (attendu ∈ {pvals})")
            params[pkey] = prix_ohlcv
        else:
            LOGGER.warning("Paramètre 'prix_ohlcv' ignoré : non supporté par %s", getattr(exchange, 'id', 'exchange'))
    if until_ms is not None:
        ukey = preset.get("until_param")
        if ukey:
            if preset.get("until_in_seconds"):
                params[ukey] = int(until_ms / 1000)
            else:
                params[ukey] = until_ms
    return params

# ---------------------- Écrivains de données (pluggables) -----------------

@dataclass
class ParametresSortie:
    format: Literal["csv","parquet","feather","sqlite"] = "csv"
    chemin: Optional[Path] = None
    table: str = "ohlcv"          # pour sqlite
    compression: Optional[str] = None  # "gzip", "bz2", etc. (CSV uniquement)
    # Pour SQLite, clef unique :
    unique_par: Tuple[str, ...] = ("symbole","timeframe","timestamp")

def _ecrire_parquet(df: pd.DataFrame, out: Path) -> None:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out, index=False)
        LOGGER.info("✅ Sauvegardé (parquet) : %s", out)
    except Exception as e:
        raise RuntimeError("Écriture Parquet requiert 'pyarrow' ou 'fastparquet'.") from e

def _ecrire_feather(df: pd.DataFrame, out: Path) -> None:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_feather(out)
        LOGGER.info("✅ Sauvegardé (feather) : %s", out)
    except Exception as e:
        raise RuntimeError("Écriture Feather requiert 'pyarrow'.") from e

def _ecrire_sqlite(df: pd.DataFrame, out: Path, table: str, unique_cols: Tuple[str, ...]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(out)
    try:
        cur = conn.cursor()
        # Création table si besoin (types simples)
        cols = df.columns.tolist()
        col_defs = []
        for c in cols:
            if c == "timestamp":
                col_defs.append(f"{c} TEXT NOT NULL")
            elif c in ("open","high","low","close","volume","price","amount"):
                col_defs.append(f"{c} REAL")
            else:
                col_defs.append(f"{c} TEXT")
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(col_defs)})")

        # Vérification des colonnes pour l'index unique
        missing = [c for c in unique_cols if c not in cols]
        if missing:
            raise ValueError(f"Colonnes manquantes pour l'index unique sur table '{table}': {missing}. "
                             f"Colonnes disponibles: {cols}. Adaptez 'unique_par' ou le schéma/table.")

        # Index unique
        unique_clause = ", ".join(unique_cols)
        cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_unique ON {table} ({unique_clause})")

        # UPSERT (SQLite ≥ 3.24) — convertit timestamp en ISO8601
        df_to = df.copy()
        if "timestamp" in df_to.columns:
            df_to["timestamp"] = pd.to_datetime(df_to["timestamp"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        update_list = ", ".join([f"{c}=excluded.{c}" for c in cols if c not in unique_cols])
        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT({unique_clause}) DO UPDATE SET {update_list}"
        cur.executemany(sql, df_to[cols].itertuples(index=False, name=None))
        conn.commit()
        LOGGER.info("✅ Sauvegardé (sqlite) : %s → table=%s (+%d lignes)", out, table, len(df))
    finally:
        conn.close()

def ecrire_dataframe(df: pd.DataFrame, sortie: ParametresSortie) -> None:
    if sortie.chemin is None:
        raise ValueError("ParametresSortie.chemin est requis")
    fmt = sortie.format
    out = sortie.chemin
    if fmt == "csv":
        if sortie.compression:
            df.to_csv(out, index=False, compression=sortie.compression)
            LOGGER.info("✅ Sauvegardé (csv,%s) : %s", sortie.compression, out)
        else:
            _atomic_write_csv(df, out)
    elif fmt == "parquet":
        _ecrire_parquet(df, out)
    elif fmt == "feather":
        _ecrire_feather(df, out)
    elif fmt == "sqlite":
        _ecrire_sqlite(df, out, sortie.table, sortie.unique_par)
    else:
        raise ValueError(f"Format inconnu : {fmt}")

def ecrire_dataframe_stream(df: pd.DataFrame, sortie: ParametresSortie) -> None:
    """
    Écriture adaptée au streaming pour éviter l'écrasement du fichier:
    - CSV: append
    - Parquet/Feather: création de fichiers 'part' tournants
    - SQLite: UPSERT (identique)
    """
    if sortie.chemin is None:
        raise ValueError("ParametresSortie.chemin est requis")
    fmt = sortie.format
    out = sortie.chemin
    if fmt == "csv":
        _append_csv(df, out)
    elif fmt in ("parquet", "feather"):
        # Fichier tournant pour conserver l'historique sans append complexe
        part = out.with_name(f"{out.stem}.part-{_utc_now_str()}{out.suffix}")
        if fmt == "parquet":
            _ecrire_parquet(df, part)
        else:
            _ecrire_feather(df, part)
        LOGGER.warning("Streaming %s: écrit en fichiers tournants (%s). Envisagez SQLite pour un upsert continu.", fmt, part.name)
    elif fmt == "sqlite":
        _ecrire_sqlite(df, out, sortie.table, sortie.unique_par)
    else:
        raise ValueError(f"Format inconnu : {fmt}")

# -------------------- Agrégation trades → OHLCV (streaming) ----------------

class AgregateurTradesOHLCV:
    """
    Construit des bougies OHLCV à partir d'un flux de trades.
    Stratégie : 'floor' du timestamp au seau (bucket) timeframe.
    - open = premier trade du bucket
    - high = max des prix
    - low  = min des prix
    - close = dernier trade
    - volume = somme des 'amount'
    """
    def __init__(self, timeframe: str, symbole: str, exclure_bougie_courante: bool = True):
        self.timeframe = timeframe
        self.symbole = symbole
        self.exclure_bougie_courante = exclure_bougie_courante
        self.pas = _timeframe_delta(timeframe)
        self.pas_ms = int(self.pas.total_seconds() * 1000)
        self._bougies: Dict[int, Dict[str, Any]] = {}  # bucket_ms -> ohlcv

    def _bucket_ms(self, ts_ms: int) -> int:
        return (ts_ms // self.pas_ms) * self.pas_ms

    def ingester_trades(self, trades: Iterable[Dict[str, Any]]) -> Tuple[pd.DataFrame, Optional[pd.DataFrame]]:
        """
        Ingère une liste de trades CCXT (dictionnaires) et retourne :
        - df_closed : DataFrame des bougies **closes** depuis le dernier appel
        - df_open   : DataFrame (taille 0 ou 1) de la bougie courante (si non exclue)
        """
        closed: List[List[Any]] = []
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        for t in trades:
            ts_ms = int(t["timestamp"])
            price = float(t["price"])
            amount = float(t.get("amount") or t.get("qty") or 0.0)
            bucket = self._bucket_ms(ts_ms)
            b = self._bougies.get(bucket)
            if b is None:
                self._bougies[bucket] = b = {
                    "timestamp": bucket,
                    "open": price, "high": price, "low": price, "close": price, "volume": 0.0,
                }
            else:
                b["high"] = max(b["high"], price)
                b["low"] = min(b["low"], price)
                b["close"] = price
            b["volume"] += amount

        # Déterminer les bougies closes (tout bucket < bucket(now))
        now_bucket = self._bucket_ms(now_ms)
        for bucket in sorted(list(self._bougies.keys())):
            if bucket < now_bucket:
                b = self._bougies.pop(bucket)
                closed.append([b["timestamp"], b["open"], b["high"], b["low"], b["close"], b["volume"]])

        df_closed = pd.DataFrame(closed, columns=["timestamp","open","high","low","close","volume"])
        if not df_closed.empty:
            df_closed["timestamp"] = pd.to_datetime(df_closed["timestamp"], unit="ms", utc=True)
            df_closed["symbole"] = self.symbole
            df_closed["timeframe"] = self.timeframe

        # Bougie courante (facultative)
        df_open = None
        if not self.exclure_bougie_courante and now_bucket in self._bougies:
            b = self._bougies[now_bucket]
            df_open = pd.DataFrame([[b["timestamp"], b["open"], b["high"], b["low"], b["close"], b["volume"]]],
                                   columns=["timestamp","open","high","low","close","volume"])
            df_open["timestamp"] = pd.to_datetime(df_open["timestamp"], unit="ms", utc=True)
            df_open["symbole"] = self.symbole
            df_open["timeframe"] = self.timeframe

        return df_closed, df_open

# -------------------------- Métriques & adaptatif --------------------------

@dataclass
class CompteurMetriques:
    messages: int = 0
    erreurs: int = 0
    reconnects: int = 0
    dernier_log: float = field(default_factory=lambda: time.time())

    def incr_msg(self, n: int = 1): self.messages += n
    def incr_err(self, n: int = 1): self.erreurs += n
    def incr_rec(self, n: int = 1): self.reconnects += n

    def maybe_log(self, interval_s: int = 60, prefix: str = "metrics"):
        now = time.time()
        if now - self.dernier_log >= interval_s:
            LOGGER.info("%s: messages=%d erreurs=%d reconnects=%d", prefix, self.messages, self.erreurs, self.reconnects)
            self.dernier_log = now

# ------------------------- Paramétrage principal ---------------------------

@dataclass
class ParametresEchange:
    id_exchange: str = "binance"
    gerer_rate_limit: bool = True
    timeout_ms: int = 30000
    sandbox: bool = False
    api_key: Optional[str] = None
    secret: Optional[str] = None
    password: Optional[str] = None
    uid: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)
    type_marche: str = "spot"
    sous_type: Optional[str] = None
    mode_marge: Optional[str] = None

@dataclass
class ParametresOHLCV:
    symbole: str
    timeframe: str = "1m"
    date_debut: Optional[str] = None
    date_fin: Optional[str] = None
    limite_par_requete: Optional[int] = None
    prix_ohlcv: Optional[str] = None
    exclure_bougie_courante: bool = True
    strict_bornes: bool = True
    reessais: int = 6
    backoff_initial_s: float = 0.6
    sortie: Optional[ParametresSortie] = None
    params_additionnels: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ParametresFlux:
    type_flux: Literal["ohlcv", "ticker", "orderbook", "trades"] = "ohlcv"
    symbole: str = "BTC/USDT"
    timeframe: str = "1m"
    prix_ohlcv: Optional[str] = None
    profondeur: Optional[int] = None
    params_additionnels: Dict[str, Any] = field(default_factory=dict)
    # exécution
    duree_max_s: Optional[int] = None
    max_messages: Optional[int] = None
    backoff_initial_s: float = 0.6
    reessais: int = 1000000
    # persistance
    sortie: Optional[ParametresSortie] = None
    flush_toutes_n: int = 20
    exclure_bougie_courante: bool = True
    # multisymboles
    symboles: Optional[List[str]] = None  # si défini, ignore "symbole" et utilise la liste
    # métriques
    intervalle_metrics_s: int = 60
    mode_trades_vers_ohlcv: bool = False  # si True et type_flux="trades", on agrège en OHLCV

# ------------------------------ Gestionnaire REST --------------------------

class GestionnaireEchangeCCXT:
    def __init__(self, cfg: ParametresEchange):
        try:
            cls = getattr(ccxt, cfg.id_exchange)
        except AttributeError as exc:
            raise ValueError(f"Exchange CCXT inconnu : {cfg.id_exchange!r}") from exc

        args = {
            "enableRateLimit": cfg.gerer_rate_limit,
            "timeout": cfg.timeout_ms,
            "options": dict(cfg.options or {}),
        }
        if cfg.api_key: args["apiKey"] = cfg.api_key
        if cfg.secret:  args["secret"] = cfg.secret
        if cfg.password: args["password"] = cfg.password
        if cfg.uid:      args["uid"] = cfg.uid

        self.exchange = cls(args)
        if cfg.sandbox and hasattr(self.exchange, "set_sandbox_mode"):
            try: self.exchange.set_sandbox_mode(True)
            except Exception as e: LOGGER.warning("Sandbox non supporté (%s): %s", cfg.id_exchange, e)

        self.preset = _get_preset(cfg.id_exchange)
        self._configurer_types_de_marche(cfg)

        try: self.exchange.load_markets()
        except Exception as exc:
            raise RuntimeError(f"Échec load_markets() pour {cfg.id_exchange}") from exc

    def _configurer_types_de_marche(self, cfg: ParametresEchange) -> None:
        opts = self.exchange.options if hasattr(self.exchange, "options") else {}
        changed = False
        if cfg.type_marche: 
            if opts.get("defaultType") != cfg.type_marche:
                opts["defaultType"] = cfg.type_marche; changed = True
        if cfg.sous_type:
            if opts.get("defaultSubType") != cfg.sous_type:
                opts["defaultSubType"] = cfg.sous_type; changed = True
        if cfg.mode_marge and self.preset.get("supports_margin_mode"):
            if opts.get("defaultMarginMode") != cfg.mode_marge:
                opts["defaultMarginMode"] = cfg.mode_marge; changed = True
        if changed: self.exchange.options = opts

    # Wrappers utiles
    def recuperer_marches(self): return self.exchange.fetch_markets()
    def recuperer_devises(self): return self.exchange.fetch_currencies()
    def telecharger_ticker(self, symbole): return self.exchange.fetch_ticker(symbole)
    def recuperer_livre_ordres(self, symbole, limite=50): return self.exchange.fetch_order_book(symbole, limit=limite)
    def recuperer_transactions(self, symbole, since=None, limite=1000): return self.exchange.fetch_trades(symbole, since=since, limit=limite)
    def recuperer_historique(self, symbole=None, since=None, limite=1000):
        if not self.exchange.has.get("fetchLedger"): raise NotImplementedError("fetch_ledger non supporté.")
        return self.exchange.fetch_ledger(symbol=symbole, since=since, limit=limite)
    def recuperer_mes_trades(self, symbole, since=None, limite=1000): return self.exchange.fetch_my_trades(symbole, since=since, limit=limite)
    def recuperer_solde(self): return self.exchange.fetch_balance()
    def recuperer_ordres_ouverts(self, symbole=None): return self.exchange.fetch_open_orders(symbol=symbole)
    def recuperer_ordres_fermes(self, symbole=None, since=None, limite=1000): return self.exchange.fetch_closed_orders(symbol=symbole, since=since, limit=limite)
    def passer_ordre(self, symbole, sens, montant, prix=None, type_ordre=None, params=None):
        ty = type_ordre or ("limit" if prix else "market")
        return self.exchange.create_order(symbole, ty, sens, montant, prix, params or {})
    def annuler_ordre(self, id_ordre, symbole): return self.exchange.cancel_order(id_ordre, symbole)
    def recuperer_positions(self): return self.exchange.fetch_positions() if self.exchange.has.get("fetchPositions") else []

    # REST OHLCV
    def _retry_fetch_ohlcv(self, symbole: str, timeframe: str, since_ms: Optional[int],
                           limite: int, reessais: int, backoff_initial_s: float,
                           params: Dict[str, Any]) -> List[List[Any]]:
        derniere: Optional[Exception] = None
        for t in range(reessais):
            try:
                return self.exchange.fetch_ohlcv(symbole, timeframe=timeframe, since=since_ms, limit=limite, params=params)
            except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable) as e:
                derniere = e
                pause = backoff_initial_s * (2 ** t) * random.uniform(0.85, 1.15)
                LOGGER.warning("fetch_ohlcv retry %d/%d après %.2fs (%s)", t + 1, reessais, pause, type(e).__name__)
                time.sleep(pause)
            except ccxt.BaseError as e:
                raise RuntimeError(f"Erreur CCXT fetch_ohlcv: {e}") from e
        raise RuntimeError(f"Échec fetch_ohlcv après {reessais} tentatives") from derniere

    def _verifier_timeframe_supporte(self, timeframe: str) -> None:
        tfs = getattr(self.exchange, "timeframes", None)
        if isinstance(tfs, dict) and tfs and timeframe not in tfs:
            raise ValueError(f"Timeframe {timeframe!r} non supporté par {self.exchange.id}. Timeframes: {sorted(tfs.keys())}")

    def _limite_effective(self, demande: Optional[int]) -> int:
        preset_lim = int(self.preset.get("ohlcv_limit_max", 1000))
        return int(min(demande or preset_lim, preset_lim))

    def telecharger_ohlcv(self, p: ParametresOHLCV) -> pd.DataFrame:
        self._verifier_timeframe_supporte(p.timeframe)

        debut_dt = _parse_date_utc(p.date_debut)
        fin_dt = _parse_date_utc(p.date_fin)
        if fin_dt and debut_dt and fin_dt <= debut_dt:
            raise ValueError("date_fin doit être strictement postérieure à date_debut.")

        pas = _timeframe_delta(p.timeframe)
        pas_ms = int(pas.total_seconds() * 1000)

        # Reprise sur sortie existante si CSV/Parquet/Feather : lit pour trouver dernier ts
        df_exist: Optional[pd.DataFrame] = None
        reprise_since_ms: Optional[int] = None
        if p.sortie and p.sortie.chemin and p.sortie.chemin.exists() and p.sortie.format in ("csv","parquet","feather"):
            try:
                if p.sortie.format == "csv":
                    df_exist = pd.read_csv(p.sortie.chemin, parse_dates=["timestamp"])
                elif p.sortie.format == "parquet":
                    df_exist = pd.read_parquet(p.sortie.chemin)
                elif p.sortie.format == "feather":
                    df_exist = pd.read_feather(p.sortie.chemin)
                if df_exist is not None and not df_exist.empty and "timestamp" in df_exist.columns:
                    df_exist["timestamp"] = pd.to_datetime(df_exist["timestamp"], utc=True)
                    dernier_ts = int(df_exist["timestamp"].max().timestamp() * 1000)
                    reprise_since_ms = dernier_ts + pas_ms
                    LOGGER.info("Reprise depuis : %s (dernier=%s)", p.sortie.chemin, df_exist["timestamp"].max())
            except Exception as e:
                LOGGER.warning("Lecture fichier existant impossible, reprise ignorée : %s", e)

        since_ms = reprise_since_ms if reprise_since_ms is not None else (int(debut_dt.timestamp()*1000) if debut_dt else None)
        until_ms: Optional[int] = int(fin_dt.timestamp()*1000) if fin_dt else None

        limite = self._limite_effective(p.limite_par_requete)
        morceaux: List[pd.DataFrame] = []
        if df_exist is not None:
            morceaux.append(df_exist)

        curseur = since_ms
        while True:
            params_requete = build_params_ohlcv_for_exchange(self.exchange, self.preset, p.prix_ohlcv, until_ms, p.params_additionnels)
            page = self._retry_fetch_ohlcv(p.symbole, p.timeframe, curseur, limite, p.reessais, p.backoff_initial_s, params_requete)
            if not page: break

            df_page = pd.DataFrame(page, columns=["timestamp","open","high","low","close","volume"])
            df_page["timestamp"] = pd.to_datetime(df_page["timestamp"], unit="ms", utc=True)

            if fin_dt is not None:
                df_page = df_page[df_page["timestamp"] < fin_dt]
            if df_page.empty: break

            # Ajoute colonnes id pour stockage
            df_page["symbole"] = p.symbole
            df_page["timeframe"] = p.timeframe

            morceaux.append(df_page)

            dernier_ms = int(df_page["timestamp"].iloc[-1].timestamp() * 1000)
            curseur = dernier_ms + 1
            if len(page) < limite: break
            if until_ms is not None and curseur >= until_ms: break

        df = _concat_dedup(morceaux, subset=("timestamp","symbole","timeframe")) if morceaux else pd.DataFrame(
            columns=["timestamp","open","high","low","close","volume","symbole","timeframe"]
        )

        if p.exclure_bougie_courante and not df.empty:
            now = datetime.now(timezone.utc) - pas
            df = df[df["timestamp"] <= now]

        if p.strict_bornes:
            if debut_dt: df = df[df["timestamp"] >= debut_dt]
            if fin_dt:   df = df[df["timestamp"] < fin_dt]

        if p.sortie:
            ecrire_dataframe(df, p.sortie)

        return df

    def nom_fichier(self, base: str, extension: str = "csv") -> Path:
        return DOSSIER_DONNEES / f"{self.exchange.id}_{base}_{_utc_now_str()}.{extension}"

# ------------------------------ Gestionnaire PRO ---------------------------

class GestionnaireEchangeCCXTPro:
    def __init__(self, cfg: ParametresEchange):
        if not HAS_CCXTPRO:
            raise RuntimeError("ccxt.pro indisponible: mise à jour 'ccxt' requise (import ccxt.pro).")
        try:
            cls = getattr(ccxtpro, cfg.id_exchange)
        except AttributeError as exc:
            raise ValueError(f"Exchange ccxt.pro inconnu: {cfg.id_exchange!r}") from exc

        args = {
            "enableRateLimit": cfg.gerer_rate_limit,
            "timeout": cfg.timeout_ms,
            "options": dict(cfg.options or {}),
        }
        if cfg.api_key: args["apiKey"] = cfg.api_key
        if cfg.secret:  args["secret"] = cfg.secret
        if cfg.password: args["password"] = cfg.password
        if cfg.uid:      args["uid"] = cfg.uid

        self.exchange = cls(args)
        self.preset = _get_preset(cfg.id_exchange)
        self.cfg = cfg

        # Sandbox si demandé
        if cfg.sandbox and hasattr(self.exchange, "set_sandbox_mode"):
            try:
                self.exchange.set_sandbox_mode(True)
            except Exception as e:
                LOGGER.warning("Sandbox pro non supporté (%s): %s", cfg.id_exchange, e)

        # Options marché
        opts = self.exchange.options if hasattr(self.exchange, "options") else {}
        if cfg.type_marche: opts["defaultType"] = cfg.type_marche
        if cfg.sous_type:   opts["defaultSubType"] = cfg.sous_type
        if cfg.mode_marge and self.preset.get("supports_margin_mode"):
            opts["defaultMarginMode"] = cfg.mode_marge
        self.exchange.options = opts

    async def _run_loop(self, fetch_fn, handle_fn, p: ParametresFlux, met: CompteurMetriques):
        debut = time.time()
        essais = 0
        loaded_markets = False
        while True:
            if p.duree_max_s and (time.time() - debut) >= p.duree_max_s: break
            try:
                async with self.exchange as ex:
                    # Charger les marchés une fois (certaines bourses l'exigent)
                    if not loaded_markets:
                        try:
                            await ex.load_markets()
                        except Exception as e:
                            LOGGER.warning("load_markets (pro) optionnel/échoué: %s", e)
                        loaded_markets = True
                    while True:
                        if p.duree_max_s and (time.time() - debut) >= p.duree_max_s: return
                        data = await fetch_fn(ex)
                        await handle_fn(data)
                        met.incr_msg()
                        met.maybe_log(interval_s=p.intervalle_metrics_s, prefix=f"{self.exchange.id}:{p.type_flux}")
                        if p.max_messages and met.messages >= p.max_messages: return
            except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable, ccxt.InvalidNonce) as e:
                met.incr_err(); met.incr_rec()
                essais += 1
                pause = p.backoff_initial_s * (2 ** min(essais, 8)) * random.uniform(0.85, 1.15)
                LOGGER.warning("WebSocket reconnect dans %.2fs (%s)", pause, type(e).__name__)
                await asyncio.sleep(pause)
                continue
            except Exception as e:
                met.incr_err()
                LOGGER.error("Flux stoppé (irréversible): %s", e)
                raise

    # -------------------- Flux concrets (mono-symbole) --------------------

    async def flux_ohlcv(self, p: ParametresFlux):
        met = CompteurMetriques()
        pas = _timeframe_delta(p.timeframe)
        tampon: List[pd.DataFrame] = []
        dernier_ts_ms: Optional[int] = None

        def params_ws():
            return build_params_ohlcv_for_exchange(self.exchange, self.preset, p.prix_ohlcv, None, p.params_additionnels)

        async def handle(batch):
            nonlocal tampon, dernier_ts_ms
            df = pd.DataFrame(batch, columns=["timestamp","open","high","low","close","volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            if p.exclure_bougie_courante and not df.empty:
                df = df[df["timestamp"] <= (datetime.now(timezone.utc) - pas)]
            if dernier_ts_ms is not None and not df.empty:
                df = df[(df["timestamp"].astype("int64") // 10**6) > dernier_ts_ms]
            if df.empty: return
            df["symbole"] = p.symbole
            df["timeframe"] = p.timeframe
            dernier_ts_ms = int(df["timestamp"].iloc[-1].timestamp()*1000)
            tampon.append(df)
            if p.sortie and sum(len(x) for x in tampon) >= p.flush_toutes_n:
                ecrire_dataframe_stream(_concat_dedup(tampon, subset=("timestamp","symbole","timeframe")), p.sortie)
                tampon = []

        async def fetch(ex): return await ex.watch_ohlcv(p.symbole, p.timeframe, None, None, params_ws())
        await self._run_loop(fetch, handle, p, met)
        if p.sortie and tampon:
            ecrire_dataframe_stream(_concat_dedup(tampon, subset=("timestamp","symbole","timeframe")), p.sortie)

    async def flux_trades(self, p: ParametresFlux):
        met = CompteurMetriques()
        tampon_trades: List[Dict[str, Any]] = []
        agreg = AgregateurTradesOHLCV(p.timeframe, p.symbole, exclure_bougie_courante=p.exclure_bougie_courante) if p.mode_trades_vers_ohlcv else None

        # Déduplication des trades (borne mémoire)
        seen_keys: set[str] = set()
        seen_order: deque[str] = deque(maxlen=50000)
        def _trade_key(t: Dict[str, Any]) -> str:
            tid = t.get("id")
            if tid is not None:
                return f"{p.symbole}:{tid}"
            return f"{p.symbole}:{t.get('timestamp')}:{t.get('side')}:{t.get('price')}:{t.get('amount')}"

        def _mark_seen(keys: List[str]) -> List[bool]:
            results = []
            for k in keys:
                if k in seen_keys:
                    results.append(True)
                else:
                    seen_keys.add(k)
                    seen_order.append(k)
                    results.append(False)
                    # purge si dépasse capacité
                    if len(seen_keys) > seen_order.maxlen:
                        old = seen_order.popleft()
                        seen_keys.discard(old)
            return results

        async def handle(trades):
            nonlocal tampon_trades
            if isinstance(trades, dict):
                trades = [trades]
            # Dédup brute
            keys = [_trade_key(t) for t in trades]
            is_dup = _mark_seen(keys)
            trades = [t for t, d in zip(trades, is_dup) if not d]
            if not trades:
                return

            # Normalisation + stockage
            norm = []
            for t in trades:
                norm.append({
                    "timestamp": datetime.fromtimestamp(t["timestamp"]/1000, tz=timezone.utc),
                    "price": t.get("price"),
                    "amount": t.get("amount"),
                    "side": t.get("side"),
                    "id": t.get("id"),
                    "symbole": p.symbole,
                })

            if p.mode_trades_vers_ohlcv and agreg is not None:
                df_closed, _ = agreg.ingester_trades(trades)
                if df_closed is not None and not df_closed.empty:
                    if p.sortie:
                        ecrire_dataframe_stream(df_closed, p.sortie)
            elif p.sortie:
                tampon_trades.extend(norm)
                if len(tampon_trades) >= p.flush_toutes_n:
                    df = pd.DataFrame(tampon_trades)
                    ecrire_dataframe_stream(df, p.sortie)
                    tampon_trades = []

        async def fetch(ex): return await ex.watch_trades(p.symbole, None, None, p.params_additionnels)
        await self._run_loop(fetch, handle, p, met)
        if p.sortie and tampon_trades:
            df = pd.DataFrame(tampon_trades)
            ecrire_dataframe_stream(df, p.sortie)

    async def flux_orderbook(self, p: ParametresFlux):
        met = CompteurMetriques()
        async def handle(ob):
            bid = ob["bids"][0][0] if ob.get("bids") else None
            ask = ob["asks"][0][0] if ob.get("asks") else None
            LOGGER.info("orderbook %s: best bid=%s ask=%s", p.symbole, bid, ask)
        async def fetch(ex): return await ex.watch_order_book(p.symbole, p.profondeur, p.params_additionnels)
        await self._run_loop(fetch, handle, p, met)

    async def flux_ticker(self, p: ParametresFlux):
        met = CompteurMetriques()
        async def handle(ticker): LOGGER.info("ticker %s: %s", p.symbole, ticker.get("last"))
        async def fetch(ex): return await ex.watch_ticker(p.symbole, p.params_additionnels)
        await self._run_loop(fetch, handle, p, met)

    # ---------------------- Multi-symboles (si supporté) -------------------

    async def flux_multisymboles(self, p: ParametresFlux):
        """
        Utilise watch_*_for_symbols si dispo, sinon crée un Task par symbole (instance exchange séparée).
        """
        if not p.symboles:
            raise ValueError("symboles non fournis pour flux multisymboles.")
        met = CompteurMetriques()

        # Détermine la méthode groupée si présente
        method_map = {
            "ohlcv": "watch_ohlcv_for_symbols",
            "trades": "watch_trades_for_symbols",
            "ticker": "watch_ticker_for_symbols",
            "orderbook": "watch_order_book_for_symbols",
        }
        grouped_fn = method_map.get(p.type_flux)
        has_group = hasattr(self.exchange, grouped_fn) if grouped_fn else False

        if has_group:
            # Gestion groupée (un seul fetch, handler distribue)
            agreg_map: Dict[str, AgregateurTradesOHLCV] = {}
            pas = _timeframe_delta(p.timeframe) if p.type_flux == "ohlcv" else None
            dernier_ts_ms: Dict[str, int] = {}
            # Dédup trades par symbole
            seen_map: Dict[str, Tuple[set, deque]] = {}

            def params_ws():
                return p.params_additionnels or {}

            async def handle(batch_map):
                # batch_map: dict symbole -> payload
                for symb, payload in batch_map.items():
                    if p.type_flux == "ohlcv":
                        df = pd.DataFrame(payload, columns=["timestamp","open","high","low","close","volume"])
                        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                        if p.exclure_bougie_courante and not df.empty:
                            df = df[df["timestamp"] <= (datetime.now(timezone.utc) - pas)]
                        last = dernier_ts_ms.get(symb)
                        if last is not None and not df.empty:
                            df = df[(df["timestamp"].astype("int64") // 10**6) > last]
                        if df.empty: continue
                        df["symbole"] = symb
                        df["timeframe"] = p.timeframe
                        dernier_ts_ms[symb] = int(df["timestamp"].iloc[-1].timestamp()*1000)
                        if p.sortie: ecrire_dataframe_stream(df, p.sortie)

                    elif p.type_flux == "trades":
                        # Initialiser structures de dédup
                        if symb not in seen_map:
                            seen_map[symb] = (set(), deque(maxlen=50000))
                        seen_keys, seen_order = seen_map[symb]

                        def _key(t):
                            tid = t.get("id")
                            if tid is not None:
                                return f"{symb}:{tid}"
                            return f"{symb}:{t.get('timestamp')}:{t.get('side')}:{t.get('price')}:{t.get('amount')}"

                        def _mark_seen(keys: List[str]) -> List[bool]:
                            results = []
                            for k in keys:
                                if k in seen_keys:
                                    results.append(True)
                                else:
                                    seen_keys.add(k)
                                    seen_order.append(k)
                                    results.append(False)
                                    if len(seen_keys) > seen_order.maxlen:
                                        old = seen_order.popleft()
                                        seen_keys.discard(old)
                            return results

                        # Normalisation / agrégation
                        if isinstance(payload, dict):
                            payload = [payload]

                        keys = [_key(t) for t in payload]
                        is_dup = _mark_seen(keys)
                        payload = [t for t, d in zip(payload, is_dup) if not d]
                        if not payload:
                            continue

                        if p.mode_trades_vers_ohlcv:
                            agr = agreg_map.get(symb) or AgregateurTradesOHLCV(p.timeframe, symb, p.exclure_bougie_courante)
                            agreg_map[symb] = agr
                            df_closed, _ = agr.ingester_trades(payload)
                            if p.sortie and df_closed is not None and not df_closed.empty:
                                ecrire_dataframe_stream(df_closed, p.sortie)
                        else:
                            norm = []
                            for t in payload:
                                norm.append({
                                    "timestamp": datetime.fromtimestamp(t["timestamp"]/1000, tz=timezone.utc),
                                    "price": t.get("price"),
                                    "amount": t.get("amount"),
                                    "side": t.get("side"),
                                    "id": t.get("id"),
                                    "symbole": symb,
                                })
                            if p.sortie and norm:
                                ecrire_dataframe_stream(pd.DataFrame(norm), p.sortie)
                    elif p.type_flux == "ticker":
                        if isinstance(payload, dict):
                            LOGGER.info("ticker %s: %s", symb, payload.get("last"))
                        else:
                            LOGGER.info("ticker %s: %s", symb, payload)
                    elif p.type_flux == "orderbook":
                        if isinstance(payload, dict):
                            bid = payload["bids"][0][0] if payload.get("bids") else None
                            ask = payload["asks"][0][0] if payload.get("asks") else None
                            LOGGER.info("orderbook %s: best bid=%s ask=%s", symb, bid, ask)

            async def fetch(ex):
                method = getattr(ex, grouped_fn)
                if p.type_flux == "ohlcv":
                    return await method(p.symboles, p.timeframe, None, None, params_ws())
                elif p.type_flux == "trades":
                    return await method(p.symboles, None, None, params_ws())
                elif p.type_flux == "orderbook":
                    return await method(p.symboles, p.profondeur, params_ws())
                elif p.type_flux == "ticker":
                    return await method(p.symboles, params_ws())

            await self._run_loop(fetch, handle, p, met)
        else:
            # Pas de méthode groupée → tâches parallèles avec instances ccxt.pro séparées
            async def run_one(symb: str):
                sous_p = ParametresFlux(**{**p.__dict__, "symbole": symb, "symboles": None})
                # Créer une instance dédiée pour éviter les conflits de contexte
                pro_i = GestionnaireEchangeCCXTPro(self.cfg)
                if p.type_flux == "ohlcv":
                    await pro_i.flux_ohlcv(sous_p)
                elif p.type_flux == "trades":
                    await pro_i.flux_trades(sous_p)
                elif p.type_flux == "ticker":
                    await pro_i.flux_ticker(sous_p)
                elif p.type_flux == "orderbook":
                    await pro_i.flux_orderbook(sous_p)

            await asyncio.gather(*(run_one(s) for s in p.symboles))

# ---------------------------------- CLI -----------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CCXT (FR) v2 — REST & ccxt.pro avec sorties avancées")
    # Exchange
    p.add_argument("--exchange", default="binance")
    p.add_argument("--sandbox", action="store_true")
    p.add_argument("--timeout", type=int, default=30000)
    p.add_argument("--options", type=str, default="{}")
    p.add_argument("--type-marche", default="spot", choices=["spot","swap","future","margin","option"])
    p.add_argument("--sous-type", default=None)
    p.add_argument("--mode-marge", default=None)
    # Credentials
    p.add_argument("--api-key", default=None)
    p.add_argument("--secret", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--uid", default=None)

    # REST OHLCV
    p.add_argument("--symbole", default=None, help="Symbole unique (REST/stream mono).")
    p.add_argument("--symbols", default=None, help="Liste de symboles séparés par des virgules (stream multi).")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--date-debut", dest="date_debut", default=None)
    p.add_argument("--date-fin", dest="date_fin", default=None)
    p.add_argument("--limite", type=int, default=None)
    p.add_argument("--prix-ohlcv", dest="prix_ohlcv", default=None)
    p.add_argument("--no-exclure-bougie-courante", action="store_true")
    p.add_argument("--no-bornes-strictes", action="store_true")
    p.add_argument("--params", default="{}")

    # Sortie
    p.add_argument("--format", default="csv", choices=["csv","parquet","feather","sqlite"])
    p.add_argument("--sortie", type=Path, default=None, help="Chemin fichier de sortie (CSV/Parquet/Feather/SQLite).")
    p.add_argument("--sqlite-table", default="ohlcv")
    p.add_argument("--compression", default=None, help="CSV: gzip, bz2, zip, xz")

    # Streaming (ccxt.pro)
    p.add_argument("--stream", choices=["ohlcv","ticker","orderbook","trades"], default=None)
    p.add_argument("--duree", type=int, default=None)
    p.add_argument("--flush", type=int, default=20)
    p.add_argument("--depth", type=int, default=None)
    p.add_argument("--metrics-interval", type=int, default=60)
    p.add_argument("--trades-vers-ohlcv", action="store_true", help="Agrège trades en OHLCV en live (stream trades).")
    return p

def main_cli() -> int:
    parser = _parser()
    args = parser.parse_args()

    # Parse JSON
    try:
        options = json.loads(args.options) if args.options else {}
        params_add = json.loads(args.params) if args.params else {}
        if not isinstance(options, dict) or not isinstance(params_add, dict):
            raise ValueError("Les champs --options et --params doivent être des objets JSON.")
    except Exception as e:
        LOGGER.error("JSON invalide pour --options/--params : %s", e)
        return 2

    cfg = ParametresEchange(
        id_exchange=args.exchange, gerer_rate_limit=True, timeout_ms=args.timeout, sandbox=bool(args.sandbox),
        api_key=args.api_key, secret=args.secret, password=args.password, uid=args.uid,
        options=options, type_marche=args.type_marche, sous_type=args.sous_type, mode_marge=args.mode_marge,
    )

    # Construction de la sortie
    if args.sortie is not None:
        sortie = ParametresSortie(format=args.format, chemin=args.sortie, table=args.sqlite_table, compression=args.compression)
    else:
        # Nom auto
        base = "flux" if args.stream else f"ohlcv_{(args.symbole or 'multi').replace('/','-').replace(':','-')}_{args.timeframe}"
        ext = {"csv":"csv","parquet":"parquet","feather":"feather","sqlite":"sqlite"}[args.format]
        chemin_auto = DOSSIER_DONNEES / f"{args.exchange}_{base}_{_utc_now_str()}.{ext}"
        sortie = ParametresSortie(format=args.format, chemin=chemin_auto, table=args.sqlite_table, compression=args.compression)

    exclure_bougie = not args.no_exclure_bougie_courante
    strict_bornes = not args.no_bornes_strictes

    # Mode streaming ?
    if args.stream:
        if not HAS_CCXTPRO:
            LOGGER.error("ccxt.pro non disponible dans votre installation. Mettez à jour 'ccxt'.")
            return 1
        try:
            pro = GestionnaireEchangeCCXTPro(cfg)
        except Exception as e:
            LOGGER.error("Initialisation ccxt.pro échouée : %s", e)
            return 1

        symboles = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
        pf = ParametresFlux(
            type_flux=args.stream,
            symbole=args.symbole or (symboles[0] if symboles else None),
            timeframe=args.timeframe,
            prix_ohlcv=args.prix_ohlcv,
            profondeur=args.depth,
            params_additionnels=params_add,
            duree_max_s=args.duree,
            sortie=sortie,
            flush_toutes_n=args.flush,
            exclure_bougie_courante=exclure_bougie,
            symboles=symboles,
            intervalle_metrics_s=args.metrics_interval,
            mode_trades_vers_ohlcv=bool(args.trades_vers_ohlcv),
        )

        try:
            if pf.symboles:
                asyncio.run(pro.flux_multisymboles(pf))
            else:
                if pf.type_flux == "ohlcv":
                    asyncio.run(pro.flux_ohlcv(pf))
                elif pf.type_flux == "trades":
                    asyncio.run(pro.flux_trades(pf))
                elif pf.type_flux == "ticker":
                    asyncio.run(pro.flux_ticker(pf))
                elif pf.type_flux == "orderbook":
                    asyncio.run(pro.flux_orderbook(pf))
            return 0
        except KeyboardInterrupt:
            LOGGER.warning("Interrompu.")
            return 130
        except Exception as e:
            LOGGER.error("Flux arrêté avec erreur : %s", e)
            return 1

    # Sinon, mode REST (OHLCV)
    if not args.symbole:
        LOGGER.error("--symbole requis en mode REST (sans --stream).")
        return 2

    try:
        gestionnaire = GestionnaireEchangeCCXT(cfg)
    except Exception as e:
        LOGGER.error("Initialisation exchange échouée : %s", e)
        return 1

    params_ohlcv = ParametresOHLCV(
        symbole=args.symbole, timeframe=args.timeframe,
        date_debut=args.date_debut, date_fin=args.date_fin, limite_par_requete=args.limite,
        prix_ohlcv=args.prix_ohlcv, exclure_bougie_courante=exclure_bougie, strict_bornes=strict_bornes,
        sortie=sortie, params_additionnels=params_add,
    )

    try:
        gestionnaire.telecharger_ohlcv(params_ohlcv)
        return 0
    except KeyboardInterrupt:
        LOGGER.warning("Interrompu par l'utilisateur.")
        return 130
    except Exception as e:
        LOGGER.error("Échec OHLCV : %s", e)
        return 1

if __name__ == "__main__":
    sys.exit(main_cli())
