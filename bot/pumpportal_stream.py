"""PumpPortal WebSocket Stream — Perbaikan subscribe keys, rumus (vSol-30)/85, & filter protokol."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Iterable, Optional

import websockets

from .config import KNOWN_PROTOCOL_AUTHORITIES, is_valid_solana_mint

logger = logging.getLogger(__name__)

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
PUMP_VIRTUAL_SOL_OFFSET = 30.0
PUMP_GRADUATION_SOL_DELTA = 85.0


def compute_pumpfun_bonding_progress_pct(v_sol_in_bonding_curve: float) -> float:
    """
    Rumus progres bonding curve Pump.fun yang BENAR:
      Pump.fun dimulai dari 30 SOL virtual dan migrasi setelah +85 SOL riil (~115 SOL total).
      Progress = clamp(((vSol - 30) / 85) * 100, 0, 100)
    """
    raw = ((float(v_sol_in_bonding_curve) - PUMP_VIRTUAL_SOL_OFFSET) / PUMP_GRADUATION_SOL_DELTA) * 100.0
    return round(max(0.0, min(100.0, raw)), 2)


def build_subscribe_token_trade_payload(mints: Iterable[str]) -> dict[str, Any]:
    """Pastikan subscribeTokenTrade SELALU menyertakan array 'keys' valid."""
    valid_keys = [m.strip() for m in mints if is_valid_solana_mint(m)]
    return {"method": "subscribeTokenTrade", "keys": valid_keys}


def parse_pumpportal_trade_event(
    payload: dict[str, Any], sol_price_usd: float, min_sol_trade: float = 3.0
) -> Optional[dict[str, Any]]:
    mint = str(payload.get("mint") or "").strip()
    trader = str(payload.get("traderPublicKey") or "").strip()
    if not is_valid_solana_mint(mint):
        return None
    # Abaikan alamat program / otoritas pool DEX agar tidak salah dilabeli sebagai Whale
    if trader in KNOWN_PROTOCOL_AUTHORITIES:
        return None

    sol_amount = abs(float(payload.get("solAmount") or 0.0))
    if sol_amount < min_sol_trade:
        return None

    v_sol = float(payload.get("vSolInBondingCurve") or 0.0)
    progress_pct = compute_pumpfun_bonding_progress_pct(v_sol) if v_sol > 0 else None
    mc_sol = float(payload.get("marketCapSol") or 0.0)
    mc_usd = round(mc_sol * sol_price_usd, 2) if mc_sol > 0 else 0.0

    return {
        "mint": mint,
        "tx_type": str(payload.get("txType") or "").lower(),
        "trader": trader,
        "sol_amount": round(sol_amount, 3),
        "usd_amount": round(sol_amount * sol_price_usd, 2),
        "market_cap_usd": mc_usd,
        "bonding_progress_pct": progress_pct,
    }


async def stream_token_trades(
    mints_provider: Callable[[], list[str]],
    sol_price_provider: Callable[[], float],
    on_large_trade: Callable[[dict[str, Any]], Awaitable[None]],
    min_sol_trade: float = 3.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Loop WebSocket PumpPortal dengan reconnect backoff dan subscribe keys yang benar."""
    backoff = 2.0
    while not (stop_event and stop_event.is_set()):
        try:
            async with websockets.connect(
                PUMPPORTAL_WS_URL, ping_interval=20, ping_timeout=20
            ) as ws:
                backoff = 2.0
                keys = [m for m in mints_provider() if is_valid_solana_mint(m)]
                if keys:
                    await ws.send(json.dumps(build_subscribe_token_trade_payload(keys)))
                    logger.info("PumpPortal subscribed to %d token keys", len(keys))
                else:
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    logger.info("PumpPortal subscribed to migrations (no active mint keys)")

                async for raw_msg in ws:
                    if stop_event and stop_event.is_set():
                        break
                    try:
                        data = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    event = parse_pumpportal_trade_event(
                        data,
                        sol_price_usd=sol_price_provider(),
                        min_sol_trade=min_sol_trade,
                    )
                    if event:
                        await on_large_trade(event)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("PumpPortal WS terputus (%s), reconnect %.0fs...", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 1.8)
