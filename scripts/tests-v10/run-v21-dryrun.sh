#!/bin/bash
# v21 dry-run: sweep the REAL source tree (worst case: everything incl.
# docs/tests) to prove no false positives. Must print SWEEP PASSED.
D="$(cd "$(dirname "$0")" && pwd)"
exec /tmp/pwsh74/pwsh -NoProfile -File "$D/v21-sweep-dryrun.ps1"
