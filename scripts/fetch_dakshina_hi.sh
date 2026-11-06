#!/usr/bin/env bash
# Download the official Dakshina Hindi test lexicon (not the 2 GB full release).
# Streams dakshina_dataset_v1.0.tar and keeps only the Hindi test TSV.
# Columns: native, roman, count. The quality harness reads that order.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$ROOT/data/dakshina/hi.translit.sampled.test.tsv}"
mkdir -p "$(dirname "$DEST")"
"$ROOT/.venv/bin/python" - "$DEST" <<'PY'
import sys, tarfile, urllib.request
dest, url = sys.argv[1], "https://storage.googleapis.com/gresearch/dakshina/dakshina_dataset_v1.0.tar"
want = "hi/lexicons/hi.translit.sampled.test.tsv"
print(f"streaming {url}", flush=True)
with urllib.request.urlopen(url, timeout=120) as resp, tarfile.open(fileobj=resp, mode="r|") as tar:
    for member in tar:
        if member.name.endswith(want):
            data = tar.extractfile(member).read()
            open(dest, "wb").write(data)
            print(f"wrote {dest} ({len(data)} bytes)", flush=True)
            break
    else:
        raise SystemExit(f"not found in archive: {want}")
PY
echo "rows: $(grep -cve '^#' "$DEST" || true)"
echo "Score with:"
echo "  python -m benchmarks.quality_dakshina --lexicon $DEST --arms stock,base,rescore \\"
echo "    --package-dir var/registry/hi/hi-1.0.0 --out results/quality_dakshina_hi_test.json"
