#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Outil de vérification "sanity check" pour ccxt_universel_v2.
- Valide exchange/symbols/timeframe (REST et capacités STREAM).
- Loggue explicitement la valeur effective de 'flush' si fournie via YAML.
- Réalise un mini test fetch_ohlcv (limité) en REST.
- Ne modifie aucune donnée; ne dépend d'aucun autre module du projet.

Usage (exemples) :
  python tools/sanity_check.py --exchange binance --mode rest \
      --symbols BTC/USDT,ETH/USDT --timeframe 1m --yaml config/exemple.yaml

  python tools/sanity_check.py --exchange okx --mode stream \
      --symbols BTC/USDT:USDT,ETH/USDT:USDT --timeframe 1m

Conventions :
- Fonctions en français, docstrings pédagogiques.
- try/except systématiques avec messages clairs.
"""
import argparse
import sys
import time
import logging
from typing import List, Optional, Dict, Any

# ccxt (synchrone) et ccxt.pro (async) sont importés si dispo.
try:
    import ccxt  # type: ignore
except Exception as e:
    print(f"[ERREUR] ccxt introuvable ou non importable: {e}", file=sys.stderr)
    sys.exit(2)

try:
    import ccxt.pro as ccxtpro  # type: ignore
    CCXTPRO_DISPONIBLE = True
except Exception:
    CCXTPRO_DISPONIBLE = False

# YAML en option pour logger la clé 'flush'
try:
    import yaml  # type: ignore
    YAML_DISPONIBLE = True
except Exception:
    YAML_DISPONIBLE = False


def configurer_logging(niveau: str = "INFO") -> None:
    """Configure le logging console simple."""
    lvl = getattr(logging, niveau.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def lire_yaml(path: Optional[str]) -> Dict[str, Any]:
    """Charge un YAML si présent, sinon retourne {}. Ne lève pas d'exception."""
    if not path:
        return {}
    if not YAML_DISPONIBLE:
        logging.warning("PyYAML indisponible: impossible de lire '%s'", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logging.warning("Fichier YAML introuvable: %s", path)
    except Exception as e:
        logging.warning("Lecture YAML '%s' impossible: %s", path, e)
    return {}


def journaliser_flush(yaml_data: Dict[str, Any]) -> None:
    """
    Extrait et journalise la valeur effective de 'flush' si présente dans le YAML.
    On inspecte les niveaux courants ('flush' à la racine ou sous 'tache'/'task').
    """
    clefs = ["flush", "FLUSH", "Flush"]
    valeur = None

    # à la racine
    for k in clefs:
        if k in yaml_data:
            valeur = yaml_data.get(k)
            break

    # sous-structures courantes
    if valeur is None:
        for sous in ("tache", "task", "stream", "rest", "sortie", "output"):
            bloc = yaml_data.get(sous, {})
            if isinstance(bloc, dict):
                for k in clefs:
                    if k in bloc:
                        valeur = bloc.get(k)
                        break
            if valeur is not None:
                break

    if valeur is not None:
        logging.info("FLUSH (YAML) → valeur effective détectée: %r", valeur)
    else:
        logging.info("FLUSH (YAML) → aucune clé 'flush' détectée; valeur par défaut gérée par le runner principal.")


def analyser_arguments() -> argparse.Namespace:
    """Parse les arguments CLI."""
    p = argparse.ArgumentParser(description="Sanity check ccxt/ccxt.pro")
    p.add_argument("--exchange", required=True, help="Identifiant exchange CCXT (ex: binance, okx, bybit, ...)")
    p.add_argument("--mode", choices=["rest", "stream"], required=True, help="Type de vérification principale")
    p.add_argument("--symbols", required=True, help="Liste de symboles séparés par des virgules")
    p.add_argument("--timeframe", default="1m", help="Timeframe OHLCV (ex: 1m, 5m, 1h, 1d)")
    p.add_argument("--yaml", dest="yaml_path", default=None, help="Chemin d'un YAML à inspecter (pour logger flush)")
    p.add_argument("--type_marche", choices=["spot", "swap", "future", "margin"], default=None, help="Type de marché ccxt")
    p.add_argument("--sous_type", choices=["linear", "inverse"], default=None, help="Sous-type (dérivés)")
    p.add_argument("--mode_marge", choices=["isolated", "cross"], default=None, help="Mode de marge par défaut")
    p.add_argument("--price", choices=["last", "mark", "index"], default="last", help="Type de prix OHLCV si supporté")
    p.add_argument("--test_fetch", action="store_true", help="Effectuer un mini fetch_ohlcv (10 bougies) pour le 1er symbole")
    p.add_argument("--timeout", type=int, default=15000, help="Timeout ms ccxt")
    p.add_argument("--loglevel", default="INFO", help="Niveau de logs (DEBUG, INFO, WARNING...)")
    return p.parse_args()


def instancier_exchange_sync(args: argparse.Namespace):
    """
    Instancie un exchange ccxt (synchrone) avec options de marché si fournies.
    """
    cls = getattr(ccxt, args.exchange, None)
    if cls is None:
        raise RuntimeError(f"Exchange '{args.exchange}' inconnu dans ccxt")
    exchange = cls({
        "enableRateLimit": True,
        "timeout": args.timeout,
        # options marché :
        "options": {}
    })
    # options marché fines
    try:
        if args.type_marche:
            exchange.options["defaultType"] = args.type_marche
        if args.sous_type:
            exchange.options["defaultSubType"] = args.sous_type
        if args.mode_marge:
            exchange.options["defaultMarginMode"] = args.mode_marge
    except Exception:
        # toutes les plateformes n'exposent pas ces options
        pass
    return exchange


def verifier_capacites(exchange) -> Dict[str, Any]:
    """
    Retourne un résumé des capacités pertinentes de l'exchange (has).
    """
    has = getattr(exchange, "has", {}) or {}
    resume = {
        "fetchOHLCV": bool(has.get("fetchOHLCV")),
        "watchOHLCV": bool(has.get("watchOHLCV")),
        "watchOHLCVForSymbols": bool(has.get("watchOHLCVForSymbols")),
        "watchTrades": bool(has.get("watchTrades")),
        "watchTicker": bool(has.get("watchTicker")),
        "watchOrderBook": bool(has.get("watchOrderBook")),
    }
    return resume


def verifier_symbols_disponibles(exchange, symbols: List[str]) -> List[str]:
    """
    Charge les marchés et valide que chaque symbole est coté.
    Retourne la liste des symboles introuvables (si vide → tout va bien).
    """
    introuvables: List[str] = []
    try:
        markets = exchange.load_markets()
        for s in symbols:
            if s not in markets:
                introuvables.append(s)
    except Exception as e:
        logging.error("Échec load_markets(): %s", e)
        raise
    return introuvables


def mini_test_fetch_ohlcv(exchange, symbol: str, timeframe: str, price: str) -> None:
    """
    Tente un petit fetch_ohlcv (10 bougies) pour valider les paramètres.
    N'utilise pas d'écriture disque; se limite à un appel réseau léger.
    """
    try:
        if not exchange.has.get("fetchOHLCV"):
            logging.warning("fetch_ohlcv non supporté → mini test REST ignoré")
            return
        # Calcul rapide du 'since' pour ~10 bougies
        seconds = ccxt.parse_timeframe(timeframe)
        since_ms = exchange.milliseconds() - (seconds * 1000 * 10)
        params = {}
        # certains exchanges acceptent un 'price' pour OHLCV (mark/index/last)
        if price in ("mark", "index"):
            params["price"] = price
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=10, params=params)
        if not ohlcv:
            logging.warning("fetch_ohlcv renvoie 0 lignes (symbol=%s, tf=%s)", symbol, timeframe)
            return
        # Log d'échantillon
        t0 = ohlcv[0][0]
        tN = ohlcv[-1][0]
        logging.info("fetch_ohlcv OK: %d lignes, de %s à %s", len(ohlcv), exchange.iso8601(t0), exchange.iso8601(tN))
    except ccxt.BaseError as e:
        logging.error("fetch_ohlcv erreur CCXT: %s", e)
        raise
    except Exception as e:
        logging.error("fetch_ohlcv erreur inattendue: %s", e)
        raise


