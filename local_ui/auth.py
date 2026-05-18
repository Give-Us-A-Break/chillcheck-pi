"""
ChillCheck — Local UI Authentication
=====================================
Single-account auth for the Pi's installer console. The threat model is
"someone else on the same LAN should not be able to pair sensors, reboot
the Pi, or read journal logs" — typical for cafe/restaurant deployments
where customer Wi-Fi sits alongside the staff network.

Username is fixed to "admin". Default password is "chillcheck"; on first
successful login the user is forced to change it before any other action
is permitted. Auth state lives in /var/lib/chillcheck/ui-auth.json, which
the local UI service can read and write without sudo.

If the password is forgotten, the device is reflashed — no recovery flow
by design.
"""

import hashlib
import json
import os
import secrets
from pathlib import Path

AUTH_FILE = os.getenv("LOCAL_UI_AUTH_FILE", "/var/lib/chillcheck/ui-auth.json")

USERNAME = "admin"
DEFAULT_PASSWORD = "chillcheck"

PBKDF2_ITERATIONS = 200_000
PBKDF2_ALGO = "sha256"
SALT_BYTES = 16
SESSION_SECRET_BYTES = 32


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        PBKDF2_ALGO, password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()


def load_or_init() -> dict:
    """Return current auth state, creating the file with defaults if missing.

    Shape:
        {
            "session_secret": "<hex>",
            "salt": "<hex>" | None,
            "password_hash": "<hex>" | None,
            "must_change": bool,
        }

    When ``password_hash`` is None the default password ("chillcheck") is
    accepted and ``must_change`` is True — first-run state.
    """
    p = Path(AUTH_FILE)
    if p.exists():
        try:
            state = json.loads(p.read_text())
            # Defensive: tolerate older state files missing newer fields.
            state.setdefault("session_secret", secrets.token_hex(SESSION_SECRET_BYTES))
            state.setdefault("salt", None)
            state.setdefault("password_hash", None)
            state.setdefault("must_change", state["password_hash"] is None)
            return state
        except (json.JSONDecodeError, OSError):
            pass

    state = {
        "session_secret": secrets.token_hex(SESSION_SECRET_BYTES),
        "salt": None,
        "password_hash": None,
        "must_change": True,
    }
    _save(state)
    return state


def _save(state: dict):
    p = Path(AUTH_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)


def verify_password(password: str, state: dict) -> bool:
    """Return True if the password matches the current state.

    When no hash is set (first-run), only the default password is accepted.
    """
    if state.get("password_hash") is None:
        return secrets.compare_digest(password, DEFAULT_PASSWORD)
    salt = state.get("salt") or ""
    candidate = _hash_password(password, salt)
    return secrets.compare_digest(candidate, state["password_hash"])


def set_password(state: dict, new_password: str) -> dict:
    """Persist a new password and clear the must_change flag."""
    salt = secrets.token_hex(SALT_BYTES)
    state["salt"] = salt
    state["password_hash"] = _hash_password(new_password, salt)
    state["must_change"] = False
    _save(state)
    return state


def validate_new_password(password: str) -> str | None:
    """Return a human-readable error message, or None if the password is OK."""
    if not isinstance(password, str):
        return "Password is required."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if password == DEFAULT_PASSWORD:
        return "Choose a password other than the default."
    if len(password) > 200:
        return "Password is too long."
    return None
