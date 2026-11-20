"""HTTP Basic authentication for the admin UI and API."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

ADMIN_USERNAME = "admin"
PASSWORD_FILE_NAME = "admin_password"  # noqa: S105 - a file name, not a secret
REALM = "Ruuvi Dashboard admin"

# Paths that stay reachable without credentials, keyed by HTTP method.
_PUBLIC_ROUTES = {("GET", "/api/health")}


def password_file_path(config_path: str) -> Path:
    """Return where the generated admin password is kept, next to the config."""
    return Path(config_path).parent / PASSWORD_FILE_NAME


def _read_password_file(path: Path) -> str | None:
    try:
        password = path.read_text(encoding="utf-8").rstrip("\r\n")
    except FileNotFoundError:
        return None
    return password if password.strip() else None


def _write_password_file(path: Path, password: str) -> None:
    """Write the password atomically to a file only the owner can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates a fresh file with mode 0600 and never follows a symlink.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{password}\n")
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def resolve_admin_password(config_path: str, env_password: str | None) -> str:
    """Return the admin password, generating and storing one if needed.

    A non-empty ``env_password`` (the ``ADMIN_PASSWORD`` variable) always
    wins. Otherwise the password is read from ``admin_password`` next to the
    config file, which is created with a random password on first start.
    The password itself is never logged.
    """
    if env_password:
        logger.info("Admin UI password taken from ADMIN_PASSWORD")
        return env_password

    path = password_file_path(config_path)
    stored = _read_password_file(path)
    if stored is not None:
        logger.info("Admin UI password read from %s", path)
        return stored

    password = secrets.token_urlsafe(24)
    _write_password_file(path, password)
    logger.warning(
        "Generated an admin UI password and saved it to %s "
        "(config/%s in the project folder with Docker Compose). Log in as "
        "user '%s' with that password, or set ADMIN_PASSWORD to choose your own.",
        path,
        PASSWORD_FILE_NAME,
        ADMIN_USERNAME,
    )
    return password


def _credentials_match(header: bytes, username: bytes, password: bytes) -> bool:
    scheme, _, encoded = header.partition(b" ")
    if scheme.lower() != b"basic":
        return False
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True)
    except binascii.Error:
        return False
    given_user, sep, given_password = decoded.partition(b":")
    if not sep:
        return False
    # Compare both parts every time so timing does not reveal which one failed.
    user_ok = secrets.compare_digest(given_user, username)
    password_ok = secrets.compare_digest(given_password, password)
    return user_ok and password_ok


class BasicAuthMiddleware:
    """Require HTTP Basic credentials for every request except public routes."""

    def __init__(self, app: ASGIApp, password: str) -> None:
        self._app = app
        self._username = ADMIN_USERNAME.encode()
        self._password = password.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass authorized requests through; answer the rest with 401."""
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] == "http" and (
            (scope["method"], scope["path"]) in _PUBLIC_ROUTES
            or self._authorized(scope)
        ):
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (
                        b"www-authenticate",
                        f'Basic realm="{REALM}", charset="UTF-8"'.encode(),
                    ),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b'{"detail":"Unauthorized"}'})

    def _authorized(self, scope: Scope) -> bool:
        for name, value in scope["headers"]:
            if name == b"authorization":
                return _credentials_match(value, self._username, self._password)
        return False
