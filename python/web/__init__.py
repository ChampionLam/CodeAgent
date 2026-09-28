"""Outbound web access for the desktop agent.

Two tools live behind this package: ``web_fetch`` (fetch one URL and hand back
readable text) and ``web_search`` (query a configured backend). Both never ask
for approval (2026-09-25: 分级已取消) — so the safety has to live in code
rather than in a user confirmation dialog.

Layout:

* ``egress``  — URL policy, DNS pinning, redirect control, byte cap.
* ``extract`` — HTML / JSON -> readable text.
* ``search``  — pluggable search backend, no key required to import.

Nothing here imports ``tools``/``permissions``; the tool wrappers do that.
"""