#!/usr/bin/env bash
# check-docs-parity.sh — Verify CN/EN bilingual mirror parity in docs/
# Exit code: Non-zero if any check fails.

set -euo pipefail

DOCS_DIR="$(cd "$(dirname "$0")/../docs" && pwd)"
DRIFT=0

echo "=== CN/EN docs/ parity check ==="

# 1. File count parity: every zh-CN file must have a matching en/ file
echo ""
echo "[1] File count parity..."
ZH_FILES=$(find "$DOCS_DIR/zh-CN" -name '*.md' | sed "s|$DOCS_DIR/zh-CN/||" | sort)
EN_FILES=$(find "$DOCS_DIR/en" -name '*.md' | sed "s|$DOCS_DIR/en/||" | sort)

if diff <(echo "$ZH_FILES") <(echo "$EN_FILES") > /dev/null 2>&1; then
  echo "  PASS: File count and names match"
else
  echo "  FAIL: File mismatch"
  echo "  Only in zh-CN/:"
  comm -23 <(echo "$ZH_FILES") <(echo "$EN_FILES") | sed 's/^/    /'
  echo "  Only in en/:"
  comm -13 <(echo "$ZH_FILES") <(echo "$EN_FILES") | sed 's/^/    /'
  DRIFT=1
fi

# 2. Section count parity per file pair
echo ""
echo "[2] Section count parity (h2+ headings per file pair)..."
while IFS= read -r rel_path; do
  if [ -z "$rel_path" ]; then continue; fi
  ZH_COUNT=$(grep -c '^##' "$DOCS_DIR/zh-CN/$rel_path" 2>/dev/null || echo 0)
  EN_COUNT=$(grep -c '^##' "$DOCS_DIR/en/$rel_path" 2>/dev/null || echo 0)
  if [ "$ZH_COUNT" != "$EN_COUNT" ]; then
    echo "  MISMATCH: $rel_path — zh-CN: $ZH_COUNT headings, en: $EN_COUNT headings"
    DRIFT=1
  fi
done <<< "$ZH_FILES"

if [ "$DRIFT" -eq 0 ]; then
  echo "  PASS: All file pairs have matching heading counts"
fi

# 3. Stale translation marker detection
echo ""
echo "[3] Stale translation markers..."
STALE=$(grep -rl '<!-- TODO: sync with' "$DOCS_DIR" 2>/dev/null || true)
if [ -n "$STALE" ]; then
  echo "  WARNING: Stale translation markers found (warning only, not a hard fail):"
  echo "$STALE" | sed 's/^/    /'
else
  echo "  PASS: No stale translation markers"
fi

echo ""
if [ "$DRIFT" -eq 0 ]; then
  echo "=== ALL CHECKS PASSED ==="
  exit 0
else
  echo "=== CHECKS FAILED ==="
  exit 1
fi
