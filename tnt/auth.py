"""Users, roles, sessions and electronic signatures (21 CFR Part 11 §11.10(d), §11.200, §11.300)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from .core import Forbidden, Invalid, NotFound, TnTError, TrackTrace, Unauthorized

ROLES = {"viewer": 0, "operator": 1, "qa": 2, "admin": 3}
MAX_FAILED = 5
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
    except ValueError:
        return False
    test = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(test.hex(), dk)


_DUMMY_HASH = hash_password(secrets.token_hex(8))  # equalises timing for unknown usernames


def password_policy(password: str) -> None:
    if (len(password) < 10 or not any(ch.isdigit() for ch in password)
            or not any(ch.isalpha() for ch in password)):
        raise Invalid("password must be at least 10 characters and contain letters and digits")


class Auth:
    def __init__(self, tt: TrackTrace, idle_minutes: int = 30, session_hours: int = 12):
        self.tt = tt
        self.idle = timedelta(minutes=idle_minutes)
        self.max_age = timedelta(hours=session_hours)

    def _audit(self, c, step, actor, data):
        self.tt._record(c, "SystemEvent", "OBSERVE", step, None, actor, data, [])

    # ----------------------------------------------------------- users
    def has_users(self) -> bool:
        return bool(self.tt.db.reader().execute("SELECT 1 FROM users LIMIT 1").fetchone())

    def create_user(self, username, full_name, password, role, actor="system", must_change=True):
        if role not in ROLES:
            raise Invalid(f"role must be one of {list(ROLES)}")
        if not username or not username.replace(".", "").replace("_", "").replace("-", "").isalnum():
            raise Invalid("username may contain letters, digits, '.', '_' and '-'")
        password_policy(password)
        now = _now().isoformat()
        with self.tt._tx() as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise TnTError(f"user {username} already exists")
            c.execute("INSERT INTO users(username,full_name,pw_hash,role,must_change_pw,created_at,pw_changed_at)"
                      " VALUES(?,?,?,?,?,?,?)",
                      (username, full_name, hash_password(password), role, int(must_change), now, now))
            self._audit(c, "user_created", actor, {"username": username, "role": role})
        return self.get_user(username)

    def get_user(self, username):
        row = self.tt.db.reader().execute(
            "SELECT id, username, full_name, role, active, failed_attempts, must_change_pw, created_at, pw_changed_at"
            " FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise NotFound(f"unknown user {username}")
        return dict(row) | {"locked": row["failed_attempts"] >= MAX_FAILED}

    def list_users(self):
        rows = self.tt.db.reader().execute(
            "SELECT id, username, full_name, role, active, failed_attempts, must_change_pw, created_at"
            " FROM users ORDER BY username")
        return [dict(r) | {"locked": r["failed_attempts"] >= MAX_FAILED} for r in rows]

    def update_user(self, username, actor, role=None, active=None, full_name=None, unlock=False):
        """Users are never deleted (audit trail attribution); deactivate instead."""
        if role is not None and role not in ROLES:
            raise Invalid(f"role must be one of {list(ROLES)}")
        changes = {k: v for k, v in {"role": role, "active": active, "full_name": full_name}.items() if v is not None}
        with self.tt._tx() as c:
            row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                raise NotFound(f"unknown user {username}")
            for col, val in changes.items():
                c.execute(f"UPDATE users SET {col}=? WHERE id=?", (int(val) if col == "active" else val, row["id"]))
            if unlock:
                c.execute("UPDATE users SET failed_attempts=0 WHERE id=?", (row["id"],))
            if active is False or role is not None:
                c.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
            self._audit(c, "user_updated", actor, {"username": username, **changes, "unlock": unlock})
        return self.get_user(username)

    def set_password(self, username, new_password, actor, temporary=False):
        password_policy(new_password)
        with self.tt._tx() as c:
            row = c.execute("SELECT id, pw_hash FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                raise NotFound(f"unknown user {username}")
            if check_password(new_password, row["pw_hash"]):
                raise Invalid("new password must differ from the current one")
            c.execute("UPDATE users SET pw_hash=?, must_change_pw=?, failed_attempts=0, pw_changed_at=? WHERE id=?",
                      (hash_password(new_password), int(temporary), _now().isoformat(), row["id"]))
            self._audit(c, "password_reset" if temporary else "password_changed", actor, {"username": username})

    def change_own_password(self, username, old_password, new_password):
        self._check_credentials(username, old_password, "password_change")
        self.set_password(username, new_password, actor=username)

    # -------------------------------------------------------- sessions
    def _check_credentials(self, username, password, purpose):
        """Verify a password; failures count toward lockout. Returns the user row."""
        with self.tt._tx() as c:
            row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            ok = check_password(password, row["pw_hash"] if row else _DUMMY_HASH)
            if row and ok and row["active"] and row["failed_attempts"] < MAX_FAILED:
                c.execute("UPDATE users SET failed_attempts=0 WHERE id=?", (row["id"],))
                return row
            if row:
                c.execute("UPDATE users SET failed_attempts=failed_attempts+1 WHERE id=?", (row["id"],))
            self._audit(c, "auth_failed", username or "?", {"purpose": purpose})
        if row and row["failed_attempts"] + 1 >= MAX_FAILED:
            raise Unauthorized("account locked - contact an administrator")
        raise Unauthorized("invalid username or password")

    def login(self, username, password, ip=None) -> dict:
        row = self._check_credentials(username, password, "login")
        token = secrets.token_urlsafe(32)
        now = _now()
        with self.tt._tx() as c:
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
            c.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                      (_token_hash(token), row["id"], now.isoformat(), now.isoformat(),
                       (now + self.max_age).isoformat()))
            self._audit(c, "login", row["username"], {"ip": ip})
        return {"token": token, "username": row["username"], "full_name": row["full_name"],
                "role": row["role"], "must_change_password": bool(row["must_change_pw"]),
                "idle_timeout_minutes": int(self.idle.total_seconds() // 60)}

    def logout(self, token, username):
        with self.tt._tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
            self._audit(c, "logout", username, {})

    def authenticate(self, token: str | None, min_role: str = "viewer") -> dict:
        if not token:
            raise Unauthorized("login required")
        th = _token_hash(token)
        row = self.tt.db.reader().execute(
            "SELECT s.last_seen, s.expires_at, u.username, u.full_name, u.role, u.active, u.must_change_pw"
            " FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (th,)).fetchone()
        now = _now()
        if (not row or not row["active"] or row["expires_at"] < now.isoformat()
                or datetime.fromisoformat(row["last_seen"]) + self.idle < now):
            raise Unauthorized("session expired - please log in again")
        if ROLES[row["role"]] < ROLES[min_role]:
            raise Forbidden(f"requires role {min_role} or higher")
        if datetime.fromisoformat(row["last_seen"]) + timedelta(seconds=60) < now:
            with self.tt._tx() as c:  # throttled so reads don't become writes
                c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now.isoformat(), th))
        return dict(row)

    def sign(self, username, password, meaning) -> dict:
        """Electronic signature: re-authenticate and bind signer + meaning + time to the record."""
        if not meaning or not meaning.strip():
            raise Invalid("signature meaning is required (e.g. 'Approved recall')")
        row = self._check_credentials(username, password, "e-signature")
        return {"signer": row["username"], "name": row["full_name"], "meaning": meaning.strip(),
                "signed_at": _now().isoformat(timespec="seconds")}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
