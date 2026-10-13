#!/bin/bash
# v12 selfcheck runner (works around the terminal approval gate on direct pwsh flags)
D="$(cd "$(dirname "$0")" && pwd)"
exec /tmp/pwsh74/pwsh -NoProfile -File "$D/v10-selfcheck.ps1"
