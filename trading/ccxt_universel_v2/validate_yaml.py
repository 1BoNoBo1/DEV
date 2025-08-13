import sys, re, os
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML manquant dans l'image. (mais runner l'utilise déjà, donc il devrait être présent)")
    sys.exit(2)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REQUIRED_COMMON = ["name", "mode", "exchange"]
STREAM_REQUIRED = ["stream", "timeframe"]
REST_REQUIRED = ["symbole", "timeframe", "date_debut", "date_fin"]

def _err(errors, msg, tname):
    errors.append(f"[{tname}] {msg}")

def _warn(warnings, msg, tname):
    warnings.append(f"[{tname}] {msg}")

def check_task(t, idx, errors, warnings):
    tname = str(t.get("name") or f"task_{idx+1}")

    # champs communs
    for k in REQUIRED_COMMON:
        if not t.get(k):
            _err(errors, f"champ requis manquant: '{k}'", tname)

    mode = (t.get("mode") or "").lower()
    if mode not in ("rest", "stream"):
        _err(errors, "mode doit être 'rest' ou 'stream'", tname)

    # REST
    if mode == "rest":
        for k in REST_REQUIRED:
            if not t.get(k):
                _err(errors, f"REST: champ requis manquant: '{k}'", tname)
        # dates au format YYYY-MM-DD
        for k in ("date_debut", "date_fin"):
            v = t.get(k)
            if isinstance(v, str) and not DATE_RE.match(v):
                _warn(warnings, f"REST: '{k}' n'est pas au format YYYY-MM-DD ('{v}')", tname)

    # STREAM
    if mode == "stream":
        for k in STREAM_REQUIRED:
            if not t.get(k):
                _err(errors, f"STREAM: champ requis manquant: '{k}'", tname)

        sym = t.get("symbole")
        syms = t.get("symboles") or t.get("symbols")
        if not sym and not syms:
            _err(errors, "STREAM: fournir 'symbole' (single) ou 'symboles' (liste/CSV)", tname)
        if sym and syms:
            _warn(warnings, "STREAM: 'symbole' ET 'symboles' fournis — 'symboles' sera prioritaire", tname)

        # format/sortie cohérents (soft checks : erreurs bloquantes si incohérence évidente)
        fmt = (t.get("format") or "").lower()
        out = t.get("sortie")
        if fmt and not out:
            _warn(warnings, "STREAM: 'format' défini mais 'sortie' vide — le runner créera un chemin par défaut", tname)
        if out and isinstance(out, str) and fmt:
            if fmt == "sqlite" and not out.lower().endswith(".sqlite"):
                _warn(warnings, "STREAM: format=sqlite mais 'sortie' n'a pas l'extension .sqlite", tname)
            if fmt == "parquet" and not out.lower().endswith(".parquet"):
                _warn(warnings, "STREAM: format=parquet mais 'sortie' n'a pas l'extension .parquet", tname)

        # sqlite_table conseillé si sqlite (sinon runner la déduira)
        if fmt == "sqlite" and not t.get("sqlite_table"):
            _warn(warnings, "STREAM: format=sqlite sans 'sqlite_table' — sera déduit (ex: ohlcv_1m)", tname)

        # recommandations par exchange
        ex = (t.get("exchange") or "").lower()
        if ex == "okx" and (t.get("stream") == "ohlcv") and "prix_ohlcv" not in t:
            _warn(warnings, "OKX ohlcv: 'prix_ohlcv: mark' recommandé pour homogénéité", tname)

def main(path):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "tasks" not in data or not isinstance(data["tasks"], list):
        print("ERROR: YAML invalide: clé 'tasks' manquante ou non-liste.")
        sys.exit(1)

    errors, warnings = [], []
    for i, t in enumerate(data["tasks"]):
        if not isinstance(t, dict):
            errors.append(f"[task_{i+1}] doit être un mapping/objet YAML")
            continue
        check_task(t, i, errors, warnings)

    if errors:
        print("✗ Erreurs:")
        for e in errors:
            print("  -", e)
    if warnings:
        print("! Avertissements:")
        for w in warnings:
            print("  -", w)

    if errors:
        sys.exit(1)
    print("✓ YAML valide:", path)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python validate_yaml.py <chemin.yaml>")
        sys.exit(2)
    main(sys.argv[1])
