#!/usr/bin/env bash
set -euo pipefail

# Par défaut on lit le YAML "ccxt_batch.yaml" à la racine du projet
YAML="${YAML:-ccxt_batch.yaml}"

# Si le premier argument est fourni ET ne commence pas par "--",
# on le considère comme le chemin du YAML.
if [[ "${1:-}" != "" && "${1:0:2}" != "--" ]]; then
  YAML="$1"
  shift
fi

# Si la variable d'env TASKS est définie → on l'utilise telle quelle (CSV)
if [[ -n "${TASKS:-}" ]]; then
  exec python runner_ccxt_batch.py --yaml "$YAML" --taches "$TASKS"
fi

# Sinon, on regarde les arguments restants :
# - si l'utilisateur a donné "--taches ..." on les passe tels quels
# - s'il a listé des noms de tâches séparés par des espaces, on les convertit en CSV
if [[ $# -gt 0 ]]; then
  if [[ "$1" == "--taches" ]]; then
    exec python runner_ccxt_batch.py --yaml "$YAML" "$@"
  else
    IFS=, read -r -a TASK_ARR <<< "$*"
    TASKS_CSV="$(printf "%s," "${TASK_ARR[@]}" | sed 's/,$//')"
    exec python runner_ccxt_batch.py --yaml "$YAML" --taches "$TASKS_CSV"
  fi
fi

# Aucune tâche spécifique → exécute tout le YAML
exec python runner_ccxt_batch.py --yaml "$YAML"
