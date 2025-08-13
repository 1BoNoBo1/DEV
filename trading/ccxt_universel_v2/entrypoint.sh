#!/usr/bin/env bash
set -Eeuo pipefail

cd /app

# -- Aide directe
if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
  exec python /app/runner_ccxt_batch.py --help
fi

# -- Pass-through shell / python
case "${1:-}" in
  bash|sh)
    exec "$@"
    ;;
  python)
    shift || true
    exec python "$@"
    ;;
esac

# -- Si le 1er argument est un YAML, on l'utilise
if [[ ${1:-} == *.yaml || ${1:-} == *.yml ]]; then
  YAML="$1"; shift || true
  if [[ ! -f "$YAML" ]]; then
    echo "ERROR: YAML '$YAML' introuvable dans le conteneur." >&2
    exit 2
  fi

  # Validation optionnelle
  if [[ -n "${VALIDATE:-}" && -f /app/validate_yaml.py ]]; then
    python /app/validate_yaml.py "$YAML"
  fi

  if [[ -n "${TASKS:-}" ]]; then
    exec python /app/runner_ccxt_batch.py --yaml "$YAML" --taches "$TASKS"
  else
    exec python /app/runner_ccxt_batch.py --yaml "$YAML"
  fi
fi

# -- Sinon: YAML par défaut + exécuter toutes les tâches si TASKS non défini
YAML="/app/ccxt_batch.yaml"
if [[ ! -f "$YAML" ]]; then
  echo "ERROR: YAML par défaut '$YAML' introuvable. Montez un fichier YAML ou passez un chemin." >&2
  exit 2
fi

# Validation optionnelle
if [[ -n "${VALIDATE:-}" && -f /app/validate_yaml.py ]]; then
  python /app/validate_yaml.py "$YAML"
fi

if [[ -n "${TASKS:-}" ]]; then
  exec python /app/runner_ccxt_batch.py --yaml "$YAML" --taches "$TASKS"
else
  exec python /app/runner_ccxt_batch.py --yaml "$YAML"
fi
