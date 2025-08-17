#!/usr/bin/env python3
# ===========================================================
# Projet : ccxt_universel_v2
# Script : test_latence_exchanges.py
# Objectif : Mesurer la latence CCXT (REST API) pour plusieurs exchanges
# Auteur : Jean BoNoBo - 2025
# ===========================================================

import ccxt
import time
import logging
import csv
from datetime import datetime

# ----------------------------
# Configuration générale
# ----------------------------
SYMBOL_PAR_DEFAUT = "BTC/USDT"
NB_TESTS_PAR_EXCHANGE = 3
FICHIER_CSV = "latences_exchanges.csv"

# Initialisation logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("ccxt_universel_latence.log"),
        logging.StreamHandler()
    ]
)

# Exchanges à tester (ajoute/enlève selon tes besoins)
EXCHANGES = [
    "binance",
    "bybit",
    "okx",
    "kucoin",
    "kraken",
    "coinbase",
    "bitfinex",
    "huobi",
    "gateio",
]

# ----------------------------
# Fonction pour tester la latence
# ----------------------------
def tester_latence(exchange_id, symbol=SYMBOL_PAR_DEFAUT, essais=NB_TESTS_PAR_EXCHANGE):
    """
    Mesure la latence moyenne d'un exchange CCXT en ms.
    """
    try:
        exchange_class = getattr(ccxt, exchange_id)
        exchange = exchange_class({"enableRateLimit": True})
    except Exception as e:
        logging.error(f"{exchange_id} - Erreur initialisation CCXT : {e}")
        return None

    latences = []
    for i in range(essais):
        try:
            debut = time.time()
            exchange.fetch_ticker(symbol)
            fin = time.time()
            latence = (fin - debut) * 1000
            latences.append(latence)
            logging.info(f"{exchange_id} [{i+1}/{essais}] - {latence:.2f} ms")
        except Exception as e:
            logging.warning(f"{exchange_id} - Erreur fetch : {e}")
            continue

    if latences:
        moyenne = sum(latences) / len(latences)
        logging.info(f"{exchange_id} - Latence moyenne : {moyenne:.2f} ms")
        return moyenne
    else:
        return None

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    logging.info("=== Début du test de latence multi-exchanges (ccxt_universel_v2) ===")

    # Écrire résultats CSV
    with open(FICHIER_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "Exchange", "Latence_ms"])

        for ex in EXCHANGES:
            latence = tester_latence(ex)
            if latence:
                writer.writerow([datetime.utcnow().isoformat(), ex, f"{latence:.2f}"])
    
    logging.info(f"✅ Résultats sauvegardés dans {FICHIER_CSV}")
