import hmac
import secrets
import time
from pathlib import Path

import bcrypt
from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE_NAME = "dw_session"


class LoginGuard:
    """Compte les échecs par adresse IP et bloque temporairement après le seuil."""

    def __init__(self, max_failures: int, lock_seconds: int):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def locked_for(self, client: str) -> int:
        remaining = self._locked_until.get(client, 0) - time.time()
        return int(remaining) + 1 if remaining > 0 else 0

    def fail(self, client: str) -> None:
        now = time.time()
        recent = [t for t in self._failures.get(client, []) if now - t < self.lock_seconds]
        recent.append(now)
        if len(recent) >= self.max_failures:
            self._locked_until[client] = now + self.lock_seconds
            recent = []
        self._failures[client] = recent

    def success(self, client: str) -> None:
        self._failures.pop(client, None)
        self._locked_until.pop(client, None)


MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def compose_escape(value: str) -> str:
    # Compose interprète « $ » dans docker-compose.yml : il faut le doubler.
    return value.replace("$", "$$")


def load_or_create_secret(path: Path) -> str:
    """Clé de session générée au premier démarrage, pour ne pas avoir à la fournir."""
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_urlsafe(48)
    path.write_text(secret)
    path.chmod(0o600)
    return secret


def verify_credentials(username: str, password: str, expected_user: str, password_hash: str) -> bool:
    # bcrypt tourne même si l'identifiant est faux, pour ne pas révéler lequel est en cause.
    try:
        password_ok = bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        password_ok = False
    user_ok = hmac.compare_digest(username.encode(), expected_user.encode())
    return password_ok and user_ok


class Sessions:
    def __init__(self, secret_key: str, max_age: int):
        self.serializer = URLSafeTimedSerializer(secret_key, salt="domain-watch-session")
        self.max_age = max_age

    def create(self, username: str) -> str:
        return self.serializer.dumps({"u": username})

    def valid(self, token: str | None, username: str) -> bool:
        if not token:
            return False
        try:
            data = self.serializer.loads(token, max_age=self.max_age)
        except BadSignature:
            return False
        return isinstance(data, dict) and data.get("u") == username
