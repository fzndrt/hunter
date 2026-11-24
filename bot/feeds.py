"""6-Feed Async Solana Discovery (GeckoTerminal + DexScreener PumpSwap/Raydium/Profiles/Boosts)."""

from __future__ import annotations

import asyncio
from typing import Any, Optional
import httpx

from .config import SOL_MINT, VERIFIED_HARD_QUOTE_MINTS, is_valid_solana_mint
from .models import PairSnapshot


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        num = float(val)
        return num if num == num else default
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def parse_dexscreener_pair(
    raw: dict[str, Any], feed_sources: tuple[str, ...] = ()
) -> Optional[PairSnapshot]:
    if str(raw.get("chainId") or "").lower() != "solana":
        return None
    base = raw.get("baseToken") or {}
    quote = raw.get("quoteToken") or {}
    mint = str(base.get("address") or "").strip()
    quote_mint = str(quote.get("address") or "").strip()
    if not is_valid_solana_mint(mint) or mint == SOL_MINT:
        return None

    liq = raw.get("liquidity") or {}
    vol = raw.get("volume") or {}
    txns = raw.get("txns") or {}
    pc = raw.get("priceChange") or {}

    m5_tx = txns.get("m5") or {}
    h1_tx = txns.get("h1") or {}
    h24_tx = txns.get("h24") or {}

    mc = _safe_float(raw.get("marketCap")) or _safe_float(raw.get("fdv"))
    return PairSnapshot(
        mint=mint,
        pair_address=str(raw.get("pairAddress") or ""),
        dex_id=str(raw.get("dexId") or "solana"),
        symbol=str(base.get("symbol") or "TOKEN")[:20],
        name=str(base.get("name") or base.get("symbol") or "Solana Token")[:48],
        quote_mint=quote_mint,
        quote_symbol=str(quote.get("symbol") or "UNKNOWN")[:12],
        price_usd=_safe_float(raw.get("priceUsd")),
        market_cap=mc,
        liquidity_usd=_safe_float(liq.get("usd")),
        vol_m5=_safe_float(vol.get("m5")),
        vol_h1=_safe_float(vol.get("h1")),
        vol_h24=_safe_float(vol.get("h24")),
        buys_m5=_safe_int(m5_tx.get("buys")),
        sells_m5=_safe_int(m5_tx.get("sells")),
        buys_h1=_safe_int(h1_tx.get("buys")),
        sells_h1=_safe_int(h1_tx.get("sells")),
        buys_h24=_safe_int(h24_tx.get("buys")),
        sells_h24=_safe_int(h24_tx.get("sells")),
        price_change_m5=_safe_float(pc.get("m5")),
        price_change_h1=_safe_float(pc.get("h1")),
        price_change_h24=_safe_float(pc.get("h24")),
        pair_created_at_ms=_safe_int(raw.get("pairCreatedAt")),
        feed_sources=feed_sources,
        url=str(raw.get("url") or f"https://dexscreener.com/solana/{mint}"),
        vol_h6=_safe_float(vol.get("h6")),
        price_change_h6=_safe_float(pc.get("h6")),
    )


def pick_best_solana_pair(
    pairs: list[dict[str, Any]], mint: str, feed_sources: tuple[str, ...] = ()
) -> Optional[PairSnapshot]:
    candidates: list[PairSnapshot] = []
    for raw in pairs:
        snap = parse_dexscreener_pair(raw, feed_sources=feed_sources)
        if snap and snap.mint == mint:
            candidates.append(snap)
    if not candidates:
        return None
    # Prioritaskan pair dengan hard-quote terverifikasi lalu likuiditas terbesar
    candidates.sort(
        key=lambda p: (
            1 if p.quote_mint in VERIFIED_HARD_QUOTE_MINTS else 0,
            p.liquidity_usd,
        ),
        reverse=True,
    )
    return candidates[0]


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    try:
        resp = await client.get(url, timeout=8.0)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


async def fetch_live_sol_price_usd(
    client: httpx.AsyncClient, fallback_usd: float = 160.0
) -> float:
    """Ambil harga SOL/USD dinamis (mengganti harga hardcoded di versi lama)."""
    data = await _get_json(
        client, f"https://api.dexscreener.com/tokens/v1/solana/{SOL_MINT}"
    )
    if isinstance(data, list):
        for pair in data:
            q = (pair.get("quoteToken") or {}).get("symbol", "")
            if q in {"USDC", "USDT"}:
                px = _safe_float(pair.get("priceUsd"))
                if 10.0 <= px <= 5000.0:
                    return px
    return fallback_usd


