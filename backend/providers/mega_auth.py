from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from mega.api import MegaAPI
from mega.auth import AuthInfo, _decrypt_credentials
from mega.client import MegaNzClient
from mega.crypto import (
    b64_url_decode,
    b64_url_encode,
    generate_v1_hash,
    prepare_v1_key,
    str_to_a32,
)
from mega.errors import RequestError
from mega.vault import MegaVault

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _auth_info(email: str, password: str, version: int, salt: str | None) -> AuthInfo:
    if version == 2 and salt:
        derived_key = hashlib.pbkdf2_hmac(
            hash_name="sha512",
            password=password.encode("utf-8"),
            salt=b64_url_decode(salt),
            iterations=100_000,
            dklen=32,
        )
        aes_key = str_to_a32(derived_key[:16])
        user_hash = b64_url_encode(derived_key[-16:])
    elif version == 1:
        aes_key = prepare_v1_key(password)
        user_hash = generate_v1_hash(email, aes_key)
    else:
        raise RuntimeError(f"Account version not supported: {version}")
    return AuthInfo(email, aes_key, user_hash)  # type: ignore[arg-type]


def _map_login_error(exc: RequestError) -> ValueError:
    code = exc.code
    if code == -9:
        return ValueError(
            "Invalid MEGA email or password. "
            "If you sign in on mega.nz with Google/Apple, set a MEGA password under "
            "Account settings → Security. If 2FA is enabled, enter the authenticator code."
        )
    if code == -26:
        return ValueError(
            "MEGA requires a 2FA code. Enter the 6-digit code from your authenticator app."
        )
    if code == -16:
        return ValueError("MEGA account is blocked or suspended.")
    return ValueError(f"MEGA login failed: {exc}")


async def mega_login(email: str, password: str, mfa: str | None = None) -> MegaNzClient:
    """Login with optional MFA. Raises RequestError / ValueError with clear messages."""
    email = email.strip().lower()
    client = MegaNzClient(user_agent=BROWSER_UA)
    api: MegaAPI = client._api
    try:
        account = await api.post({"a": "us0", "user": email})
        if not isinstance(account, dict) or "v" not in account:
            raise ValueError("Unexpected MEGA pre-login response")
        auth = await asyncio.to_thread(
            _auth_info,
            email,
            password,
            int(account["v"]),
            account.get("s"),
        )
        payload: dict[str, Any] = {
            "a": "us",
            "user": auth.email,
            "uh": auth.hash,
        }
        if mfa and mfa.strip():
            payload["mfa"] = mfa.strip()

        credentials = await api.post(payload)
        master_key, session_id = await asyncio.to_thread(
            _decrypt_credentials,
            credentials["csid"],
            credentials["k"],
            credentials["privk"],
            auth.aes_key,
        )
        api.session_id = session_id
        client._core.vault = MegaVault(master_key)
        return client
    except RequestError as exc:
        await client.aclose()
        raise _map_login_error(exc) from exc
    except Exception:
        await client.aclose()
        raise


async def verify_mega_login(email: str, password: str, mfa: str | None = None) -> None:
    client = await mega_login(email, password, mfa)
    await client.aclose()
