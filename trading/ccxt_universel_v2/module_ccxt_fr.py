#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
module_ccxt_fr.py — Base universelle CCXT (FR) + CCXT Pro (WebSockets)

Objectifs
---------
- Fonctionne avec *tous* les exchanges CCXT (spot / futures / swap / margin / options si supportés).
- Respect des spécificités : limites de pagination, timeframes disponibles, paramètres 'price' (mark/index/last),
  paramètres d'intervalle (until / endTime / to), types de marchés (defaultType), modes de marge (isolated/cross),
  sous-types (linear/inverse) via exchange.options si applicable.
- Téléchargement OHLCV robuste, idempotent, borné par dates, reprise sur CSV existant, exclusion de la bougie incomplète.
- Mode **streaming** via ccxt.pro (inclus dans le paquet `ccxt`) : watch_ohlcv / watch_ticker / watch_order_book / watch_trades
  avec reconnexion/backoff + jitter et persistance optionnelle.
- Backoff exponentiel + jitter, écriture atomique, déduplication fiable sur 'timestamp'.

Dépendances : ccxt, pandas
Compatibilité : Python 3.9+
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal

import ccxt
import pandas as pd

# Import optionnel de ccxt.pro (inclu dans ccxt récents)
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
LOGGER = logging.getLogger("module_ccxt_fr")

# ------------------------------ Stockage ----------------------------------

DOSSIER_DONNEES = Path(__file__).parent / "donnees"
DOSSIER_DONNEES.mkdir(parents=True, exist_ok=True)

# ------------------------- Préréglages d'exchanges ------------------------
# Valeurs par défaut *sécurisées* si non listé : limite 1000, pas de param 'price', pas de 'until'.
# Étendez ce registre selon les besoins sans toucher au reste du code.
EXCHANGE_PRESETS: Dict[str, Dict[str, Any]] = {
    # Binance
    "binance": {
        "ohlcv_limit_max": 1000,
        "until_param": "endTime",   # ms
        "price_param": None,        # pas de 'price' pour mark/index via unified OHLCV
        "supports_margin_mode": True,
    },
    # Bybit
    "bybit": {
        "ohlcv_limit_max": 1000,
        "until_param": None,        # since+limit suffisent souvent
        "price_param": "price",     # "mark" | "index" | "last"
        "price_values": {"mark", "index", "last"},
        "supports_margin_mode": True,
    },
    # OKX
    "okx": {
        "ohlcv_limit_max": 300,     # OKX retourne souvent 300 par page
        "until_param": "to",        # ms
        "price_param": "price",     # "mark" | "index"
        "price_values": {"mark", "index"},
        "supports_margin_mode": True,
    },
    # KuCoin
    "kucoin": {
        "ohlcv_limit_max": 1500,
        "until_param": None,
        "price_param": None,
        "supports_margin_mode": True,
    },
    # Kraken
    "kraken": {
        "ohlcv_limit_max": 720,     # dépend du timeframe ; valeur prudente
        "until_param": None,
        "price_param": None,
        "supports_margin_mode": False,
    },
    # Gate.io
    "gateio": {
        "ohlcv_limit_max": 1000,
        "until_param": "to",        # secondes
        "until_in_seconds": True,   # particularité d'unité pour certains endpoints
        "price_param": None,
        "supports_margin_mode": True,
    },
    # Fallback (s'applique à tout exchange non listé explicitement)
    "*": {
        "ohlcv_limit_max": 1000,
        "until_param": None,
        "price_param": None,
        "supports_margin_mode": False,
    },
}

# ------------------------------- Helpers ----------------------------------


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_date_utc(txt: Optional[str]) -> Optional[datetime]:
    if not txt:
        return None
    dt = datetime.fromisoformat(txt)
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


def _concat_dedup(df_list: List[pd.DataFrame]) -> pd.DataFrame:
    if not df_list:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = pd.concat(df_list, ignore_index=True)
    df.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _get_preset(exchange_id: str) -> Dict[str, Any]:
    return EXCHANGE_PRESETS.get(exchange_id, EXCHANGE_PRESETS["*"])


