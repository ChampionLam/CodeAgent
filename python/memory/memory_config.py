"""Configuration, startup assertions and injection budget for the
desktop agent's memory subsystem.

Every value is copied from the approved design note
(the project's design archive, "desktop-agent-memory-mechanism-v1",
lines 19-81). The note's central warning (lines 63/67/70): the memory
package defaults and its config schema defaults contradict each
other, so all eight switches must be pinned explicitly in the
environment handed to the memory subprocess -- never trusted.
Pure stdlib. No network, no third-party imports, no subprocesses.
"""
from __future__ import annotations

import os

# -- The eight mandatory env overrides (note lines 61-68). ------------
# build_env() writes every one unconditionally, so contradictory
# package defaults can never leak through.

# Note line 61: Chinese-tuned embedding model, 512 dims (half of bge-m3).
EMBEDDING_MODEL_KEY = "MNEMOSYNE_EMBEDDING_MODEL"
EMBEDDING_MODEL_DEFAULT = "BAAI/bge-small-zh-v1.5"

# Note line 62: 4x memory saving; frozen at init, changing it later
# requires a restart plus a full reindex.
VEC_TYPE_KEY = "MNEMOSYNE_VEC_TYPE"
VEC_TYPE_DEFAULT = "int8"

# Note line 63: package default "true" (local_llm.py:26) contradicts
# the schema default False (config.py:260) -- must be forced off.
LLM_ENABLED_KEY = "MNEMOSYNE_LLM_ENABLED"
LLM_ENABLED_DEFAULT = "false"

# Note line 64: read default is "" (embeddings.py:141), unlike the LLM
# switch; pin "0" so embeddings stay local and cannot be flipped on.
EMBEDDINGS_VIA_API_KEY = "MNEMOSYNE_EMBEDDINGS_VIA_API"
EMBEDDINGS_VIA_API_DEFAULT = "0"

# Note line 65: already false by default; pinned against silent drift.
HOST_LLM_ENABLED_KEY = "MNEMOSYNE_HOST_LLM_ENABLED"
HOST_LLM_ENABLED_DEFAULT = "false"

# Note line 66: injection count cap, default 5 (the memory package's
# own prefetch default, 5).
INJECTION_TOP_K_KEY = "MNEMOSYNE_PREFETCH_TOP_K"
INJECTION_TOP_K_DEFAULT = "5"

# Note line 67: per-item cap; the memory package's runtime default "0" means
# NO truncation, while its config schema default is 2000 -- contradictory,
# so pin the schema value.
INJECTION_ITEM_CHARS_KEY = "MNEMOSYNE_PREFETCH_CONTENT_CHARS"
INJECTION_ITEM_CHARS = 2000
INJECTION_ITEM_CHARS_DEFAULT = str(INJECTION_ITEM_CHARS)

# Note line 68: total injection budget, aligned with the context-engine
# boundary used by the reference architecture (context window guard).
# Our own boundary, not a memory package knob, hence the DESKAGENT_ prefix.
INJECTION_TOTAL_CHARS_KEY = "DESKAGENT_MEMORY_CONTEXT_MAX_CHARS"
MEMORY_CONTEXT_MAX_CHARS = 6000
INJECTION_TOTAL_CHARS_DEFAULT = str(MEMORY_CONTEXT_MAX_CHARS)

# -- Injection budget constants (note line 68; context_engine.py:34-37).
MEMORY_CONTEXT_HEAD_CHARS = 4000  # leading chars kept when over budget
MEMORY_CONTEXT_TAIL_CHARS = 1500  # trailing chars kept when over budget
TRUNCATION_MARKER_TEMPLATE = "\n[... %d chars of memory truncated ...]\n"
# Marker budget is 6000-4000-1500=500; injection_budget() trims further
# if a rendered marker would exceed it. Appended to a single over-long
# item (note line 67); deliberately not "[..."-prefixed so it stays
# distinguishable from the total-budget marker.
ITEM_TRUNCATION_MARKER = " [item truncated]"
ITEM_SEPARATOR = "\n\n"  # separator between items in the joined block

# -- The four startup assertions (note lines 76-79): any single failure
# refuses to start the memory subprocess -- never run while sick.
ASSERT_LLM_DISABLED = "llm_disabled"            # note line 76
ASSERT_EMBEDDINGS_LOCAL = "embeddings_local"    # note line 77
ASSERT_NO_REMOTE_LLM = "no_remote_llm"          # note line 78
ASSERT_IMPORTER_DISABLED = "importer_disabled"  # note line 79

# Note line 79: importer switches that must stay off; the note fixes
# no exact key names, so cover the obvious spellings.
IMPORTER_ENV_KEYS = (
    "MNEMOSYNE_IMPORTER_ENABLED",
    "MNEMOSYNE_IMPORT_ENABLED",
    "MNEMOSYNE_DATA_IMPORTER_ENABLED",
)

# Unknown non-empty strings parse as enabled: fail closed, so a typo
# refuses startup instead of silently passing.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n", "f", ""})
# The eight overrides as (key, value) pairs, in note-table order.
_BUILD_OVERRIDES = (
    (EMBEDDING_MODEL_KEY, EMBEDDING_MODEL_DEFAULT),
    (VEC_TYPE_KEY, VEC_TYPE_DEFAULT),
    (LLM_ENABLED_KEY, LLM_ENABLED_DEFAULT),
    (EMBEDDINGS_VIA_API_KEY, EMBEDDINGS_VIA_API_DEFAULT),
    (HOST_LLM_ENABLED_KEY, HOST_LLM_ENABLED_DEFAULT),
    (INJECTION_TOP_K_KEY, INJECTION_TOP_K_DEFAULT),
    (INJECTION_ITEM_CHARS_KEY, INJECTION_ITEM_CHARS_DEFAULT),
    (INJECTION_TOTAL_CHARS_KEY, INJECTION_TOTAL_CHARS_DEFAULT),
)


