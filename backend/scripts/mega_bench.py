#!/usr/bin/env python3
"""Sweep MEGA upload params and rank by stability then speed.

Streams a on-disk payload through ``vault.upload_file`` (same encrypt / place /
provider path as production). Suitable for multi-GiB trials.

Env (from backend/.env or shell):
  CLOUD_STORAGE_TEST_MEGA_EMAIL
  CLOUD_STORAGE_TEST_MEGA_PASSWORD
  CLOUD_STORAGE_TEST_MEGA_MFA (optional)

Example:
  uv run python scripts/mega_bench.py --size-mib 1024 --cooldown 20
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
load_dotenv(_BACKEND / ".env")
os.environ.setdefault("SESSION_SECRET", "mega-bench-session")

BENCH_FOLDER = "cloud-storage-vault-bench"
BENCH_USERNAME = "mega-bench"
PASSPHRASE = "mega-bench-passphrase"


@dataclass
class TrialConfig:
    mega_conc: int
    shard_conc: int
    chunk_mib: int

    @property
    def chunk_size(self) -> int:
        return self.chunk_mib * 1024 * 1024

    def label(self) -> str:
        return f"mega={self.mega_conc} shard={self.shard_conc} chunk={self.chunk_mib}MiB"


@dataclass
class TrialResult:
    config: TrialConfig
    ok: bool
    seconds: float
    eagain: int = 0
    retries: int = 0
    timeouts: int = 0
    shards: int = 0
    error: str = ""
    hiccups: int = field(init=False)

    def __post_init__(self) -> None:
        stall = max(self.eagain, self.retries)
        self.hiccups = stall + self.timeouts + (0 if self.ok else 10)


class _LogCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.eagain = 0
        self.retries = 0
        self.timeouts = 0

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        low = msg.lower()
        if "EAGAIN" in msg:
            self.eagain += 1
        if "retry" in low or "retrying" in low:
            self.retries += 1
        if "timed out" in low or "timeout" in low:
            self.timeouts += 1


def _env(*names: str) -> str | None:
    for name in names:
        raw = os.environ.get(name, "").strip().strip("'\"")
        if raw:
            return raw
    return None


def _make_payload_file(path: Path, size_mib: int) -> None:
    """Write ``size_mib`` MiB without holding the whole file in RAM."""
    frame = os.urandom(1024 * 1024)
    with path.open("wb") as out:
        for i in range(size_mib):
            out.write(frame)
            if size_mib >= 64 and (i + 1) % 64 == 0:
                print(f"  wrote payload {i + 1}/{size_mib} MiB", flush=True)


def _iter_file_chunks(path: Path, chunk_size: int, size: int) -> Iterator[bytes]:
    remaining = size
    with path.open("rb") as src:
        while remaining > 0:
            take = min(chunk_size, remaining)
            piece = src.read(take)
            if not piece:
                break
            remaining -= len(piece)
            yield piece
    if size == 0:
        yield b""


def _apply_config(cfg: TrialConfig) -> None:
    import config
    import providers.mega_session as mega_session
    import services.upload_pipeline as upload_pipeline
    import services.vault as services_vault

    chunk = cfg.chunk_size
    config.CHUNK_SIZE = chunk
    config.CHUNK_SIZE_LARGE = chunk
    config.CHUNK_SIZE_LARGE_THRESHOLD = 2**62
    config.SHARD_UPLOAD_CONCURRENCY = cfg.shard_conc
    config.UPLOAD_QUEUE_DEPTH = 0
    config.MEGA_ACCOUNT_UPLOAD_CONCURRENCY = cfg.mega_conc
    config.SHARD_FOLDER_NAME = BENCH_FOLDER

    services_vault.SHARD_UPLOAD_CONCURRENCY = cfg.shard_conc
    services_vault.MEGA_ACCOUNT_UPLOAD_CONCURRENCY = cfg.mega_conc

    upload_pipeline.SHARD_UPLOAD_CONCURRENCY = cfg.shard_conc
    upload_pipeline.UPLOAD_QUEUE_DEPTH = 0

    mega_session.MEGA_ACCOUNT_UPLOAD_CONCURRENCY = cfg.mega_conc
    mega_session.SHARD_FOLDER_NAME = BENCH_FOLDER
    for key in list(mega_session.mega_sessions._sessions):
        mega_session.mega_sessions.drop(key)


def _setup_data_dir(root: Path) -> None:
    import config
    from storage.db import init_db, reset_for_tests
    from storage.users import create_user, get_by_username

    db_path = root / "app.db"
    os.environ["DATA_DIR"] = str(root)
    os.environ["DB_PATH"] = str(db_path)
    config.DATA_DIR = root
    config.DB_PATH = db_path
    reset_for_tests()
    init_db(db_path)
    if get_by_username(BENCH_USERNAME) is None:
        create_user(BENCH_USERNAME, PASSPHRASE)


def _run_trial(
    cfg: TrialConfig,
    *,
    payload_path: Path,
    payload_size: int,
    email: str,
    password: str,
    mfa: str | None,
) -> TrialResult:
    from providers.mega_session import mega_sessions
    from services import crypto
    from services import vault as vault_service
    from storage import accounts as accounts_store
    from storage import auth_context
    from storage import users as users_store
    from storage.db import init_db, reset_for_tests
    from storage.users import create_user

    _apply_config(cfg)
    mega_sessions.drop(email)

    # Fresh DB per trial so account/file state cannot leak across runs.
    import config

    root = Path(config.DATA_DIR)
    db_path = root / "app.db"
    if db_path.exists():
        db_path.unlink()
    reset_for_tests()
    init_db(db_path)
    create_user(BENCH_USERNAME, PASSPHRASE)
    user = users_store.get_by_username(BENCH_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(PASSPHRASE, crypto.b64d(user["kdf_salt"]))

    counter = _LogCounter()
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(counter)
    for name in ("mega", "services.vault", "providers.mega", "providers.mega_session"):
        logging.getLogger(name).setLevel(logging.DEBUG)

    expected_shards = max(1, (payload_size + cfg.chunk_size - 1) // cfg.chunk_size) if payload_size else 1
    shards = expected_shards
    t0 = time.monotonic()
    error = ""
    ok = False
    last_report = t0

    def on_progress(phase: str, current: int, total: int, message: str) -> None:
        nonlocal last_report
        now = time.monotonic()
        if now - last_report < 15 and phase != "done":
            return
        last_report = now
        pct = (100 * current / total) if total else 0
        print(
            f"    … {phase} {pct:5.1f}% {message} "
            f"eagain={counter.eagain} retries={counter.retries}",
            flush=True,
        )

    try:
        with auth_context.auth_scope(user["id"], master_key):
            credentials = {"email": email, "password": password}
            if mfa:
                credentials["mfa"] = mfa
            accounts_store.add_account(provider="mega", label=email, credentials=credentials)

            filename = f"bench-{cfg.mega_conc}-{cfg.shard_conc}-{cfg.chunk_mib}-{uuid.uuid4().hex[:8]}.bin"
            entry = vault_service.upload_file(
                master_key,
                "/bench",
                filename,
                _iter_file_chunks(payload_path, cfg.chunk_size, payload_size),
                size=payload_size,
                chunk_size=cfg.chunk_size,
                on_progress=on_progress,
            )
            shards = len(entry.get("shards") or [])
            if entry.get("size") != payload_size:
                raise RuntimeError(
                    f"size mismatch stored={entry.get('size')} expected={payload_size}"
                )
            vault_service.delete_file(entry["id"])
        ok = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        ok = False
    finally:
        root_logger.removeHandler(counter)
        root_logger.setLevel(prev_level)
        try:
            mega_sessions.drop(email)
        except Exception:
            pass

    seconds = time.monotonic() - t0
    return TrialResult(
        config=cfg,
        ok=ok,
        seconds=seconds,
        eagain=counter.eagain,
        retries=counter.retries,
        timeouts=counter.timeouts,
        shards=shards,
        error=error,
    )


def _default_configs() -> list[TrialConfig]:
    return [
        TrialConfig(1, 1, 2),
        TrialConfig(1, 1, 4),
        TrialConfig(1, 1, 8),
        TrialConfig(1, 1, 16),
        TrialConfig(1, 2, 4),
        TrialConfig(1, 2, 8),
        TrialConfig(1, 4, 4),
        TrialConfig(1, 4, 8),
        TrialConfig(2, 2, 4),
        TrialConfig(2, 2, 8),
        TrialConfig(2, 4, 4),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=16, help="payload size per trial")
    parser.add_argument("--cooldown", type=float, default=6.0, help="seconds between trials")
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="optional filter regex on config label, e.g. 'mega=1.*chunk=8'",
    )
    parser.add_argument(
        "--payload",
        type=Path,
        default=None,
        help="reuse an existing file instead of generating one",
    )
    args = parser.parse_args()

    email = _env("CLOUD_STORAGE_TEST_MEGA_EMAIL")
    password = _env("CLOUD_STORAGE_TEST_MEGA_PASSWORD")
    if not email or not password:
        print(
            "Set CLOUD_STORAGE_TEST_MEGA_EMAIL and CLOUD_STORAGE_TEST_MEGA_PASSWORD",
            file=sys.stderr,
        )
        return 2
    mfa = _env("CLOUD_STORAGE_TEST_MEGA_MFA")

    configs = _default_configs()
    if args.only:
        rx = re.compile(args.only)
        configs = [c for c in configs if rx.search(c.label())]
    if not configs:
        print("no configs matched", file=sys.stderr)
        return 2

    print(
        f"MEGA bench: {len(configs)} trials, payload={args.size_mib} MiB, "
        f"folder={BENCH_FOLDER}, cooldown={args.cooldown}s",
        flush=True,
    )

    results: list[TrialResult] = []
    with tempfile.TemporaryDirectory(prefix="mega-bench-") as tmp:
        root = Path(tmp)
        _setup_data_dir(root)
        if args.payload is not None:
            payload_path = args.payload.expanduser().resolve()
            if not payload_path.is_file():
                print(f"payload not found: {payload_path}", file=sys.stderr)
                return 2
            payload_size = payload_path.stat().st_size
            print(f"using existing payload {payload_path} ({payload_size} bytes)", flush=True)
        else:
            payload_path = root / f"payload-{args.size_mib}mib.bin"
            print(f"generating payload {payload_path} …", flush=True)
            _make_payload_file(payload_path, args.size_mib)
            payload_size = payload_path.stat().st_size
            print(f"payload ready ({payload_size} bytes)", flush=True)

        for i, cfg in enumerate(configs, 1):
            print(f"\n[{i}/{len(configs)}] {cfg.label()} …", flush=True)
            _setup_data_dir(root)
            result = _run_trial(
                cfg,
                payload_path=payload_path,
                payload_size=payload_size,
                email=email,
                password=password,
                mfa=mfa,
            )
            results.append(result)
            status = "OK" if result.ok else "FAIL"
            mib_s = (payload_size / (1024 * 1024) / result.seconds) if result.seconds > 0 else 0
            print(
                f"  {status} {result.seconds:.1f}s ({mib_s:.2f} MiB/s) shards={result.shards} "
                f"eagain={result.eagain} retries={result.retries} "
                f"timeouts={result.timeouts} hiccups={result.hiccups}"
                + (f" err={result.error[:200]}" if result.error else ""),
                flush=True,
            )
            if i < len(configs) and args.cooldown > 0:
                print(f"  cooldown {args.cooldown:.0f}s …", flush=True)
                time.sleep(args.cooldown)

    print("\n=== Ranked (fewest hiccups, then fastest) ===", flush=True)
    ranked = sorted(results, key=lambda r: (not r.ok, r.hiccups, r.seconds))
    print(
        f"{'rank':<5} {'mega':>4} {'shard':>5} {'chunk':>6} {'ok':>3} "
        f"{'sec':>8} {'MiB/s':>6} {'eagain':>6} {'retry':>6} {'t/o':>4} {'hick':>5}",
        flush=True,
    )
    size_mib = payload_size / (1024 * 1024)
    for i, r in enumerate(ranked, 1):
        mib_s = (size_mib / r.seconds) if r.seconds > 0 else 0
        print(
            f"{i:<5} {r.config.mega_conc:>4} {r.config.shard_conc:>5} "
            f"{r.config.chunk_mib:>5}M {'Y' if r.ok else 'N':>3} "
            f"{r.seconds:>8.1f} {mib_s:>6.2f} {r.eagain:>6} {r.retries:>6} "
            f"{r.timeouts:>4} {r.hiccups:>5}",
            flush=True,
        )

    winners = [r for r in ranked if r.ok]
    if not winners:
        print("\nNo successful trials.", flush=True)
        return 1

    best = winners[0]
    stable = [r for r in winners if r.hiccups <= winners[0].hiccups + max(2, winners[0].hiccups // 5)]
    print("\n=== Recommendation ===", flush=True)
    print(
        f"Best overall: MEGA_ACCOUNT_UPLOAD_CONCURRENCY={best.config.mega_conc} "
        f"SHARD_UPLOAD_CONCURRENCY={best.config.shard_conc} "
        f"chunk≈{best.config.chunk_mib} MiB "
        f"({best.seconds:.1f}s, hiccups={best.hiccups})",
        flush=True,
    )
    if len(stable) > 1:
        print("Near-ties:", flush=True)
        for r in stable[:5]:
            print(f"  - {r.config.label()}  {r.seconds:.1f}s  hiccups={r.hiccups}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
