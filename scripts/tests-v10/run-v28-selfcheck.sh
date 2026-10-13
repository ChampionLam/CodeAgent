#!/bin/bash
# v28 selfcheck runner (uses the pinned pwsh 7.4 from /tmp/pwsh74,
# same workaround as run-v24-selfcheck.sh for the terminal approval gate)
D="$(cd "$(dirname "$0")" && pwd)"
if [ -x /tmp/pwsh74/pwsh ]; then
    exec /tmp/pwsh74/pwsh -NoProfile -File "$D/v28-selfcheck.ps1"
fi
exec pwsh -NoProfile -File "$D/v28-selfcheck.ps1"
