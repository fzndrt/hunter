"""Abstraksi Persistensi Cloud (Upstash Redis / Vercel KV) & Lokal (SQLite WAL)."""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import uuid
from dataclasses import asdict
from typing import Any, Optional, Protocol
from urllib.parse import urlparse
import httpx

from .config import BotConfig, is_valid_solana_mint
from .exit_guard import ExitGuardStore, evaluate_position_exit
from .models import PairSnapshot, TrackedPosition

logger = logging.getLogger(__name__)

POSITIONS_HASH_KEY = "corridor_desk:v2:positions"
ALERT_PREFIX = "corridor_desk:v2:alert:"


class PositionStore(Protocol):
    mode: str

    async def add_position(
        self,
        *,
        mint: str,
        symbol: str,
        name: str,
        entry_mc: float,
        entry_liq_usd: float,
        position_usd: float,
        entry_price_usd: float = 0.0,
        pair_address: str = "",
        hard_stop_pct: float = 15.0,
        tp1_pct: float = 45.0,
        tp2_pct: float = 100.0,
        notes: str = "",
    ) -> TrackedPosition: ...

    async def list_positions(self) -> list[TrackedPosition]: ...

    async def remove_position_by_mint_or_id(self, identifier: str) -> bool: ...

    async def update_position_state(
        self, pos: TrackedPosition, live_pair: PairSnapshot
    ) -> TrackedPosition: ...

    async def should_send_alert(
        self, mint: str, alert_type: str, cooldown_minutes: int
    ) -> bool: ...


def validate_upstash_rest_url(raw_url: str) -> Optional[str]:
    """Pastikan URL REST Upstash/Vercel KV memakai HTTPS dan bukan alamat internal/loopback."""
    cleaned = raw_url.strip().rstrip("/")
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        return None
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return None
    except ValueError:
        # Hostname domain publik (mis. *.upstash.io)
        if "." not in host:
            return None
    return cleaned