def _truthy(value) -> bool:
    """Parse an env-style value; unknown non-empty text is enabled."""
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return bool(text)


def _is_remote_llm_key(key) -> bool:
    """True for env keys configuring a remote LLM endpoint (note line
    78): key name contains LLM plus URL or ENDPOINT."""
    upper = str(key).upper()
    return "LLM" in upper and ("URL" in upper or "ENDPOINT" in upper)


# Where the embedding model lands. Fastembed and the HF stack each have their
# own default, and on Windows those defaults are under the user profile on C:
# -- which is the one drive we must not fill. Pinning them under the engine
# home keeps the download on the drive the app already uses.
MODEL_CACHE_KEYS = ("FASTEMBED_CACHE_PATH", "HF_HOME", "HF_HUB_CACHE",
                    "TRANSFORMERS_CACHE", "SENTENCE_TRANSFORMERS_HOME")

# The engine's data directory. Verified against the installed package
# (mnemosyne/core/banks.py reads MNEMOSYNE_DATA_DIR); getting this wrong is
# silent -- the database just appears somewhere else.
DATA_DIR_KEY = "MNEMOSYNE_DATA_DIR"


def build_env(base_env: dict, *, home: str = None) -> dict:
    """Return a copy of base_env with the eight overrides applied.

    Overrides are unconditional: caller-supplied opposite values are
    replaced, never merged (note lines 63/67 -- package and schema
    defaults contradict, so nothing may pass through un-pinned).
    The input dict is not mutated.
    """
    env = dict(base_env or {})
    for key, value in _BUILD_OVERRIDES:
        env[key] = value
    if home:
        # The engine reads MNEMOSYNE_DATA_DIR; MNEMOSYNE_HOME is not consulted
        # and an earlier version of this file set only that, so the database
        # quietly landed in the user profile on C: instead of the app's drive.
        # Both are set now (harmless, and it keeps older builds working).
        env[DATA_DIR_KEY] = home
        env["MNEMOSYNE_HOME"] = home
        # Unlike the safety overrides above, an explicit caller value wins here:
        # where to keep a model is a preference, not a safety property.
        cache = os.path.join(home, "models")
        for key in MODEL_CACHE_KEYS:
            if not env.get(key):
                env[key] = cache
    return env


def assert_safe(env: dict) -> list:
    """Run the four startup assertions (note lines 76-79).

    Returns human-readable failure reasons; an empty list means the
    environment is safe to start the memory subprocess.
    """
    failures = []
    env = env or {}
    if _truthy(env.get(LLM_ENABLED_KEY)):
        failures.append("%s: %s must parse to false (note line 76)"
                        % (ASSERT_LLM_DISABLED, LLM_ENABLED_KEY))
    if _truthy(env.get(EMBEDDINGS_VIA_API_KEY)):
        failures.append("%s: %s must stay off so embeddings run locally"
                        " (note line 77)"
                        % (ASSERT_EMBEDDINGS_LOCAL, EMBEDDINGS_VIA_API_KEY))
    for key, value in env.items():
        if value and _is_remote_llm_key(key):
            failures.append("%s: remote LLM endpoint configured via %s"
                            " (note line 78)" % (ASSERT_NO_REMOTE_LLM, key))
    for key in IMPORTER_ENV_KEYS:
        if _truthy(env.get(key)):
            failures.append("%s: %s must stay off (note line 79)"
                            % (ASSERT_IMPORTER_DISABLED, key))
    return failures


def _truncate_item(text: str, limit: int) -> tuple:
    """Cap one memory item at limit chars, marking the cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit] + ITEM_TRUNCATION_MARKER, True


def injection_budget(texts: list, total: int = None) -> tuple:
    """Assemble the memory injection block under the note's budget.

    Step 1 (note line 67): every item capped at INJECTION_ITEM_CHARS
    (package default 0 means no truncation). Step 2 (note line 68):
    the joined block is capped at MEMORY_CONTEXT_MAX_CHARS, keeping
    MEMORY_CONTEXT_HEAD_CHARS head + MEMORY_CONTEXT_TAIL_CHARS tail
    with a truncation marker between; the result never exceeds the
    total budget. Returns (block, truncated).
    """
    items = []
    truncated = False
    for text in texts or []:
        item, was_cut = _truncate_item(str(text), INJECTION_ITEM_CHARS)
        truncated = truncated or was_cut
        items.append(item)
    # `total` is the caller's effective budget (an operator override can move
    # it); the note's constant stays the default so the pinned numbers hold.
    budget = int(total) if total else MEMORY_CONTEXT_MAX_CHARS
    joined = ITEM_SEPARATOR.join(items)
    if len(joined) <= budget:
        return joined, truncated
    head = joined[:MEMORY_CONTEXT_HEAD_CHARS]
    tail = joined[len(joined) - MEMORY_CONTEXT_TAIL_CHARS:]
    marker = TRUNCATION_MARKER_TEMPLATE % (len(joined) - len(head) - len(tail))
    # Keep head + marker + tail within budget; trim the tail first,
    # then the head, if the marker pushes it over.
    excess = len(head) + len(marker) + len(tail) - budget
    if excess > 0:
        if excess <= len(tail):
            tail = tail[excess:]
        else:
            head = head[: len(head) - (excess - len(tail))]
            tail = ""
    return head + marker + tail, True
