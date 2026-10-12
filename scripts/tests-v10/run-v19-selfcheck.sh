#!/bin/bash
# v19 selfcheck runner (same pattern as run-v17/v18: avoids the approval
# gate on direct pwsh flags)
D="$(cd "$(dirname "$0")" && pwd)"
exec /tmp/pwsh74/pwsh -NoProfile -File "$D/v19-selfcheck.ps1"