class UpstashExitGuardStore:
    """
    Persistensi serverless 100% persisten via Upstash Redis / Vercel KV REST API.
    Tidak butuh paket pip tambahan (memakai httpx) dan tahan 100% Vercel cold start.
    """

    mode = "upstash_redis"

    def __init__(self, rest_url: str, rest_token: str) -> None:
        validated = validate_upstash_rest_url(rest_url)
        if not validated or not rest_token.strip():
            raise ValueError("Konfigurasi UPSTASH_REDIS_REST_URL / TOKEN tidak valid.")
        self.rest_url = validated
        self.rest_token = rest_token.strip()

    async def _cmd(self, *args: Any) -> Any:
        headers = {
            "Authorization": f"Bearer {self.rest_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=False) as client:
            resp = await client.post(
                self.rest_url,
                headers=headers,
                json=[str(a) for a in args],
            )
            if resp.status_code != 200:
                logger.warning("Upstash REST error HTTP %s", resp.status_code)
                return None
            data = resp.json()
            return data.get("result")

    @staticmethod
    def _dict_to_position(raw: dict[str, Any]) -> TrackedPosition:
        return TrackedPosition(
            id=str(raw.get("id") or ""),
            mint=str(raw.get("mint") or ""),
            pair_address=str(raw.get("pair_address") or ""),
            symbol=str(raw.get("symbol") or "TOKEN"),
            name=str(raw.get("name") or "Solana Token"),
            entry_mc=float(raw.get("entry_mc") or 0.0),
            entry_price_usd=float(raw.get("entry_price_usd") or 0.0),
            entry_liq_usd=float(raw.get("entry_liq_usd") or 0.0),
            position_usd=float(raw.get("position_usd") or 100.0),
            hard_stop_pct=float(raw.get("hard_stop_pct") or 15.0),
            tp1_pct=float(raw.get("tp1_pct") or 45.0),
            tp2_pct=float(raw.get("tp2_pct") or 100.0),
            peak_mc=float(raw.get("peak_mc") or 0.0),
            tp1_hit=bool(raw.get("tp1_hit", False)),
            tp2_hit=bool(raw.get("tp2_hit", False)),
            live_mc=float(raw.get("live_mc") or 0.0),
            live_liq_usd=float(raw.get("live_liq_usd") or 0.0),
            pnl_pct=float(raw.get("pnl_pct") or 0.0),
            real_lp_retention_pct=(
                float(raw["real_lp_retention_pct"])
                if raw.get("real_lp_retention_pct") is not None
                else None
            ),
            signal=raw.get("signal") or "HOLDING_SAFE",
            signal_detail=str(raw.get("signal_detail") or ""),
            created_at_ms=int(raw.get("created_at_ms") or 0),
            updated_at_ms=int(raw.get("updated_at_ms") or 0),
            notes=str(raw.get("notes") or ""),
        )

    async def add_position(
        self,
        *,
        mint: str,
        symbol: str,
        name: str,
        entry_mc: float,
        entry_liq_usd: float,
        position_usd: float,
        entry_price_usd: float = 0.0,
        pair_address: str = "",
        hard_stop_pct: float = 15.0,
        tp1_pct: float = 45.0,
        tp2_pct: float = 100.0,
        notes: str = "",
    ) -> TrackedPosition:
        clean_mint = mint.strip()
        if not is_valid_solana_mint(clean_mint):
            raise ValueError("Alamat mint Solana tidak valid (wajib Base58 32–44 karakter).")
        if entry_mc <= 0:
            raise ValueError("entry_mc harus bernilai positif.")

        now = int(time.time() * 1000)
        pos = TrackedPosition(
            id=uuid.uuid4().hex[:12],
            mint=clean_mint,
            pair_address=pair_address,
            symbol=symbol.strip().upper()[:16] or "TOKEN",
            name=name.strip()[:48] or "Solana Token",
            entry_mc=entry_mc,
            entry_price_usd=max(0.0, entry_price_usd),
            entry_liq_usd=max(0.0, entry_liq_usd),
            position_usd=max(1.0, position_usd),
            hard_stop_pct=max(5.0, min(40.0, hard_stop_pct)),
            tp1_pct=max(15.0, tp1_pct),
            tp2_pct=max(30.0, tp2_pct),
            peak_mc=entry_mc,
            live_mc=entry_mc,
            live_liq_usd=max(0.0, entry_liq_usd),
            pnl_pct=0.0,
            real_lp_retention_pct=100.0 if entry_liq_usd > 0 else None,
            signal="HOLDING_SAFE",
            signal_detail="Posisi tercatat di Upstash Cloud Exit Guard.",
            created_at_ms=now,
            updated_at_ms=now,
            notes=notes[:200],
        )
        await self._cmd("HSET", POSITIONS_HASH_KEY, pos.id, json.dumps(asdict(pos)))
        return pos

    async def list_positions(self) -> list[TrackedPosition]:
        res = await self._cmd("HGETALL", POSITIONS_HASH_KEY)
        if not isinstance(res, list):
            return []
        positions: list[TrackedPosition] = []
        # Redis HGETALL via REST mengembalikan [field1, val1, field2, val2, ...]
        for i in range(1, len(res), 2):
            try:
                raw = json.loads(res[i])
                if isinstance(raw, dict) and raw.get("id"):
                    positions.append(self._dict_to_position(raw))
            except Exception:
                continue
        positions.sort(key=lambda p: p.created_at_ms, reverse=True)
        return positions

    async def remove_position_by_mint_or_id(self, identifier: str) -> bool:
        clean = identifier.strip()
        if not clean:
            return False
        positions = await self.list_positions()
        removed = False
        for pos in positions:
            if pos.id == clean or pos.mint == clean:
                await self._cmd("HDEL", POSITIONS_HASH_KEY, pos.id)
                removed = True
        return removed

    async def update_position_state(
        self, pos: TrackedPosition, live_pair: PairSnapshot
    ) -> TrackedPosition:
        signal, detail, pnl_pct, real_lp_ret, tp1_hit, tp2_hit = evaluate_position_exit(
            pos, live_pair
        )
        pos.live_mc = live_pair.market_cap
        pos.live_liq_usd = live_pair.liquidity_usd
        pos.peak_mc = max(pos.peak_mc, live_pair.market_cap)
        pos.pnl_pct = pnl_pct
        pos.real_lp_retention_pct = real_lp_ret
        pos.tp1_hit = tp1_hit
        pos.tp2_hit = tp2_hit
        pos.signal = signal
        pos.signal_detail = detail
        pos.updated_at_ms = int(time.time() * 1000)
        await self._cmd("HSET", POSITIONS_HASH_KEY, pos.id, json.dumps(asdict(pos)))
        return pos

    async def should_send_alert(
        self, mint: str, alert_type: str, cooldown_minutes: int
    ) -> bool:
        ttl_sec = max(60, int(cooldown_minutes) * 60)
        key = f"{ALERT_PREFIX}{mint.strip()}:{alert_type.strip()}"
        # Atomik SET NX EX: mengembalikan "OK" hanya jika key belum ada (bebas duplikat antar invocation Vercel)
        res = await self._cmd("SET", key, str(int(time.time())), "EX", str(ttl_sec), "NX")
        return res == "OK"


class SqliteStoreWrapper(ExitGuardStore):
    """Pembungkus SQLite lokal dengan label mode eksplisit untuk status Vercel/VPS."""

    def __init__(self, db_path: str, is_vercel: bool = False) -> None:
        super().__init__(db_path)
        self.mode = "sqlite_tmp_ephemeral" if is_vercel else "sqlite_wal_persistent"


def create_store(cfg: BotConfig) -> PositionStore:
    """
    Pilih backend persistensi otomatis:
    - Jika UPSTASH_REDIS_REST_URL + TOKEN tersedia -> Upstash Cloud Redis (rekomendasi Vercel).
    - Jika tidak -> SQLite WAL (otomatis di /tmp pada Vercel atau ./data pada VPS).
    """
    if cfg.upstash_redis_rest_url and cfg.upstash_redis_rest_token:
        validated = validate_upstash_rest_url(cfg.upstash_redis_rest_url)
        if validated:
            return UpstashExitGuardStore(validated, cfg.upstash_redis_rest_token)
        logger.warning("UPSTASH_REDIS_REST_URL tidak valid; fallback ke SQLite.")
    return SqliteStoreWrapper(cfg.db_path, is_vercel=cfg.is_vercel)
