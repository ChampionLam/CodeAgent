#!/usr/bin/env bash
# bootstrap-venv.sh -- Linux/macOS twin of scripts/bootstrap-venv.ps1.
# Creates python/.venv and installs every library the app's own features need
# (images, document extraction, OCR fallback). Deploy should call this so a
# fresh clone is functional instead of half-functional.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Optional 2nd arg: pip index URL (e.g. https://pypi.tuna.tsinghua.edu.cn/simple).
# Used for this run only; the machine's pip config is left alone.
INDEX_URL="${2:-}"
VENV="$ROOT/python/.venv"
PY="$VENV/bin/python3"

[ -d "$ROOT/python" ] || { echo "[bootstrap] no python/ under $ROOT" >&2; exit 1; }
echo "[bootstrap] root: $ROOT"

if [ ! -x "$PY" ]; then
  echo "[bootstrap] creating venv at $VENV"
  python3 -m venv "$VENV"
fi

echo "[bootstrap] upgrading pip"
"$PY" -m pip install --upgrade pip --quiet

for req in "$ROOT/python/requirements.txt" "$ROOT/python/requirements-docs.txt"; do
  # Fail loud -- a skipped requirements file means a half-working install.
  [ -f "$req" ] || { echo "[bootstrap] missing requirements: $req" >&2; exit 1; }
  echo "[bootstrap] installing $(basename "$req")"
  if [ -n "$INDEX_URL" ]; then
    "$PY" -m pip install -i "$INDEX_URL" -r "$req"
  else
    "$PY" -m pip install -r "$req"
  fi
done

# 模型层（guard 第二层，DecisionBert int8）是可选层：装失败只警告并继续，
# 不许拖垮整个 bootstrap —— 缺了它 guard 自动降级为纯正则（fail-open），
# sidecar 照常起。这与上面的文档层口径不同（文档层部署时必须装齐）。
GUARD_REQ="$ROOT/python/requirements-guard.txt"
if [ ! -f "$GUARD_REQ" ]; then
  echo "[bootstrap] warn: optional guard-model requirements missing: $GUARD_REQ (skipped)" >&2
else
  echo "[bootstrap] installing (optional) requirements-guard.txt"
  if [ -n "$INDEX_URL" ]; then
    if ! "$PY" -m pip install -i "$INDEX_URL" -r "$GUARD_REQ"; then
      echo "[bootstrap] warn: requirements-guard.txt install failed -- guard model layer degrades to regex-only (fail-open); bootstrap continues" >&2
    fi
  else
    if ! "$PY" -m pip install -r "$GUARD_REQ"; then
      echo "[bootstrap] warn: requirements-guard.txt install failed -- guard model layer degrades to regex-only (fail-open); bootstrap continues" >&2
    fi
  fi
fi

echo "[bootstrap] self-check"
"$PY" - <<'PYEOF'
import sys
print("  python", sys.version.split()[0])
for mod, label in (("PIL", "Pillow"), ("anydoc", "anydoc"), ("fitz", "pymupdf"),
                   ("pypdf", "pypdf"), ("openpyxl", "openpyxl"), ("docx", "python-docx")):
    try:
        __import__(mod)
        print("  OK   ", label)
    except Exception as exc:
        print("  MISS ", label, "->", type(exc).__name__)
PYEOF
echo "[bootstrap] done"
