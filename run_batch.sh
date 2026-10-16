#!/bin/bash
# Runs N solo games end to end with the random agent and prints each summary.
# Usage: bash run_batch.sh [games] [extra random_agent.py flags...]
set -u
cd "$(dirname "$0")"

GAMES=${1:-5}
shift || true

for i in $(seq 1 "$GAMES"); do
  info=$(python3 new_game.py) || { echo "game $i: new_game.py failed"; continue; }
  name=$(printf '%s' "$info" | python3 -c 'import sys,json;print(json.load(sys.stdin)["gameName"])')
  key=$(printf '%s' "$info" | python3 -c 'import sys,json;print(json.load(sys.stdin)["authKey"])')
  echo "=== run $i: game $name ==="
  python3 random_agent.py "$name" 1 "$key" --seed "$i" --quiet "$@"
done
