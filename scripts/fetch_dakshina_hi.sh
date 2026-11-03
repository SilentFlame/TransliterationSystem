#!/usr/bin/env bash
# Optional: download the public Dakshina Hindi lexicon for a fuller quality run.
# The checked-in data/dakshina_hi_sample.tsv is enough for offline demo_check.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$ROOT/data/dakshina_hi_lex.tsv}"
URL="${DAKSHINA_URL:-https://raw.githubusercontent.com/google-research-datasets/dakshina/master/hi/lexicons/hi.translit.sampled.train.tsv}"

echo "Fetching $URL → $DEST"
curl -fsSL "$URL" -o "$DEST"
echo "rows: $(grep -cv '^#' "$DEST" || true)"
echo "Score with:"
echo "  python -m benchmarks.quality_dakshina --lexicon $DEST --arms stock"
