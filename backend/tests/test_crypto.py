from __future__ import annotations

from services import crypto


def test_encrypt_decrypt_roundtrip() -> None:
    key = crypto.new_file_key()
    plain = b"hello vault"
    ciphertext, nonce = crypto.encrypt_chunk(key, plain)
    assert ciphertext != plain
    assert crypto.decrypt_chunk(key, ciphertext, nonce) == plain


def test_empty_chunk_roundtrip() -> None:
    key = crypto.new_file_key()
    ciphertext, nonce = crypto.encrypt_chunk(key, b"")
    assert crypto.decrypt_chunk(key, ciphertext, nonce) == b""


def test_wrap_unwrap_file_key() -> None:
    master = crypto.new_file_key()
    file_key = crypto.new_file_key()
    wrapped = crypto.wrap_file_key(master, file_key)
    assert crypto.unwrap_file_key(master, wrapped) == file_key


def test_verifier_accepts_matching_key() -> None:
    salt = crypto.new_salt()
    master = crypto.derive_master_key("passphrase", salt)
    verifier = crypto.make_verifier(master)
    assert crypto.check_verifier(master, verifier) is True


def test_verifier_rejects_wrong_key() -> None:
    salt = crypto.new_salt()
    master = crypto.derive_master_key("passphrase", salt)
    other = crypto.derive_master_key("other", salt)
    verifier = crypto.make_verifier(master)
    assert crypto.check_verifier(other, verifier) is False


def test_b64_roundtrip() -> None:
    raw = b"\x00\xff\x10abc"
    assert crypto.b64d(crypto.b64e(raw)) == raw
