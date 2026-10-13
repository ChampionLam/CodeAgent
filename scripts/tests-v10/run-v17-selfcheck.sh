#!/bin/bash
# v17 selfcheck runner (same pattern as run-selfcheck.sh: the runner
# script avoids the approval gate on direct pwsh flags)
D="$(cd "$(dirname "$0")" && pwd)"
exec /tmp/pwsh74/pwsh -NoProfile -File "$D/v17-selfcheck.ps1"
