"""Dataclass terstruktur untuk seluruh 5 layer mesin audit & Exit Guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

LayerName = Literal["SECURITY", "ANTI_TOP", "EXECUTION"]
CheckSeverity = Literal["PASS", "WAIT_DIP", "VETO"]
Verdict = Literal["ENTRY_READY", "WATCH_PULLBACK", "REJECTED"]
ExitSignalCode = Literal[
    "HOLDING_SAFE",
    "TP1_DERISK_BREAKEVEN",
    "TP2_TAKE_PROFIT",
    "TRAILING_PEAK_EXIT",
    "WHALE_DUMP_DIVERGENCE",
    "LP_PULL_EMERGENCY",
    "HARD_STOP_LOSS",
]


@dataclass(frozen=True)
class GateCheck:
    id: str
    layer: LayerName
    label: str
    passed: bool
    severity: CheckSeverity
    actual: str
    threshold: str
    detail: str


@dataclass(frozen=True)
class PairSnapshot:
    mint: str
    pair_address: str
    dex_id: str
    symbol: str
    name: str
    quote_mint: str
    quote_symbol: str
    price_usd: float
    market_cap: float
    liquidity_usd: float
    vol_m5: float
    vol_h1: float
    vol_h24: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    sells_h1: int
    buys_h24: int
    sells_h24: int
    price_change_m5: float
    price_change_h1: float
    price_change_h24: float
    pair_created_at_ms: int
    feed_sources: tuple[str, ...] = ()
    url: str = ""
    vol_h6: float = 0.0
    price_change_h6: float = 0.0


@dataclass(frozen=True)
class SecurityResult:
    passed: bool
    checks: tuple[GateCheck, ...]
    hard_quote_symbol: str
    rugcheck_score: Optional[int]
    dev_pct: Optional[float]
    top1_pct: Optional[float]
    top10_pct: Optional[float]
    lp_locked_pct: Optional[float]
    lp_locked_usd: Optional[float]
    is_meteora_nft_lp: bool
    pareto_status: str


@dataclass(frozen=True)
class AntiTopResult:
    passed: bool
    wait_dip_only: bool
    checks: tuple[GateCheck, ...]
    corridor_id: Optional[str]
    corridor_label: str
    spot_mc: float
    projected_entry_mc: float
    projected_delay_drift_pct: float
    net_headroom_pct: float
    age_minutes: float
    liq_usd: float
    liq_mc_ratio_pct: float
    p_m5: float
    p_h1: float
    tx_24h: int
    effective_min_tx_24h: int
    avg_ticket_1h_usd: float
    buy_ratio_5m_pct: float
    buy_ratio_1h_pct: float
    p_h6: float = 0.0
    p_h24: float = 0.0
    is_sleeper_revival: bool = False
    vol_wake_5m_ratio: float = 0.0
    vol_h1_share_24h_pct: float = 0.0


@dataclass(frozen=True)
class ExecutionResult:
    passed: bool
    status: str
    checks: tuple[GateCheck, ...]
    round_trip_loss_01_sol_pct: Optional[float]
    depth_loss_1_sol_pct: Optional[float]
    buy_impact_01_sol_pct: Optional[float]
    sell_impact_01_sol_pct: Optional[float]


@dataclass(frozen=True)
class PillarScore:
    name: str
    score: int
    max_score: int
    summary: str


@dataclass(frozen=True)
class TradePlan:
    corridor_label: str
    max_safe_entry_mc: float
    hard_stop_mc: float
    hard_stop_pct: float
    tp1_mc: float
    tp1_pct: float
    tp2_mc: float
    tp2_pct: float
    risk_reward_to_tp1: float
    risk_reward_to_tp2: float


@dataclass(frozen=True)
class AuditResult:
    pair: PairSnapshot
    security: SecurityResult
    anti_top: AntiTopResult
    execution: ExecutionResult
    pillars: tuple[PillarScore, ...]
    total_score: int
    verdict: Verdict
    headline: str
    trade_plan: Optional[TradePlan]
    audited_at_ms: int


@dataclass
class TrackedPosition:
    id: str
    mint: str
    pair_address: str
    symbol: str
    name: str
    entry_mc: float
    entry_price_usd: float
    entry_liq_usd: float
    position_usd: float
    hard_stop_pct: float
    tp1_pct: float
    tp2_pct: float
    peak_mc: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    live_mc: float = 0.0
    live_liq_usd: float = 0.0
    pnl_pct: float = 0.0
    real_lp_retention_pct: Optional[float] = None
    signal: ExitSignalCode = "HOLDING_SAFE"
    signal_detail: str = "Menunggu pengecekan live."
    created_at_ms: int = 0
    updated_at_ms: int = 0
    notes: str = ""
    extra: dict[str, str] = field(default_factory=dict)
