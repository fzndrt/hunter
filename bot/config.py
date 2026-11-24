"""Konfigurasi terpusat, allowlist mint resmi, dan batas koridor entry."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

BASE58_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

SOL_MINT = "So11111111111111111111111111111111111111112"
METEORA_NFT_LP_SENTINEL = "11111111111111111111111111111111"

# HANYA alamat kontrak mint Solana yang terverifikasi.
# Ticker string ("NVDA", "USDG", dll.) DILARANG keras digunakan sebagai validasi.
VERIFIED_HARD_QUOTE_MINTS: dict[str, str] = {
    SOL_MINT: "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "WBTC",
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": "cbBTC",
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB": "USD1",
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn": "PUMP",
}

# Program & otoritas pool DEX — WAJIB dikecualikan agar tidak salah dilabeli sebagai Whale/Dev.
KNOWN_PROTOCOL_AUTHORITIES: frozenset[str] = frozenset(
    {
        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium Authority V4
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM V4
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
        "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # Pump.fun Program
        "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # Pump.fun Migration
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # PumpSwap AMM
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
        "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # Meteora DLMM
        "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora Pools
        "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",   # OpenBook / Serum
        "11111111111111111111111111111111",              # System Program
    }
)

# Host eksternal yang diizinkan (SSRF guard)
ALLOWED_EXTERNAL_HOSTS: frozenset[str] = frozenset(
    {
        "api.dexscreener.com",
        "api.geckoterminal.com",
        "api.rugcheck.xyz",
        "lite-api.jup.ag",
        "api.jup.ag",
        "api.telegram.org",
    }
)


@dataclass(frozen=True)
class EntryCorridor:
    corridor_id: str
    label: str
    min_mc: float
    max_mc: float
    ceiling_mc: float
    default_hard_stop_pct: float
    default_tp1_pct: float
    default_tp2_pct: float


ENTRY_CORRIDORS: tuple[EntryCorridor, ...] = (
    EntryCorridor(
        corridor_id="CORRIDOR_A_EARLY",
        label="Corridor A (Early Ignition $22K–$78K)",
        min_mc=22_000.0,
        max_mc=78_000.0,
        ceiling_mc=100_000.0,
        default_hard_stop_pct=18.0,
        default_tp1_pct=45.0,
        default_tp2_pct=110.0,
    ),
    EntryCorridor(
        corridor_id="CORRIDOR_B_POST100K",
        label="Corridor B (Post-100K Breakout $112K–$172K)",
        min_mc=112_000.0,
        max_mc=172_000.0,
        ceiling_mc=200_000.0,
        default_hard_stop_pct=15.0,
        default_tp1_pct=40.0,
        default_tp2_pct=90.0,
    ),
    EntryCorridor(
        corridor_id="CORRIDOR_C_EXPANSION",
        label="Corridor C (Mid Expansion $220K–$335K)",
        min_mc=220_000.0,
        max_mc=335_000.0,
        ceiling_mc=450_000.0,
        default_hard_stop_pct=14.0,
        default_tp1_pct=35.0,
        default_tp2_pct=75.0,
    ),
)


def is_valid_solana_mint(value: str) -> bool:
    return bool(BASE58_MINT_RE.match(value.strip()))


def _env_float(key: str, default: float, lo: float, hi: float) -> float:
    try:
        val = float(os.getenv(key, str(default)))
    except ValueError:
        return default
    return max(lo, min(hi, val))


def _env_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        val = int(os.getenv(key, str(default)))
    except ValueError:
        return default
    return max(lo, min(hi, val))


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class BotConfig:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""
    cron_secret: str = ""
    jupiter_api_key: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    db_path: str = "./data/corridor_desk.db"
    upstash_redis_rest_url: str = ""
    upstash_redis_rest_token: str = ""
    is_vercel: bool = False

    scan_interval_sec: int = 45
    exit_check_interval_sec: int = 20
    alert_cooldown_min: int = 30

    reaction_delay_sec: int = 22
    min_score: int = 72
    max_round_trip_loss_pct: float = 4.5
    max_depth_cliff_loss_pct: float = 13.5
    min_net_headroom_pct: float = 20.0
    max_m5_change_pct: float = 28.0
    max_h1_change_pct: float = 145.0
    min_liquidity_usd: float = 12_000.0
    include_midcap_corridor: bool = True

    enable_pumpportal_stream: bool = False
    pumpportal_min_sol_trade: float = 3.0
    sol_price_fallback_usd: float = 160.0

    @classmethod
    def from_env(cls) -> "BotConfig":
        is_vercel = os.getenv("VERCEL", "").strip() in {"1", "true", "TRUE"}
        default_db = "/tmp/corridor_desk.db" if is_vercel else "./data/corridor_desk.db"
        raw_db_path = os.getenv("DB_PATH", "").strip()
        if not raw_db_path:
            resolved_db = default_db
        elif is_vercel and not raw_db_path.startswith("/tmp/"):
            # Vercel filesystem bersifat read-only kecuali /tmp
            resolved_db = "/tmp/corridor_desk.db"
        else:
            resolved_db = raw_db_path

        redis_url = (
            os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
            or os.getenv("KV_REST_API_URL", "").strip()
        )
        redis_token = (
            os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
            or os.getenv("KV_REST_API_TOKEN", "").strip()
        )

        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip(),
            cron_secret=os.getenv("CRON_SECRET", "").strip(),
            jupiter_api_key=os.getenv("JUPITER_API_KEY", "").strip(),
            solana_rpc_url=os.getenv(
                "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
            ).strip(),
            db_path=resolved_db,
            upstash_redis_rest_url=redis_url,
            upstash_redis_rest_token=redis_token,
            is_vercel=is_vercel,
            scan_interval_sec=_env_int("SCAN_INTERVAL_SEC", 45, 15, 600),
            exit_check_interval_sec=_env_int("EXIT_CHECK_INTERVAL_SEC", 20, 10, 300),
            alert_cooldown_min=_env_int("ALERT_COOLDOWN_MIN", 30, 5, 1440),
            reaction_delay_sec=_env_int("REACTION_DELAY_SEC", 22, 5, 90),
            min_score=_env_int("MIN_SCORE", 72, 40, 95),
            max_round_trip_loss_pct=_env_float("MAX_ROUND_TRIP_LOSS_PCT", 4.5, 1.5, 12.0),
            max_depth_cliff_loss_pct=_env_float("MAX_DEPTH_CLIFF_LOSS_PCT", 13.5, 4.0, 25.0),
            min_net_headroom_pct=_env_float("MIN_NET_HEADROOM_PCT", 20.0, 8.0, 60.0),
            max_m5_change_pct=_env_float("MAX_M5_CHANGE_PCT", 28.0, 8.0, 60.0),
            max_h1_change_pct=_env_float("MAX_H1_CHANGE_PCT", 145.0, 40.0, 400.0),
            min_liquidity_usd=_env_float("MIN_LIQUIDITY_USD", 12_000.0, 5_000.0, 250_000.0),
            include_midcap_corridor=_env_bool("INCLUDE_MIDCAP_CORRIDOR", True),
            enable_pumpportal_stream=_env_bool("ENABLE_PUMPPORTAL_STREAM", False),
            pumpportal_min_sol_trade=_env_float("PUMPPORTAL_MIN_SOL_TRADE", 3.0, 0.5, 100.0),
            sol_price_fallback_usd=_env_float("SOL_PRICE_FALLBACK_USD", 160.0, 10.0, 2000.0),
        )