def build_params_ohlcv_for_exchange(
    exchange: Any,
    preset: Dict[str, Any],
    prix_ohlcv: Optional[str],
    until_ms: Optional[int],
    params_add: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Construit les params CCXT pour fetch_ohlcv/watch_ohlcv en respectant
    les spécificités exchange (clé 'price', 'until'/'endTime'/'to', unités).
    """
    params: Dict[str, Any] = dict(params_add or {})

    # Paramètre 'price' si l'exchange le supporte
    if prix_ohlcv:
        pkey = preset.get("price_param")
        pvals = preset.get("price_values")
        if pkey:
            if pvals and prix_ohlcv not in pvals:
                raise ValueError(f"prix_ohlcv={prix_ohlcv!r} non supporté (attendu ∈ {pvals})")
            params[pkey] = prix_ohlcv
        else:
            LOGGER.warning("Paramètre 'prix_ohlcv' ignoré : non supporté par %s", getattr(exchange, 'id', 'exchange'))

    # Paramètre 'until'/'endTime'/'to' si configuré
    if until_ms is not None:
        ukey = preset.get("until_param")
        if ukey:
            if preset.get("until_in_seconds"):
                params[ukey] = int(until_ms / 1000)
            else:
                params[ukey] = until_ms
    return params


# ------------------------- Paramétrage principal --------------------------

@dataclass
class ParametresEchange:
    id_exchange: str = "binance"
    gerer_rate_limit: bool = True
    timeout_ms: int = 30000
    sandbox: bool = False
    # Credentials (optionnels)
    api_key: Optional[str] = None
    secret: Optional[str] = None
    password: Optional[str] = None
    uid: Optional[str] = None
    # options ccxt spécifiques (JSON -> dict)
    options: Dict[str, Any] = field(default_factory=dict)
    # Types de marchés
    type_marche: str = "spot"   # "spot" | "swap" | "future" | "margin" | "option"
    sous_type: Optional[str] = None  # "linear" | "inverse" (si supporté)
    mode_marge: Optional[str] = None  # "isolated" | "cross" (si supporté)


@dataclass
class ParametresOHLCV:
    symbole: str
    timeframe: str = "1m"
    date_debut: Optional[str] = None      # ISO8601
    date_fin: Optional[str] = None        # ISO8601 (exclue)
    limite_par_requete: Optional[int] = None  # si None, utilise le preset
    prix_ohlcv: Optional[str] = None      # "mark" | "index" | "last" si supporté
    exclure_bougie_courante: bool = True
    strict_bornes: bool = True
    reessais: int = 6
    backoff_initial_s: float = 0.6
    chemin_sortie: Optional[Path] = None
    params_additionnels: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParametresFlux:
    """Paramétrage d’un flux WebSocket via ccxt.pro"""
    type_flux: Literal["ohlcv", "ticker", "orderbook", "trades"] = "ohlcv"
    symbole: str = "BTC/USDT"
    timeframe: str = "1m"                 # pour ohlcv
    prix_ohlcv: Optional[str] = None      # mark|index|last si supporté
    profondeur: Optional[int] = None      # pour orderbook
    params_additionnels: Dict[str, Any] = field(default_factory=dict)

    # Contrôle d’exécution
    duree_max_s: Optional[int] = None     # stop après N secondes
    max_messages: Optional[int] = None    # stop après N messages
    backoff_initial_s: float = 0.6        # reconnexion exponentielle + jitter
    reessais: int = 1000000               # "infini"

    # Persistance
    chemin_sortie: Optional[Path] = None  # CSV: ohlcv ou trades
    flush_toutes_n: int = 20              # flush disque tous les N événements
    exclure_bougie_courante: bool = True  # ohlcv: n’écrit que les bougies closes


# ------------------------------ Gestionnaire REST -------------------------

class GestionnaireEchangeCCXT:
    """
    Enveloppe universelle CCXT orientée production (FR).
    """

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
        # Credentials si fournis
        if cfg.api_key: args["apiKey"] = cfg.api_key
        if cfg.secret:  args["secret"] = cfg.secret
        if cfg.password: args["password"] = cfg.password
        if cfg.uid:      args["uid"] = cfg.uid

        self.exchange = cls(args)

        if cfg.sandbox and hasattr(self.exchange, "set_sandbox_mode"):
            try:
                self.exchange.set_sandbox_mode(True)
            except Exception as e:
                LOGGER.warning("Sandbox non supporté (%s): %s", cfg.id_exchange, e)

        self.preset = _get_preset(cfg.id_exchange)
        self._configurer_types_de_marche(cfg)

        try:
            self.exchange.load_markets()
        except Exception as exc:
            raise RuntimeError(f"Échec load_markets() pour {cfg.id_exchange}") from exc

    # ----------------------- Configuration des marchés ---------------------

    def _configurer_types_de_marche(self, cfg: ParametresEchange) -> None:
        """
        Configure exchange.options en fonction de type_marche / sous_type / mode_marge.
        N'affecte que si l'exchange supporte ces notions via options.
        """
        opts = self.exchange.options if hasattr(self.exchange, "options") else {}
        changed = False

        # defaultType (spot/swap/future/margin/option)
        if cfg.type_marche and isinstance(opts, dict):
            if "defaultType" not in opts or opts.get("defaultType") != cfg.type_marche:
                opts["defaultType"] = cfg.type_marche
                changed = True

        # Sous-type (linear/inverse)
        if cfg.sous_type:
            if opts.get("defaultSubType") != cfg.sous_type:
                opts["defaultSubType"] = cfg.sous_type
                changed = True

        # Mode de marge (isolated/cross) si supporté
        if cfg.mode_marge and self.preset.get("supports_margin_mode"):
            if "defaultMarginMode" not in opts or opts.get("defaultMarginMode") != cfg.mode_marge:
                opts["defaultMarginMode"] = cfg.mode_marge
                changed = True

        if changed:
            self.exchange.options = opts

    # ------------------------------ Wrappers -------------------------------

    def executer_commande(self, commande: str, *args, **kwargs):
        if not hasattr(self.exchange, commande):
            raise AttributeError(f"La commande '{commande}' n'existe pas pour {self.exchange.id}.")
        return getattr(self.exchange, commande)(*args, **kwargs)

    def recuperer_marches(self): return self.exchange.fetch_markets()
    def recuperer_devises(self): return self.exchange.fetch_currencies()
    def telecharger_ticker(self, symbole): return self.exchange.fetch_ticker(symbole)
    def recuperer_livre_ordres(self, symbole, limite=50): return self.exchange.fetch_order_book(symbole, limit=limite)
    def recuperer_transactions(self, symbole, since=None, limite=1000): return self.exchange.fetch_trades(symbole, since=since, limit=limite)
    def recuperer_historique(self, symbole=None, since=None, limite=1000):
        if not self.exchange.has.get("fetchLedger"):
            raise NotImplementedError("fetch_ledger non supporté.")
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
    def recuperer_depots(self, since=None, limite=1000): return self.exchange.fetch_deposits(since=since, limit=limite)
    def recuperer_retraits(self, since=None, limite=1000): return self.exchange.fetch_withdrawals(since=since, limit=limite)
    def recuperer_adresse_depot(self, devise, params=None): return self.exchange.fetch_deposit_address(devise, params)
    def effectuer_retrait(self, devise, montant, adresse, tag=None, params=None): return self.exchange.withdraw(devise, montant, adresse, tag, params or {})

    # ---------------------------- OHLCV robuste ----------------------------

    def _retry_fetch_ohlcv(self, symbole: str, timeframe: str, since_ms: Optional[int],
                           limite: int, reessais: int, backoff_initial_s: float,
                           params: Dict[str, Any]) -> List[List[Any]]:
        """fetch_ohlcv avec retries, backoff exponentiel + jitter."""
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
        if isinstance(tfs, dict) and tfs:
            if timeframe not in tfs:
                raise ValueError(f"Timeframe {timeframe!r} non supporté par {self.exchange.id}. Timeframes: {sorted(tfs.keys())}")

    def _limite_effective(self, demande: Optional[int]) -> int:
        preset_lim = int(self.preset.get("ohlcv_limit_max", 1000))
        return int(min(demande or preset_lim, preset_lim))

    def telecharger_ohlcv(self, p: ParametresOHLCV) -> pd.DataFrame:
        """
        Téléchargement OHLCV universel, repris si fichier existe, dédoublonné, borné strictement si demandé.
        """
        self._verifier_timeframe_supporte(p.timeframe)

        # Bornes
        debut_dt = _parse_date_utc(p.date_debut)
        fin_dt = _parse_date_utc(p.date_fin)
        if fin_dt and debut_dt and fin_dt <= debut_dt:
            raise ValueError("date_fin doit être strictement postérieure à date_debut.")

        pas = _timeframe_delta(p.timeframe)
        pas_ms = int(pas.total_seconds() * 1000)

        # Lecture existante pour reprise
        df_exist: Optional[pd.DataFrame] = None
        reprise_since_ms: Optional[int] = None
        if p.chemin_sortie and p.chemin_sortie.exists():
            try:
                df_exist = pd.read_csv(p.chemin_sortie, parse_dates=["timestamp"])
                df_exist["timestamp"] = pd.to_datetime(df_exist["timestamp"], utc=True)
                if not df_exist.empty:
                    dernier_ts = int(df_exist["timestamp"].max().timestamp() * 1000)
                    reprise_since_ms = dernier_ts + pas_ms
                    LOGGER.info("Reprise depuis : %s (dernier=%s)", p.chemin_sortie, df_exist["timestamp"].max())
            except Exception as e:
                LOGGER.warning("Lecture CSV existant impossible, on repart de zéro : %s", e)

        # since initial
        if reprise_since_ms is not None:
            since_ms = reprise_since_ms
        elif debut_dt:
            since_ms = int(debut_dt.timestamp() * 1000)
        else:
            since_ms = None

        # until calculé (bornage côté client + param until côté serveur si possible)
        until_ms: Optional[int] = int(fin_dt.timestamp() * 1000) if fin_dt else None

        # Limite par requête (préréglage ou CLI)
        limite = self._limite_effective(p.limite_par_requete)

        morceaux: List[pd.DataFrame] = []
        if df_exist is not None:
            morceaux.append(df_exist)

        curseur = since_ms
        total = 0

        while True:
            params_requete = build_params_ohlcv_for_exchange(self.exchange, self.preset, p.prix_ohlcv, until_ms, p.params_additionnels)
            page = self._retry_fetch_ohlcv(
                symbole=p.symbole,
                timeframe=p.timeframe,
                since_ms=curseur,
                limite=limite,
                reessais=p.reessais,
                backoff_initial_s=p.backoff_initial_s,
                params=params_requete,
            )
            if not page:
                break

            df_page = pd.DataFrame(page, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df_page["timestamp"] = pd.to_datetime(df_page["timestamp"], unit="ms", utc=True)

            # Coupe côté client si fin_dt
            if fin_dt is not None:
                df_page = df_page[df_page["timestamp"] < fin_dt]

            if df_page.empty:
                break

            morceaux.append(df_page)
            total += len(df_page)

            dernier_ms = int(df_page["timestamp"].iloc[-1].timestamp() * 1000)
            curseur = dernier_ms + 1

            # fin si page incomplète ou borne atteinte
            if len(page) < limite:
                break
            if until_ms is not None and curseur >= until_ms:
                break

        df = _concat_dedup(morceaux)

        # Exclusion bougie courante
        if p.exclure_bougie_courante and not df.empty:
            now = datetime.now(timezone.utc) - pas
            df = df[df["timestamp"] <= now]

        if p.strict_bornes:
            if debut_dt:
                df = df[df["timestamp"] >= debut_dt]
            if fin_dt:
                df = df[df["timestamp"] < fin_dt]

        # Réordonner colonnes
        if not df.empty:
            df = df[["timestamp", "open", "high", "low", "close", "volume"]]

        LOGGER.info("OHLCV terminé: %d lignes (symbole=%s, tf=%s)", len(df), p.symbole, p.timeframe)

        if p.chemin_sortie:
            _atomic_write_csv(df, p.chemin_sortie)

        return df

    # ------------------------- Utilitaires fichiers -------------------------

    def nom_fichier(self, base: str, extension: str = "csv") -> Path:
        return DOSSIER_DONNEES / f"{self.exchange.id}_{base}_{_utc_now_str()}.{extension}"


# ------------------------------ Gestionnaire PRO --------------------------

class GestionnaireEchangeCCXTPro:
    """
    Gestionnaire WebSocket universel basé sur ccxt.pro (inclus dans ccxt).
    - Reconnexion avec backoff + jitter
    - Déduplication simple
    - Écriture CSV optionnelle (atomique par batch)
    """
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

        # Aligne les mêmes options que la classe REST
        opts = self.exchange.options if hasattr(self.exchange, "options") else {}
        changed = False
        if cfg.type_marche:
            opts["defaultType"] = cfg.type_marche; changed = True
        if cfg.sous_type:
            opts["defaultSubType"] = cfg.sous_type; changed = True
        if cfg.mode_marge and self.preset.get("supports_margin_mode"):
            opts["defaultMarginMode"] = cfg.mode_marge; changed = True
        if changed:
            self.exchange.options = opts

    async def _run_with_reconnect(self, coro_factory, handle, p: ParametresFlux):
        debut = time.time()
        essais = 0
        messages = 0
        while True:
            if p.duree_max_s and (time.time() - debut) >= p.duree_max_s: break
            if p.max_messages and messages >= p.max_messages: break
            try:
                async with self.exchange as ex:
                    while True:
                        if p.duree_max_s and (time.time() - debut) >= p.duree_max_s: return
                        if p.max_messages and messages >= p.max_messages: return
                        data = await coro_factory(ex)
                        await handle(data)
                        messages += 1
            except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable, ccxt.InvalidNonce) as e:
                essais += 1
                pause = p.backoff_initial_s * (2 ** min(essais, 8)) * random.uniform(0.85, 1.15)
                LOGGER.warning("WebSocket reconnect dans %.2fs (%s)", pause, type(e).__name__)
                await asyncio.sleep(pause)
                continue
            except Exception as e:
                LOGGER.error("Flux stoppé (irréversible): %s", e)
                raise

    # -------------------- Flux concrets --------------------

    async def demarrer_flux(self, p: ParametresFlux):
        if p.type_flux == "ohlcv":
            await self._flux_ohlcv(p)
        elif p.type_flux == "ticker":
            await self._flux_ticker(p)
        elif p.type_flux == "orderbook":
            await self._flux_orderbook(p)
        elif p.type_flux == "trades":
            await self._flux_trades(p)
        else:
            raise ValueError(f"type_flux inconnu: {p.type_flux}")

    async def _flux_ohlcv(self, p: ParametresFlux):
        pas = _timeframe_delta(p.timeframe)
        dernier_ts_ecrit_ms: Optional[int] = None
        tampon: List[pd.DataFrame] = []

        def params_ws():
            return build_params_ohlcv_for_exchange(self.exchange, self.preset, p.prix_ohlcv, None, p.params_additionnels)

        async def handle(batch):
            nonlocal tampon, dernier_ts_ecrit_ms
            # ccxt.pro renvoie souvent la *liste* des bougies connues pour ce symbole
            df = pd.DataFrame(batch, columns=["timestamp","open","high","low","close","volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            if p.exclure_bougie_courante and not df.empty:
                now = datetime.now(timezone.utc) - pas
                df = df[df["timestamp"] <= now]
            if dernier_ts_ecrit_ms is not None and not df.empty:
                df = df[(df["timestamp"].astype("int64") // 10**6) > dernier_ts_ecrit_ms]
            if df.empty:
                return
            df.sort_values("timestamp", inplace=True)
            dernier_ts_ecrit_ms = int(df["timestamp"].iloc[-1].timestamp()*1000)
            tampon.append(df)
            if p.chemin_sortie and sum(len(x) for x in tampon) >= p.flush_toutes_n:
                _atomic_write_csv(_concat_dedup(tampon), p.chemin_sortie)
                tampon = []

        async def coro_factory(ex):
            return await ex.watch_ohlcv(p.symbole, p.timeframe, None, None, params_ws())

        await self._run_with_reconnect(coro_factory, handle, p)
        if p.chemin_sortie and tampon:
            _atomic_write_csv(_concat_dedup(tampon), p.chemin_sortie)

    async def _flux_ticker(self, p: ParametresFlux):
        async def handle(ticker):
            LOGGER.info("ticker %s: %s", p.symbole, ticker.get("last"))
        async def coro_factory(ex):
            return await ex.watch_ticker(p.symbole, p.params_additionnels)
        await self._run_with_reconnect(coro_factory, handle, p)

    async def _flux_orderbook(self, p: ParametresFlux):
        async def handle(ob):
            bid = ob["bids"][0][0] if ob.get("bids") else None
            ask = ob["asks"][0][0] if ob.get("asks") else None
            LOGGER.info("orderbook %s: best bid=%s ask=%s", p.symbole, bid, ask)
        async def coro_factory(ex):
            return await ex.watch_order_book(p.symbole, p.profondeur, p.params_additionnels)
        await self._run_with_reconnect(coro_factory, handle, p)

    async def _flux_trades(self, p: ParametresFlux):
        dernier_id = None
        tampon: List[Dict[str, Any]] = []

        async def handle(trades):
            nonlocal dernier_id, tampon
            if isinstance(trades, dict): trades = [trades]
            for t in trades:
                tid = t.get("id") or t.get("timestamp")
                if dernier_id is not None and tid == dernier_id:
                    continue
                dernier_id = tid
                if p.chemin_sortie:
                    tampon.append({
                        "timestamp": datetime.fromtimestamp(t["timestamp"]/1000, tz=timezone.utc),
                        "price": t.get("price"), "amount": t.get("amount"),
                        "side": t.get("side"), "id": t.get("id")
                    })
            if p.chemin_sortie and len(tampon) >= p.flush_toutes_n:
                df_new = pd.DataFrame(tampon)
                if p.chemin_sortie.exists():
                    try:
                        df_old = pd.read_csv(p.chemin_sortie, parse_dates=["timestamp"])
                    except Exception:
                        df_old = pd.DataFrame()
                    df_all = pd.concat([df_old, df_new], ignore_index=True)
                else:
                    df_all = df_new
                _atomic_write_csv(df_all, p.chemin_sortie)
                tampon.clear()

        async def coro_factory(ex):
            return await ex.watch_trades(p.symbole, None, None, p.params_additionnels)

        await self._run_with_reconnect(coro_factory, handle, p)
        if p.chemin_sortie and tampon:
            df_new = pd.DataFrame(tampon)
            if p.chemin_sortie.exists():
                try:
                    df_old = pd.read_csv(p.chemin_sortie, parse_dates=["timestamp"])
                except Exception:
                    df_old = pd.DataFrame()
                df_all = pd.concat([df_old, df_new], ignore_index=True)
            else:
                df_all = df_new
            _atomic_write_csv(df_all, p.chemin_sortie)


# ---------------------------------- CLI -----------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Base universelle CCXT (FR) — REST & WebSockets (ccxt.pro)")
    # Exchange
    p.add_argument("--exchange", default="binance", help="ID CCXT (ex: binance, okx, bybit, kraken, kucoin...)")
    p.add_argument("--sandbox", action="store_true", help="Active le mode sandbox si supporté")
    p.add_argument("--timeout", type=int, default=30000, help="Timeout (ms)")
    p.add_argument("--options", type=str, default="{}", help="options CCXT (JSON)")
    p.add_argument("--type-marche", default="spot", choices=["spot", "swap", "future", "margin", "option"], help="Type de marché par défaut")
    p.add_argument("--sous-type", default=None, help="Sous-type (linear|inverse) si supporté")
    p.add_argument("--mode-marge", default=None, help="Mode de marge (isolated|cross) si supporté")
    # Credentials (optionnels)
    p.add_argument("--api-key", default=None)
    p.add_argument("--secret", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--uid", default=None)

    # OHLCV (REST)
    p.add_argument("--symbole", required=True, help="Symbole CCXT (ex: BTC/USDT, BTC/USDT:USDT pour perp).")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--date-debut", dest="date_debut", default=None, help="ISO8601 (UTC) ou 'YYYY-MM-DD'")
    p.add_argument("--date-fin", dest="date_fin", default=None, help="ISO8601 (UTC) ou 'YYYY-MM-DD' (exclue)")
    p.add_argument("--limite", type=int, default=None, help="Limite par requête (par défaut : preset de l'exchange)")
    p.add_argument("--prix-ohlcv", dest="prix_ohlcv", default=None, help="mark|index|last si supporté")
    p.add_argument("--no-exclure-bougie-courante", action="store_true", help="Ne pas exclure la bougie en cours")
    p.add_argument("--no-bornes-strictes", action="store_true", help="Ne pas couper strictement aux bornes")
    p.add_argument("--params", default="{}", help="Paramètres additionnels OHLCV (JSON)")
    p.add_argument("--sortie", type=Path, default=None, help="Chemin CSV de sortie (reprise idempotente si existe)")

    # Streaming (ccxt.pro)
    p.add_argument("--stream", choices=["ohlcv","ticker","orderbook","trades"], default=None,
                   help="Active un flux WebSocket ccxt.pro pour le type demandé.")
    p.add_argument("--duree", type=int, default=None, help="Durée max en secondes du flux.")
    p.add_argument("--flush", type=int, default=20, help="Flush disque toutes les N unités (flux).")
    p.add_argument("--depth", type=int, default=None, help="Profondeur du carnet pour --stream orderbook.")
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
        id_exchange=args.exchange,
        gerer_rate_limit=True,
        timeout_ms=args.timeout,
        sandbox=bool(args.sandbox),
        api_key=args.api_key,
        secret=args.secret,
        password=args.password,
        uid=args.uid,
        options=options,
        type_marche=args.type_marche,
        sous_type=args.sous_type,
        mode_marge=args.mode_marge,
    )

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

        sortie = args.sortie  # optionnelle
        pf = ParametresFlux(
            type_flux=args.stream,
            symbole=args.symbole,
            timeframe=args.timeframe,
            prix_ohlcv=args.prix_ohlcv,
            profondeur=args.depth,
            params_additionnels=params_add,
            duree_max_s=args.duree,
            chemin_sortie=sortie,
            flush_toutes_n=args.flush,
            exclure_bougie_courante=not args.no_exclure_bougie_courante,
        )
        try:
            asyncio.run(pro.demarrer_flux(pf))
            return 0
        except KeyboardInterrupt:
            LOGGER.warning("Interrompu.")
            return 130
        except Exception as e:
            LOGGER.error("Flux arrêté avec erreur : %s", e)
            return 1

    # Sinon, mode REST (OHLCV)
    try:
        gestionnaire = GestionnaireEchangeCCXT(cfg)
    except Exception as e:
        LOGGER.error("Initialisation exchange échouée : %s", e)
        return 1

    # Fichier de sortie par défaut
    sortie = args.sortie or gestionnaire.nom_fichier(
        base=f"ohlcv_{args.symbole.replace('/', '-').replace(':', '-')}_{args.timeframe}"
    )

    params_ohlcv = ParametresOHLCV(
        symbole=args.symbole,
        timeframe=args.timeframe,
        date_debut=args.date_debut,
        date_fin=args.date_fin,
        limite_par_requete=args.limite,
        prix_ohlcv=args.prix_ohlcv,
        exclure_bougie_courante=not args.no_exclure_bougie_courante,
        strict_bornes=not args.no_bornes_strictes,
        chemin_sortie=sortie,
        params_additionnels=params_add,
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