def main() -> int:
    args = analyser_arguments()
    configurer_logging(args.loglevel)

    logging.info("ccxt version: %s | ccxt.pro dispo: %s", getattr(ccxt, "__version__", "inconnue"), CCXTPRO_DISPONIBLE)
    logging.info("Exchange ciblé: %s | Mode: %s | Timeframe: %s", args.exchange, args.mode, args.timeframe)
    logging.info("Symbols fournis: %s", args.symbols)

    # YAML → logger la valeur effective de 'flush' s'il existe
    yaml_data = lire_yaml(args.yaml_path)
    journaliser_flush(yaml_data)

    # Instanciation ccxt synchrone pour checks généraux
    try:
        ex = instancier_exchange_sync(args)
    except Exception as e:
        logging.error("Échec d'instanciation exchange '%s': %s", args.exchange, e)
        return 2

    # Capacités
    resume = verifier_capacites(ex)
    logging.info("Capacités exchange (has): %s", resume)

    # Vérif markets + symboles
    try:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        introuvables = verifier_symbols_disponibles(ex, symbols)
        if introuvables:
            logging.error("Symboles introuvables: %s", introuvables)
            return 3
        logging.info("Tous les symboles sont présents sur l'exchange.")
    except Exception:
        return 4

    # Mode REST → mini fetch_ohlcv optionnel (1er symbole seulement)
    if args.mode == "rest":
        if args.test_fetch:
            try:
                mini_test_fetch_ohlcv(ex, symbols[0], args.timeframe, args.price)
            except Exception:
                return 5

    # Mode STREAM → on vérifie seulement les capacités et ccxt.pro dispo
    if args.mode == "stream":
        if not CCXTPRO_DISPONIBLE:
            logging.error("ccxt.pro indisponible dans cet environnement (pip/clé licence/installation).")
            return 6
        # Rappel des capacités WebSocket utiles
        if not (resume["watchOHLCV"] or resume["watchTrades"] or resume["watchTicker"] or resume["watchOrderBook"]):
            logging.warning("Aucune capacité WebSocket pertinente détectée (watch*). Vérifiez l'exchange et la licence ccxt.pro.")

    logging.info("Sanity check terminé avec succès.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrompu par l'utilisateur.", file=sys.stderr)
        sys.exit(130)