async def fetch_pair_for_mint(
    client: httpx.AsyncClient, mint: str, feed_sources: tuple[str, ...] = ("manual",)
) -> Optional[PairSnapshot]:
    if not is_valid_solana_mint(mint):
        return None
    data = await _get_json(
        client, f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
    )
    if not isinstance(data, list):
        return None
    return pick_best_solana_pair(data, mint, feed_sources=feed_sources)


async def fetch_pairs_batch(
    client: httpx.AsyncClient,
    mints: list[str],
    sources_map: dict[str, set[str]],
) -> dict[str, PairSnapshot]:
    results: dict[str, PairSnapshot] = {}
    clean_mints = [m for m in mints if is_valid_solana_mint(m) and m != SOL_MINT]

    for i in range(0, len(clean_mints), 25):
        chunk = clean_mints[i : i + 25]
        joined = ",".join(chunk)
        data = await _get_json(
            client, f"https://api.dexscreener.com/tokens/v1/solana/{joined}"
        )
        if not isinstance(data, list):
            continue
        grouped: dict[str, list[dict[str, Any]]] = {}
        for raw in data:
            addr = str((raw.get("baseToken") or {}).get("address") or "").strip()
            if addr:
                grouped.setdefault(addr, []).append(raw)
        for m in chunk:
            best = pick_best_solana_pair(
                grouped.get(m, []),
                m,
                feed_sources=tuple(sorted(sources_map.get(m, set()))),
            )
            if best:
                results[m] = best
    return results


async def discover_solana_candidates(
    client: httpx.AsyncClient, max_candidates: int = 50
) -> tuple[list[PairSnapshot], list[str]]:
    """Kumpulkan kandidat dari 8 feed paralel (termasuk koin tua Sleeper Revival 1h & Top Boosts)."""
    urls = {
        "gecko_trending_5m": "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?duration=5m",
        "gecko_trending_1h": "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?duration=1h",
        "gecko_new_pools": "https://api.geckoterminal.com/api/v2/networks/solana/new_pools",
        "dex_pumpswap": "https://api.dexscreener.com/latest/dex/search?q=pumpswap",
        "dex_raydium": "https://api.dexscreener.com/latest/dex/search?q=raydium",
        "dex_profiles": "https://api.dexscreener.com/token-profiles/latest/v1",
        "dex_boosts": "https://api.dexscreener.com/token-boosts/latest/v1",
        "dex_boosts_top": "https://api.dexscreener.com/token-boosts/top/v1",
    }

    keys = list(urls.keys())
    responses = await asyncio.gather(*(_get_json(client, urls[k]) for k in keys))
    sources_map: dict[str, set[str]] = {}
    notes: list[str] = []

    def add_mint(mint: str, source: str) -> None:
        m = mint.strip()
        if is_valid_solana_mint(m) and m not in VERIFIED_HARD_QUOTE_MINTS:
            sources_map.setdefault(m, set()).add(source)

    for key, payload in zip(keys, responses):
        count_before = sum(1 for s in sources_map.values() if key in s)
        if key.startswith("gecko_") and isinstance(payload, dict):
            for item in payload.get("data") or []:
                rel = (item.get("relationships") or {}).get("base_token") or {}
                tid = str((rel.get("data") or {}).get("id") or "")
                if tid.startswith("solana_"):
                    add_mint(tid.replace("solana_", "", 1), key)
        elif key in {"dex_pumpswap", "dex_raydium"} and isinstance(payload, dict):
            for p in payload.get("pairs") or []:
                if str(p.get("chainId") or "").lower() == "solana":
                    add_mint(str((p.get("baseToken") or {}).get("address") or ""), key)
        elif key in {"dex_profiles", "dex_boosts", "dex_boosts_top"} and isinstance(payload, list):
            for item in payload[:30]:
                if str(item.get("chainId") or "").lower() == "solana":
                    add_mint(str(item.get("tokenAddress") or ""), key)
        count_after = sum(1 for s in sources_map.values() if key in s)
        notes.append(f"{key}:{count_after - count_before}")

    # Prioritaskan token yang muncul di lebih dari satu feed
    ranked_mints = sorted(
        sources_map.keys(), key=lambda m: len(sources_map[m]), reverse=True
    )[:max_candidates]

    batch = await fetch_pairs_batch(client, ranked_mints, sources_map)
    pairs = list(batch.values())
    pairs.sort(
        key=lambda p: (len(p.feed_sources), p.vol_m5, p.liquidity_usd),
        reverse=True,
    )
    return pairs, notes
