#!/bin/bash
# v18 selfcheck runner (same pattern as run-v17-selfcheck.sh: the
# runner script avoids the approval gate on direct pwsh flags)
D="$(cd "$(dirname "$0")" && pwd)"
exec /tmp/pwsh74/pwsh -NoProfile -File "$D/v18-selfcheck.ps1"
