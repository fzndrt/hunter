"""Layer 3 — Jupiter Round-Trip 0.1 SOL & 1.0 SOL Depth Cliff Verification (100% Fail-Closed)."""

from __future__ import annotations

import asyncio
from typing import Any, Optional
import httpx

from .config import BotConfig, SOL_MINT, is_valid_solana_mint
from .models import ExecutionResult, GateCheck

LAMPORTS_01_SOL = 100_000_000       # 0.1 SOL
LAMPORTS_1_SOL = 1_000_000_000      # 1.0 SOL


async def _fetch_jup_quote(
    client: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    amount_atomic: int,
    api_key: str = "",
) -> Optional[dict[str, Any]]:
    base_url = (
        "https://api.jup.ag/swap/v1/quote"
        if api_key
        else "https://lite-api.jup.ag/swap/v1/quote"
    )
    headers = {"x-api-key": api_key} if api_key else {}
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_atomic),
        "slippageBps": "150",
    }
    try:
        resp = await client.get(base_url, params=params, headers=headers, timeout=8.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data.get("outAmount"):
            return None
        return data
    except Exception:
        return None


def _parse_impact_pct(quote: Optional[dict[str, Any]]) -> Optional[float]:
    if not quote:
        return None
    raw = quote.get("priceImpactPct")
    if raw is None:
        return None
    try:
        # Jupiter mengembalikan priceImpactPct sebagai fraksi desimal (0.015 = 1.5%)
        return round(abs(float(raw)) * 100.0, 2)
    except (TypeError, ValueError):
        return None


async def verify_jupiter_execution(
    client: httpx.AsyncClient, mint: str, cfg: BotConfig
) -> ExecutionResult:
    if not is_valid_solana_mint(mint) or mint == SOL_MINT:
        return ExecutionResult(
            passed=False,
            status="INVALID_MINT",
            checks=(
                GateCheck(
                    id="jup_valid_mint",
                    layer="EXECUTION",
                    label="Validasi Rute Jupiter",
                    passed=False,
                    severity="VETO",
                    actual="INVALID",
                    threshold="Mint SPL valid non-SOL",
                    detail="Alamat mint tidak valid untuk simulasi swap Jupiter.",
                ),
            ),
            round_trip_loss_01_sol_pct=None,
            depth_loss_1_sol_pct=None,
            buy_impact_01_sol_pct=None,
            sell_impact_01_sol_pct=None,
        )

    # Jalankan buy 0.1 SOL dan buy 1.0 SOL secara paralel
    buy_01, buy_1 = await asyncio.gather(
        _fetch_jup_quote(client, SOL_MINT, mint, LAMPORTS_01_SOL, cfg.jupiter_api_key),
        _fetch_jup_quote(client, SOL_MINT, mint, LAMPORTS_1_SOL, cfg.jupiter_api_key),
    )

    if not buy_01:
        return ExecutionResult(
            passed=False,
            status="BUY_01_FAILED",
            checks=(
                GateCheck(
                    id="jup_roundtrip_01",
                    layer="EXECUTION",
                    label="Simulasi Pulang-Pergi 0.1 SOL",
                    passed=False,
                    severity="VETO",
                    actual="NO ROUTE / FAIL",
                    threshold=f"Loss <= {cfg.max_round_trip_loss_pct:.1f}%",
                    detail="DITOLAK (Fail-Closed): Jupiter gagal mendapatkan kuotasi beli 0.1 SOL.",
                ),
            ),
            round_trip_loss_01_sol_pct=None,
            depth_loss_1_sol_pct=None,
            buy_impact_01_sol_pct=None,
            sell_impact_01_sol_pct=None,
        )

    tokens_from_01 = int(buy_01["outAmount"])
    tokens_from_1 = int(buy_1["outAmount"]) if buy_1 else 0

    sell_tasks = [
        _fetch_jup_quote(client, mint, SOL_MINT, tokens_from_01, cfg.jupiter_api_key)
    ]
    if tokens_from_1 > 0:
        sell_tasks.append(
            _fetch_jup_quote(client, mint, SOL_MINT, tokens_from_1, cfg.jupiter_api_key)
        )
    sell_results = await asyncio.gather(*sell_tasks)
    sell_01 = sell_results[0]
    sell_1 = sell_results[1] if len(sell_results) > 1 else None

    return evaluate_execution_quotes(buy_01, sell_01, buy_1, sell_1, cfg)


def evaluate_execution_quotes(
    buy_01: Optional[dict[str, Any]],
    sell_01: Optional[dict[str, Any]],
    buy_1: Optional[dict[str, Any]],
    sell_1: Optional[dict[str, Any]],
    cfg: BotConfig,
) -> ExecutionResult:
    """Pure evaluator untuk hasil kuotasi Jupiter (100% fail-closed, ZERO clamping)."""
    checks: list[GateCheck] = []

    buy_impact_01 = _parse_impact_pct(buy_01)
    sell_impact_01 = _parse_impact_pct(sell_01)

    rt_loss_01: Optional[float] = None
    if buy_01 and sell_01 and int(sell_01.get("outAmount", 0)) > 0:
        sol_back_01 = int(sell_01["outAmount"])
        rt_loss_01 = round(max(0.0, (1.0 - sol_back_01 / LAMPORTS_01_SOL) * 100.0), 2)

    rt_01_ok = rt_loss_01 is not None and rt_loss_01 <= cfg.max_round_trip_loss_pct
    checks.append(
        GateCheck(
            id="jup_roundtrip_01",
            layer="EXECUTION",
            label="Pulang-Pergi 0.1 SOL (Anti-Honeypot)",
            passed=rt_01_ok,
            severity="PASS" if rt_01_ok else "VETO",
            actual=f"-{rt_loss_01:.2f}%" if rt_loss_01 is not None else "FAIL / UNSELLABLE",
            threshold=f"<= -{cfg.max_round_trip_loss_pct:.1f}%",
            detail=(
                f"Token dapat dijual kembali dengan loss wajar (-{rt_loss_01:.2f}%)."
                if rt_01_ok and rt_loss_01 is not None
                else "DITOLAK (Fail-Closed): Token tidak bisa dijual atau pajak/slippage 0.1 SOL terlalu tinggi."
            ),
        )
    )

    # 1.0 SOL Depth Cliff Stress Test — TIDAK BOLEH ada fallback clamp min(12.5, ...)
    depth_loss_1: Optional[float] = None
    if buy_1 and sell_1 and int(sell_1.get("outAmount", 0)) > 0:
        sol_back_1 = int(sell_1["outAmount"])
        depth_loss_1 = round(max(0.0, (1.0 - sol_back_1 / LAMPORTS_1_SOL) * 100.0), 2)

    depth_1_ok = depth_loss_1 is not None and depth_loss_1 <= cfg.max_depth_cliff_loss_pct
    checks.append(
        GateCheck(
            id="jup_depth_1sol",
            layer="EXECUTION",
            label="Uji Kedalaman Jual 1.0 SOL (Anti-Hollow Bin)",
            passed=depth_1_ok,
            severity="PASS" if depth_1_ok else "VETO",
            actual=f"-{depth_loss_1:.2f}%" if depth_loss_1 is not None else "FAIL / RATE-LIMIT",
            threshold=f"<= -{cfg.max_depth_cliff_loss_pct:.1f}% (fail-closed)",
            detail=(
                f"Kedalaman orderbook 1.0 SOL sehat (-{depth_loss_1:.2f}%)."
                if depth_1_ok and depth_loss_1 is not None
                else "DITOLAK (Fail-Closed): Kuotasi 1.0 SOL gagal atau slippage melebihi batas — rawan nyangkut saat exit."
            ),
        )
    )

    passed = rt_01_ok and depth_1_ok
    status = "VERIFIED" if passed else ("DEPTH_CLIFF_FAIL" if rt_01_ok else "ROUNDTRIP_FAIL")

    return ExecutionResult(
        passed=passed,
        status=status,
        checks=tuple(checks),
        round_trip_loss_01_sol_pct=rt_loss_01,
        depth_loss_1_sol_pct=depth_loss_1,
        buy_impact_01_sol_pct=buy_impact_01,
        sell_impact_01_sol_pct=sell_impact_01,
    )
