"""Encryption for stored logins and PDF passwords (spec §5.5).

A Fernet key lives in an owner-only file (default ~/.portfolio-agent/secret.key,
mode 600; folder mode 700). Keychain is deliberately not used (spec D18).
Losing the key file means re-entering the passwords; nothing else is affected.
"""

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config


class SecretError(Exception):
    pass


def _key(path: Path | None = None) -> bytes:
    path = Path(path or config.KEY_PATH)
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise SecretError(f"{path} is readable by other users (mode {mode:o}); "
                              "run: chmod 600 " + str(path))
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def encrypt(data: dict, key_path: Path | None = None) -> bytes:
    return Fernet(_key(key_path)).encrypt(json.dumps(data).encode())


def decrypt(token: bytes, key_path: Path | None = None) -> dict:
    try:
        return json.loads(Fernet(_key(key_path)).decrypt(token))
    except InvalidToken as e:
        raise SecretError("Stored secret can't be decrypted with the current key file; "
                          "re-enter it in Settings.") from e
