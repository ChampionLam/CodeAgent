"""User-editable agent files: identity and workspace instructions.

The reference agent (Hermes) keeps these as markdown layers (SOUL.md /
AGENTS.md); the desktop
agent had neither, so its persona was a hard-coded constant and there was no
way to give the agent project rules. Both live here:

  * identity     -- ``~/.desktop-agent/identity.md``. Absent or empty means
                    "use the built-in BASE_ROLE text" (see prompt_build).
  * instructions -- ``AGENTS.md`` (or ``WORKBUDDY.md``) in the workspace root,
                    i.e. the project's own rules, kept in the project tree so
                    it can be diffed, versioned and shared.

Both are read from disk on every turn, so editing the file takes effect on
the next message with no restart. Neither is written by the agent: these are
user-authored layers.

Character budgets: the design docs fixed the memory-injection budget, not
these. The numbers below are this implementation's choice and are enforced
with a visible marker rather than a silent cut.
"""
from __future__ import annotations

import os

IDENTITY_FILENAME = "identity.md"
INSTRUCTION_FILENAMES = ("AGENTS.md", "WORKBUDDY.md")

IDENTITY_MAX_CHARS = 4000
INSTRUCTIONS_MAX_CHARS = 6000
# Hard rules are not a layer of their own: they live in the identity (SOUL)
# file, which is rendered into every turn anyway. The agent may append a rule
# there -- the one file it is allowed to write -- and the budget is enforced by
# REFUSING a new rule, never by dropping an existing one: a hard rule that
# silently disappears is worse than one that could not be saved.
BULLET = "- "

TRUNCATION_MARKER = "\n\n…（内容超长，此处截断）"


def agent_home(home: str | None = None) -> str:
    """The agent's own data directory (same convention as skills_registry)."""
    if home:
        return home
    return os.path.join(os.path.expanduser("~"), ".desktop-agent")


def identity_path(home: str | None = None) -> str:
    return os.path.join(agent_home(home), IDENTITY_FILENAME)


def load_identity(home: str | None = None) -> str | None:
    """Return the user's identity text, or None when there is none.

    Missing file, unreadable file and whitespace-only content all mean the
    caller should fall back to the built-in role text -- never an error.
    """
    return _read_capped(identity_path(home), IDENTITY_MAX_CHARS)


def instruction_path(workspace: str | None) -> str | None:
    """First instruction file found in the workspace root, if any."""
    if not workspace or not os.path.isdir(workspace):
        return None
    for name in INSTRUCTION_FILENAMES:
        candidate = os.path.join(workspace, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def load_instructions(workspace: str | None) -> str | None:
    """Project rules for this workspace, or None when the project has none."""
    path = instruction_path(workspace)
    if path is None:
        return None
    return _read_capped(path, INSTRUCTIONS_MAX_CHARS)


def _read_capped(path: str, cap: int) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    return cap_text(text, cap)


# --------------------------------------------------------------- hard rules


def list_rules(home: str | None = None) -> list[str]:
    """Hard rules the identity file states as bullets ("- ..."), in order.

    The identity file is free-form: prose describes who the agent is, bullets
    state what it must always do. Only the bullets are rules.
    """
    try:
        with open(identity_path(home), "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text.startswith(BULLET):
            continue
        text = text[len(BULLET):].strip()
        if text and text not in out:
            out.append(text)
    return out


def append_rule(text: str, home: str | None = None) -> tuple[bool, str]:
    """Append one hard rule to the identity file. Returns (ok, reason).

    Reasons: "saved", "already", "empty", "too_long", "full". The user's own
    prose is never rewritten -- the rule is appended, so hand-written persona
    text stays exactly as authored.
    """
    rule = " ".join((text or "").split())
    if not rule:
        return False, "empty"
    line = BULLET + rule
    if len(line) + 1 > IDENTITY_MAX_CHARS:
        return False, "too_long"
    try:
        with open(identity_path(home), "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        raw = ""
    if rule in list_rules(home):
        return False, "already"
    if len(raw) + len(line) + 2 > IDENTITY_MAX_CHARS:
        return False, "full"
    body = raw.rstrip("\n") + "\n" if raw.strip() else ""
    return _atomic_write(identity_path(home), body + line + "\n"), "saved"


def remove_rule(text: str, home: str | None = None) -> tuple[bool, str]:
    """Drop one hard rule line from the identity file. (ok, reason)."""
    target = " ".join((text or "").split())
    if not target:
        return False, "empty"
    rules = list_rules(home)
    matches = [r for r in rules if r == target]
    if not matches:
        matches = [r for r in rules if target in r]
        if len(matches) != 1:
            return False, "not_found" if not matches else "ambiguous"
    try:
        with open(identity_path(home), "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False, "not_found"
    kept = [ln for ln in lines
            if ln.strip()[len(BULLET):].strip() not in matches]
    ok = _atomic_write(identity_path(home), "\n".join(kept).rstrip("\n") + "\n")
    return ok, "removed" if ok else "not_found"


def _atomic_write(path: str, body: str) -> bool:
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


def cap_text(text: str, cap: int) -> str | None:
    """Trim to ``cap`` characters, keeping a visible truncation marker."""
    if text is None:
        return None
    body = text.strip()
    if not body:
        return None
    if len(body) <= cap:
        return body
    room = max(0, cap - len(TRUNCATION_MARKER))
    return body[:room].rstrip() + TRUNCATION_MARKER