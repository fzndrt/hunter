"""Layer 5 — Persistent SQLite WAL Exit Guard & AMM Reserve-Normalized LP Monitor."""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import time
import uuid
from typing import Optional

from .config import is_valid_solana_mint
from .models import ExitSignalCode, PairSnapshot, TrackedPosition


def compute_real_lp_retention_pct(
    entry_liq_usd: float, live_liq_usd: float, entry_mc: float, live_mc: float
) -> Optional[float]:
    """
    Normalisasi likuiditas AMM x*y=k terhadap pergerakan harga:
        Expected_LP = Entry_LP * sqrt(Live_MC / Entry_MC)
        Real_LP_Retention = (Live_LP / Expected_LP) * 100%

    Jika dev TIDAK menarik LP, rasio tetap ~100% meski harga naik/turun tajam.
    Jika rasio jatuh < 80%, terjadi penarikan LP nyata (rugpull / LP drain).
    """
    if entry_liq_usd <= 0 or live_liq_usd < 0 or entry_mc <= 0 or live_mc <= 0:
        return None
    expected_lp = entry_liq_usd * math.sqrt(live_mc / entry_mc)
    if expected_lp <= 0:
        return None
    return round((live_liq_usd / expected_lp) * 100.0, 1)


def evaluate_position_exit(
    pos: TrackedPosition, live_pair: PairSnapshot
) -> tuple[ExitSignalCode, str, float, Optional[float], bool, bool]:
    """Evaluasi murni status keluar posisi aktif sesuai hierarki prioritas modal."""
    live_mc = max(0.0, float(live_pair.market_cap))
    live_liq = max(0.0, float(live_pair.liquidity_usd))
    pnl_pct = ((live_mc - pos.entry_mc) / pos.entry_mc * 100.0) if pos.entry_mc > 0 else 0.0
    peak_mc = max(pos.peak_mc, live_mc)
    peak_pnl_pct = ((peak_mc - pos.entry_mc) / pos.entry_mc * 100.0) if pos.entry_mc > 0 else 0.0
    drawdown_from_peak_pct = (
        ((peak_mc - live_mc) / peak_mc * 100.0) if peak_mc > 0 else 0.0
    )

    real_lp_ret = compute_real_lp_retention_pct(
        pos.entry_liq_usd, live_liq, pos.entry_mc, live_mc
    )

    tp1_hit = pos.tp1_hit or (pnl_pct >= pos.tp1_pct)
    tp2_hit = pos.tp2_hit or (pnl_pct >= pos.tp2_pct)

    # Prioritas 1: Darurat Penarikan LP Riil (< 80% setelah normalisasi √(MC/MC0))
    if real_lp_ret is not None and real_lp_ret < 80.0:
        return (
            "LP_PULL_EMERGENCY",
            f"DARURAT: LP riil menyusut ke {real_lp_ret:.1f}% (ternormalisasi AMM) — JUAL SEGERA.",
            round(pnl_pct, 2),
            real_lp_ret,
            tp1_hit,
            tp2_hit,
        )

    # Prioritas 2: Unconditional Hard Stop-Loss (atau Breakeven Stop setelah TP1 tercapai)
    effective_stop_pct = 0.0 if pos.tp1_hit else -abs(pos.hard_stop_pct)
    if pnl_pct <= effective_stop_pct:
        if pos.tp1_hit:
            return (
                "HARD_STOP_LOSS",
                f"Breakeven Stop tersentuh ({pnl_pct:+.1f}%) pasca-TP1 — tutup sisa posisi lindungi profit.",
                round(pnl_pct, 2),
                real_lp_ret,
                tp1_hit,
                tp2_hit,
            )
        return (
            "HARD_STOP_LOSS",
            f"HARD STOP-LOSS tersentuh ({pnl_pct:+.1f}% <= -{pos.hard_stop_pct:.0f}%) — wajib cut loss disiplin.",
            round(pnl_pct, 2),
            real_lp_ret,
            tp1_hit,
            tp2_hit,
        )

    # Prioritas 3: Dynamic Trailing Stop dari Peak MC saat posisi sudah profit >= +30%
    if peak_pnl_pct >= 30.0:
        trail_limit = 12.0 if peak_pnl_pct >= 150.0 else (14.0 if peak_pnl_pct >= 80.0 else 18.0)
        if drawdown_from_peak_pct >= trail_limit:
            return (
                "TRAILING_PEAK_EXIT",
                f"Trailing Stop aktif: turun -{drawdown_from_peak_pct:.1f}% dari puncak ${peak_mc:,.0f} (batas -{trail_limit:.0f}%).",
                round(pnl_pct, 2),
                real_lp_ret,
                tp1_hit,
                tp2_hit,
            )

    # Prioritas 4: Whale Dump Divergence (5m)
    if live_pair.sells_m5 >= 8 and live_pair.sells_m5 > live_pair.buys_m5 * 1.8 and live_pair.price_change_m5 <= -12.0:
        return (
            "WHALE_DUMP_DIVERGENCE",
            f"Divergensi jual berat 5m ({live_pair.sells_m5}S vs {live_pair.buys_m5}B, {live_pair.price_change_m5:+.1f}%) — pertimbangkan kunci profit.",
            round(pnl_pct, 2),
            real_lp_ret,
            tp1_hit,
            tp2_hit,
        )

    # Prioritas 5: Take Profit Ladder (TP2 & TP1)
    if pnl_pct >= pos.tp2_pct and not pos.tp2_hit:
        return (
            "TP2_TAKE_PROFIT",
            f"TP2 TERCAPAI ({pnl_pct:+.1f}% >= +{pos.tp2_pct:.0f}%) — Jual 30%, sisakan 20% moonbag dengan trailing stop.",
            round(pnl_pct, 2),
            real_lp_ret,
            tp1_hit,
            True,
        )
    if pnl_pct >= pos.tp1_pct and not pos.tp1_hit:
        return (
            "TP1_DERISK_BREAKEVEN",
            f"TP1 TERCAPAI ({pnl_pct:+.1f}% >= +{pos.tp1_pct:.0f}%) — Jual 50% untuk amankan modal & geser SL ke Breakeven.",
            round(pnl_pct, 2),
            real_lp_ret,
            True,
            tp2_hit,
        )

    return (
        "HOLDING_SAFE",
        f"Posisi aman ({pnl_pct:+.1f}%). Peak ${peak_mc:,.0f} · LP Retention {real_lp_ret if real_lp_ret is not None else '—'}%.",
        round(pnl_pct, 2),
        real_lp_ret,
        tp1_hit,
        tp2_hit,
    )


