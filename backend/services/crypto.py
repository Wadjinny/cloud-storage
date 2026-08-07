from __future__ import annotations

import base64
import json
import os
from typing import Any

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERIFIER_PLAINTEXT = b"cloud-storage-vault-v1"
MASTER_KEY_LEN = 32
FILE_KEY_LEN = 32
NONCE_LEN = 12


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii"))


def new_salt() -> bytes:
    return os.urandom(16)


def derive_master_key(passphrase: str, salt: bytes) -> bytes:
    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=3,
        memory_cost=64 * 1024,
        parallelism=2,
        hash_len=MASTER_KEY_LEN,
        type=Type.ID,
    )


def encrypt(key: bytes, plaintext: bytes, nonce: bytes | None = None) -> tuple[bytes, bytes]:
    nonce = nonce or os.urandom(NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return ciphertext, nonce


def decrypt(key: bytes, ciphertext: bytes, nonce: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, ciphertext, None)


def encrypt_chunk(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt one plaintext chunk. Returns (ciphertext_with_tag, nonce)."""
    return encrypt(key, plaintext)


def decrypt_chunk(key: bytes, ciphertext: bytes, nonce: bytes) -> bytes:
    """Decrypt one chunk produced by encrypt_chunk."""
    return decrypt(key, ciphertext, nonce)


ALG_CHUNK = "AES-256-GCM-CHUNK"


def make_verifier(master_key: bytes) -> str:
    ciphertext, nonce = encrypt(master_key, VERIFIER_PLAINTEXT)
    return b64e(nonce + ciphertext)


def check_verifier(master_key: bytes, verifier: str) -> bool:
    raw = b64d(verifier)
    nonce, ciphertext = raw[:NONCE_LEN], raw[NONCE_LEN:]
    try:
        return decrypt(master_key, ciphertext, nonce) == VERIFIER_PLAINTEXT
    except Exception:
        return False


def new_file_key() -> bytes:
    return os.urandom(FILE_KEY_LEN)


def wrap_file_key(master_key: bytes, file_key: bytes) -> str:
    ciphertext, nonce = encrypt(master_key, file_key)
    return b64e(nonce + ciphertext)


def unwrap_file_key(master_key: bytes, wrapped: str) -> bytes:
    raw = b64d(wrapped)
    nonce, ciphertext = raw[:NONCE_LEN], raw[NONCE_LEN:]
    return decrypt(master_key, ciphertext, nonce)


def seal_json(key: bytes, obj: Any) -> str:
    """Encrypt a JSON-serializable object; returns urlsafe-b64(nonce||ciphertext)."""
    plaintext = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ciphertext, nonce = encrypt(key, plaintext)
    return b64e(nonce + ciphertext)


def open_json(key: bytes, sealed: str) -> Any:
    """Decrypt a blob produced by seal_json."""
    raw = b64d(sealed)
    nonce, ciphertext = raw[:NONCE_LEN], raw[NONCE_LEN:]
    plaintext = decrypt(key, ciphertext, nonce)
    return json.loads(plaintext.decode("utf-8"))


def seal_bytes(key: bytes, plaintext: bytes) -> bytes:
    """Encrypt to an opaque binary blob: nonce || ciphertext(+tag)."""
    ciphertext, nonce = encrypt(key, plaintext)
    return nonce + ciphertext


def open_bytes(key: bytes, blob: bytes) -> bytes:
    """Decrypt a blob produced by seal_bytes."""
    nonce, ciphertext = blob[:NONCE_LEN], blob[NONCE_LEN:]
    return decrypt(key, ciphertext, nonce)
