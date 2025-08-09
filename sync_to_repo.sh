#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${1:-}"
BRANCH="${2:-main}"

if [[ -z "$REPO_URL" ]]; then
  echo "Usage: $0 <GITHUB_REPO_URL> [branch]" >&2
  echo "ex: $0 git@github.com:username/DEV.git main" >&2
  exit 1
fi

cd "$(dirname "$0")"

if [[ ! -d ".git" ]]; then
  git init
  git checkout -b "$BRANCH"
  git remote add origin "$REPO_URL"
fi

git add .
if git diff --cached --quiet; then
  echo "Rien à committer."
else
  git commit -m "chore(repo): initial DEV skeleton + ccxt_universel_v2"
fi

git push -u origin "$BRANCH"
echo "✅ Poussé sur $REPO_URL ($BRANCH)"