class ExitGuardStore:
    """Persistensi SQLite WAL + asyncio.Lock — anti amnesia cold-start & anti race condition."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    mint TEXT NOT NULL,
                    pair_address TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entry_mc REAL NOT NULL,
                    entry_price_usd REAL NOT NULL DEFAULT 0,
                    entry_liq_usd REAL NOT NULL DEFAULT 0,
                    position_usd REAL NOT NULL DEFAULT 100,
                    hard_stop_pct REAL NOT NULL DEFAULT 15,
                    tp1_pct REAL NOT NULL DEFAULT 45,
                    tp2_pct REAL NOT NULL DEFAULT 100,
                    peak_mc REAL NOT NULL,
                    tp1_hit INTEGER NOT NULL DEFAULT 0,
                    tp2_hit INTEGER NOT NULL DEFAULT 0,
                    live_mc REAL NOT NULL DEFAULT 0,
                    live_liq_usd REAL NOT NULL DEFAULT 0,
                    pnl_pct REAL NOT NULL DEFAULT 0,
                    real_lp_retention_pct REAL,
                    signal TEXT NOT NULL DEFAULT 'HOLDING_SAFE',
                    signal_detail TEXT NOT NULL DEFAULT '',
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    notes TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS sent_alerts (
                    mint TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    sent_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (mint, alert_type)
                );

                CREATE TABLE IF NOT EXISTS smart_wallets (
                    wallet TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    win_rate_pct REAL NOT NULL DEFAULT 0,
                    added_at_ms INTEGER NOT NULL
                );
                """
            )

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> TrackedPosition:
        return TrackedPosition(
            id=row["id"],
            mint=row["mint"],
            pair_address=row["pair_address"],
            symbol=row["symbol"],
            name=row["name"],
            entry_mc=float(row["entry_mc"]),
            entry_price_usd=float(row["entry_price_usd"]),
            entry_liq_usd=float(row["entry_liq_usd"]),
            position_usd=float(row["position_usd"]),
            hard_stop_pct=float(row["hard_stop_pct"]),
            tp1_pct=float(row["tp1_pct"]),
            tp2_pct=float(row["tp2_pct"]),
            peak_mc=float(row["peak_mc"]),
            tp1_hit=bool(row["tp1_hit"]),
            tp2_hit=bool(row["tp2_hit"]),
            live_mc=float(row["live_mc"]),
            live_liq_usd=float(row["live_liq_usd"]),
            pnl_pct=float(row["pnl_pct"]),
            real_lp_retention_pct=(
                float(row["real_lp_retention_pct"])
                if row["real_lp_retention_pct"] is not None
                else None
            ),
            signal=row["signal"],
            signal_detail=row["signal_detail"],
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
            notes=row["notes"],
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
            signal_detail="Posisi tercatat. Menunggu pemantauan siklus berikutnya.",
            created_at_ms=now,
            updated_at_ms=now,
            notes=notes[:200],
        )

        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO positions (
                        id, mint, pair_address, symbol, name, entry_mc, entry_price_usd,
                        entry_liq_usd, position_usd, hard_stop_pct, tp1_pct, tp2_pct,
                        peak_mc, tp1_hit, tp2_hit, live_mc, live_liq_usd, pnl_pct,
                        real_lp_retention_pct, signal, signal_detail, created_at_ms, updated_at_ms, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pos.id, pos.mint, pos.pair_address, pos.symbol, pos.name,
                        pos.entry_mc, pos.entry_price_usd, pos.entry_liq_usd, pos.position_usd,
                        pos.hard_stop_pct, pos.tp1_pct, pos.tp2_pct, pos.peak_mc,
                        int(pos.tp1_hit), int(pos.tp2_hit), pos.live_mc, pos.live_liq_usd,
                        pos.pnl_pct, pos.real_lp_retention_pct, pos.signal, pos.signal_detail,
                        pos.created_at_ms, pos.updated_at_ms, pos.notes,
                    ),
                )
        return pos

    async def list_positions(self) -> list[TrackedPosition]:
        async with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM positions ORDER BY created_at_ms DESC"
                ).fetchall()
                return [self._row_to_position(r) for r in rows]

    async def remove_position_by_mint_or_id(self, identifier: str) -> bool:
        clean = identifier.strip()
        async with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM positions WHERE id = ? OR mint = ?", (clean, clean)
                )
                return cur.rowcount > 0

    async def update_position_state(
        self, pos: TrackedPosition, live_pair: PairSnapshot
    ) -> TrackedPosition:
        signal, detail, pnl_pct, real_lp_ret, tp1_hit, tp2_hit = evaluate_position_exit(
            pos, live_pair
        )
        now = int(time.time() * 1000)
        pos.live_mc = live_pair.market_cap
        pos.live_liq_usd = live_pair.liquidity_usd
        pos.peak_mc = max(pos.peak_mc, live_pair.market_cap)
        pos.pnl_pct = pnl_pct
        pos.real_lp_retention_pct = real_lp_ret
        pos.tp1_hit = tp1_hit
        pos.tp2_hit = tp2_hit
        pos.signal = signal
        pos.signal_detail = detail
        pos.updated_at_ms = now

        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE positions
                    SET peak_mc = ?, tp1_hit = ?, tp2_hit = ?, live_mc = ?, live_liq_usd = ?,
                        pnl_pct = ?, real_lp_retention_pct = ?, signal = ?, signal_detail = ?,
                        updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (
                        pos.peak_mc, int(pos.tp1_hit), int(pos.tp2_hit), pos.live_mc,
                        pos.live_liq_usd, pos.pnl_pct, pos.real_lp_retention_pct,
                        pos.signal, pos.signal_detail, pos.updated_at_ms, pos.id,
                    ),
                )
        return pos

    async def should_send_alert(
        self, mint: str, alert_type: str, cooldown_minutes: int
    ) -> bool:
        now = int(time.time() * 1000)
        cutoff = now - cooldown_minutes * 60_000
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT sent_at_ms FROM sent_alerts WHERE mint = ? AND alert_type = ?",
                    (mint, alert_type),
                ).fetchone()
                if row and int(row["sent_at_ms"]) > cutoff:
                    return False
                conn.execute(
                    """
                    INSERT INTO sent_alerts (mint, alert_type, sent_at_ms)
                    VALUES (?, ?, ?)
                    ON CONFLICT(mint, alert_type) DO UPDATE SET sent_at_ms = excluded.sent_at_ms
                    """,
                    (mint, alert_type, now),
                )
                return True
