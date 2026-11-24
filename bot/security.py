"""Layer 1 — Binary Security Veto (Zero Bypass, 100% Fail-Closed)."""

from __future__ import annotations

from typing import Any, Optional
import httpx

from .config import (
    KNOWN_PROTOCOL_AUTHORITIES,
    METEORA_NFT_LP_SENTINEL,
    VERIFIED_HARD_QUOTE_MINTS,
    is_valid_solana_mint,
)
from .models import GateCheck, PairSnapshot, SecurityResult

DANGEROUS_TOKEN2022_EXTENSIONS: tuple[str, ...] = (
    "permanentdelegate",
    "transferhook",
    "transferfee",
    "defaultaccountstate",
    "nontransferable",
)


async def fetch_rugcheck_report(
    client: httpx.AsyncClient, mint: str
) -> Optional[dict[str, Any]]:
    if not is_valid_solana_mint(mint):
        return None
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
    try:
        resp = await client.get(url, timeout=8.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def evaluate_security(
    pair: PairSnapshot, rugcheck_report: Optional[dict[str, Any]]
) -> SecurityResult:
    checks: list[GateCheck] = []

    # 1. Strict Hard-Quote Mint Allowlist (ZERO ticker string / liq >= $6K bypass)
    quote_symbol = VERIFIED_HARD_QUOTE_MINTS.get(pair.quote_mint)
    hard_quote_ok = quote_symbol is not None
    checks.append(
        GateCheck(
            id="hard_quote_mint",
            layer="SECURITY",
            label="Hard-Quote Mint Allowlist",
            passed=hard_quote_ok,
            severity="PASS" if hard_quote_ok else "VETO",
            actual=f"{quote_symbol} ({pair.quote_mint[:6]}…)" if quote_symbol else f"UNVERIFIED ({pair.quote_symbol})",
            threshold="Mint resmi: SOL/USDC/USDT/WBTC/cbBTC/USD1/PUMP",
            detail=(
                f"Kolam dipasangkan dengan kontrak {quote_symbol} terverifikasi."
                if hard_quote_ok
                else "DITOLAK: Quote mint tidak terdaftar di allowlist resmi (rawan fake quote / spoof ticker)."
            ),
        )
    )

    # 2. RugCheck Fail-Closed Availability
    if not rugcheck_report:
        checks.append(
            GateCheck(
                id="rugcheck_available",
                layer="SECURITY",
                label="RugCheck On-Chain Audit",
                passed=False,
                severity="VETO",
                actual="UNAVAILABLE",
                threshold="Data RugCheck wajib tersedia (fail-closed)",
                detail="DITOLAK (Fail-Closed): Laporan RugCheck tidak tersedia / timeout — uang riil tidak boleh masuk buta.",
            )
        )
        return SecurityResult(
            passed=False,
            checks=tuple(checks),
            hard_quote_symbol=quote_symbol or pair.quote_symbol or "UNKNOWN",
            rugcheck_score=None,
            dev_pct=None,
            top1_pct=None,
            top10_pct=None,
            lp_locked_pct=None,
            lp_locked_usd=None,
            is_meteora_nft_lp=False,
            pareto_status="UNAVAILABLE",
        )

    # 3. Mint & Freeze Authority + Rugged Flag
    token_info = rugcheck_report.get("token") or {}
    mint_auth = token_info.get("mintAuthority")
    freeze_auth = token_info.get("freezeAuthority")
    is_rugged = bool(rugcheck_report.get("rugged"))
    auth_ok = (not mint_auth) and (not freeze_auth) and (not is_rugged)
    checks.append(
        GateCheck(
            id="authorities_revoked",
            layer="SECURITY",
            label="Mint / Freeze Authority & Rugged Flag",
            passed=auth_ok,
            severity="PASS" if auth_ok else "VETO",
            actual=(
                "Revoked & Clean"
                if auth_ok
                else f"mintAuth={bool(mint_auth)} freezeAuth={bool(freeze_auth)} rugged={is_rugged}"
            ),
            threshold="Mint=Null, Freeze=Null, Rugged=False",
            detail=(
                "Otoritas cetak & beku token sudah hangus."
                if auth_ok
                else "DITOLAK: Token masih bisa dicetak ulang, dibekukan, atau ditandai rugged."
            ),
        )
    )

    # 4. Token-2022 Dangerous Extensions & Mutable Metadata / Critical Risks
    token_ext = rugcheck_report.get("token_extensions") or {}
    risks = rugcheck_report.get("risks") or []
    found_dangerous_ext: list[str] = []
    if isinstance(token_ext, dict):
        for k, v in token_ext.items():
            if v and any(d in str(k).lower() for d in DANGEROUS_TOKEN2022_EXTENSIONS):
                found_dangerous_ext.append(str(k))
    for r in risks:
        r_name = str(r.get("name", "")).lower()
        r_level = str(r.get("level", "")).lower()
        if any(
            kw in r_name
            for kw in ("transfer hook", "permanent delegate", "transfer fee", "freeze", "honeypot")
        ) and r_level in {"danger", "error", "critical"}:
            found_dangerous_ext.append(str(r.get("name", "danger")))

    ext_ok = len(found_dangerous_ext) == 0
    checks.append(
        GateCheck(
            id="token2022_extensions",
            layer="SECURITY",
            label="Token-2022 & Kontrak Berbahaya",
            passed=ext_ok,
            severity="PASS" if ext_ok else "VETO",
            actual="Bersih" if ext_ok else ", ".join(sorted(set(found_dangerous_ext)))[:60],
            threshold="0 ekstensi berbahaya (TransferHook/PermanentDelegate/Fee)",
            detail=(
                "Tidak ditemukan ekstensi Token-2022 berbahaya."
                if ext_ok
                else f"DITOLAK: Terdeteksi risiko kontrak berbahaya ({', '.join(sorted(set(found_dangerous_ext)))})."
            ),
        )
    )

    # 5. Creator / Dev Supply Cap (<= 2.5%)
    creator = str(rugcheck_report.get("creator") or "")
    top_holders = rugcheck_report.get("topHolders") or []
    dev_pct = 0.0
    non_protocol_holders: list[float] = []
    for h in top_holders:
        owner = str(h.get("owner") or h.get("address") or "")
        pct = float(h.get("pct") or 0.0)
        is_insider = bool(h.get("insider"))
        if owner and creator and owner == creator:
            dev_pct = max(dev_pct, pct)
        if owner in KNOWN_PROTOCOL_AUTHORITIES:
            continue
        if is_insider and not dev_pct:
            dev_pct = max(dev_pct, pct)
        non_protocol_holders.append(pct)

    dev_ok = dev_pct <= 2.5
    checks.append(
        GateCheck(
            id="dev_supply",
            layer="SECURITY",
            label="Kepemilikan Dev / Creator",
            passed=dev_ok,
            severity="PASS" if dev_ok else "VETO",
            actual=f"{dev_pct:.2f}%",
            threshold="<= 2.50%",
            detail=(
                f"Supply dev terkendali ({dev_pct:.2f}%)."
                if dev_ok
                else f"DITOLAK: Dev/insider memegang {dev_pct:.2f}% (> 2.50%, rawan dump)."
            ),
        )
    )

    # 6. LP Lock >= 80% OR >= $18K locked pool (with explicit Meteora NFT/DLMM awareness)
    markets = rugcheck_report.get("markets") or []
    best_lp_lock_pct = 0.0
    best_lp_lock_usd = 0.0
    is_meteora_nft_lp = False

    for m in markets:
        lp = m.get("lp") or {}
        lp_mint = str(lp.get("lpMint") or "")
        market_type = str(m.get("marketType") or "").lower()
        if lp_mint == METEORA_NFT_LP_SENTINEL or "meteora" in market_type:
            is_meteora_nft_lp = True
        lock_pct = float(lp.get("lpLockedPct") or 0.0)
        lock_usd = float(lp.get("lpLockedUSD") or 0.0)
        if lock_pct > best_lp_lock_pct:
            best_lp_lock_pct = lock_pct
        if lock_usd > best_lp_lock_usd:
            best_lp_lock_usd = lock_usd

    lp_ok = best_lp_lock_pct >= 80.0 or best_lp_lock_usd >= 18_000.0
    if lp_ok:
        lp_detail = (
            f"LP terverifikasi terkunci ({best_lp_lock_pct:.1f}% / ${best_lp_lock_usd:,.0f})."
            if best_lp_lock_pct >= 80.0
            else f"Pool AMM terkunci >= $18K (${best_lp_lock_usd:,.0f}) meski ada posisi NFT/CLMM paralel."
        )
    elif is_meteora_nft_lp:
        lp_detail = (
            "DITOLAK: Pool Meteora DLMM/DAMM v2 memakai NFT position tanpa bukti lock >= $18K di AMM terverifikasi."
        )
    else:
        lp_detail = f"DITOLAK: LP terkunci hanya {best_lp_lock_pct:.1f}% (${best_lp_lock_usd:,.0f}) — rawan rugpull tarik LP."

    checks.append(
        GateCheck(
            id="lp_lock",
            layer="SECURITY",
            label="Kunci Likuiditas (LP Lock)",
            passed=lp_ok,
            severity="PASS" if lp_ok else "VETO",
            actual=f"{best_lp_lock_pct:.1f}% (${best_lp_lock_usd:,.0f})",
            threshold=">= 80% terkunci ATAU >= $18K locked pool",
            detail=lp_detail,
        )
    )

    # 7. Pareto Conservation Law (Anti-Whale Concentration & Anti-Nano-Swarm/Cabal)
    non_protocol_holders.sort(reverse=True)
    top1_pct = non_protocol_holders[0] if non_protocol_holders else 0.0
    top10_pct = sum(non_protocol_holders[:10]) if non_protocol_holders else 0.0
    top5 = non_protocol_holders[:5]

    # Deteksi Nano-Swarm buatan: 5 wallet teratas hampir seragam persis (spread < 0.35%) dengan total > 9%
    is_nano_swarm = (
        len(top5) >= 5
        and (top5[0] - top5[4]) < 0.35
        and sum(top5) >= 9.0
    )
    pareto_ok = (top1_pct <= 6.0) and (top10_pct <= 30.0) and (not is_nano_swarm)
    if is_nano_swarm:
        pareto_status = "NANO_SWARM_DETECTED"
        pareto_detail = (
            f"DITOLAK: Pola Nano-Swarm/Cabal terdeteksi — Top 5 holder nyaris seragam ({top5[0]:.2f}%..{top5[4]:.2f}%)."
        )
    elif top1_pct > 6.0 or top10_pct > 30.0:
        pareto_status = "WHALE_CONCENTRATED"
        pareto_detail = (
            f"DITOLAK: Konsentrasi holder terlalu tinggi (Top1={top1_pct:.2f}%, Top10={top10_pct:.2f}%)."
        )
    else:
        pareto_status = "HEALTHY_PARETO"
        pareto_detail = f"Distribusi organik lolos Hukum Pareto (Top1={top1_pct:.2f}%, Top10={top10_pct:.2f}%)."

    checks.append(
        GateCheck(
            id="pareto_distribution",
            layer="SECURITY",
            label="Hukum Pareto & Anti-Nano-Swarm",
            passed=pareto_ok,
            severity="PASS" if pareto_ok else "VETO",
            actual=f"Top1 {top1_pct:.2f}% · Top10 {top10_pct:.2f}%",
            threshold="Top1 <= 6.0%, Top10 <= 30.0%, non-seragam",
            detail=pareto_detail,
        )
    )

    rc_score_raw = rugcheck_report.get("score_normalised")
    if rc_score_raw is None:
        rc_score_raw = rugcheck_report.get("score")
    rc_score = int(rc_score_raw) if isinstance(rc_score_raw, (int, float)) else None

    all_passed = all(c.passed for c in checks)
    return SecurityResult(
        passed=all_passed,
        checks=tuple(checks),
        hard_quote_symbol=quote_symbol or pair.quote_symbol or "UNKNOWN",
        rugcheck_score=rc_score,
        dev_pct=round(dev_pct, 2),
        top1_pct=round(top1_pct, 2),
        top10_pct=round(top10_pct, 2),
        lp_locked_pct=round(best_lp_lock_pct, 1),
        lp_locked_usd=round(best_lp_lock_usd, 0),
        is_meteora_nft_lp=is_meteora_nft_lp,
        pareto_status=pareto_status,
    )
