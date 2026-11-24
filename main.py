"""Entrypoint Async Utama — Smart Money FOMO Radar v2 (Clean 5-Layer Architecture)."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import httpx

from bot.config import BotConfig
from bot.feeds import fetch_live_sol_price_usd, fetch_pair_for_mint
from bot.pipeline import run_scan_cycle
from bot.pumpportal_stream import stream_token_trades
from bot.storage import PositionStore, create_store
from bot.telegram_notifier import (
    TelegramNotifier,
    format_entry_alert,
    format_exit_alert,
    handle_telegram_command,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("corridor-desk")


async def scanner_loop(
    client: httpx.AsyncClient,
    cfg: BotConfig,
    store: PositionStore,
    tg: TelegramNotifier,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            results, notes = await run_scan_cycle(client, cfg)
            ready = [r for r in results if r.verdict == "ENTRY_READY"]
            watch = [r for r in results if r.verdict == "WATCH_PULLBACK"]
            logger.info(
                "Scan selesai: %d diaudit | %d ENTRY_READY | %d WATCH | feeds=%s",
                len(results),
                len(ready),
                len(watch),
                ",".join(notes),
            )
            for audit in ready:
                if await store.should_send_alert(
                    audit.pair.mint, "ENTRY_READY", cfg.alert_cooldown_min
                ):
                    msg = format_entry_alert(audit)
                    if tg.enabled:
                        await tg.send_message(client, msg)
                    else:
                        logger.info("ENTRY READY: $%s (%s) score=%d", audit.pair.symbol, audit.pair.mint, audit.total_score)
        except Exception as exc:
            logger.exception("Error siklus scanner: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=cfg.scan_interval_sec)
        except asyncio.TimeoutError:
            continue


async def exit_guard_loop(
    client: httpx.AsyncClient,
    cfg: BotConfig,
    store: PositionStore,
    tg: TelegramNotifier,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            positions = await store.list_positions()
            for pos in positions:
                live_pair = await fetch_pair_for_mint(client, pos.mint, ("exit_guard",))
                if not live_pair:
                    continue
                updated = await store.update_position_state(pos, live_pair)
                if updated.signal != "HOLDING_SAFE":
                    cooldown = 3 if updated.signal in {"HARD_STOP_LOSS", "LP_PULL_EMERGENCY"} else 15
                    if await store.should_send_alert(updated.mint, updated.signal, cooldown):
                        if tg.enabled:
                            await tg.send_message(client, format_exit_alert(updated))
                        else:
                            logger.warning(
                                "EXIT SIGNAL [%s] $%s: %s",
                                updated.signal,
                                updated.symbol,
                                updated.signal_detail,
                            )
        except Exception as exc:
            logger.exception("Error siklus Exit Guard: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=cfg.exit_check_interval_sec)
        except asyncio.TimeoutError:
            continue


async def telegram_command_loop(
    client: httpx.AsyncClient,
    cfg: BotConfig,
    store: PositionStore,
    tg: TelegramNotifier,
    stop_event: asyncio.Event,
) -> None:
    if not tg.enabled:
        return
    while not stop_event.is_set():
        commands = await tg.poll_commands(client)
        for chat_id, text in commands:
            await handle_telegram_command(client, cfg, store, tg, chat_id, text)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            continue


async def run() -> None:
    cfg = BotConfig.from_env()
    store = create_store(cfg)
    tg = TelegramNotifier(cfg)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    sol_price_cache = [cfg.sol_price_fallback_usd]

    async with httpx.AsyncClient(
        headers={"User-Agent": "CorridorDeskBot/2.0"},
        follow_redirects=False,
    ) as client:
        sol_price_cache[0] = await fetch_live_sol_price_usd(
            client, cfg.sol_price_fallback_usd
        )
        logger.info(
            "Corridor Desk Bot v2 aktif | DB=%s | SOL=$%.2f | Telegram=%s",
            cfg.db_path,
            sol_price_cache[0],
            "ON" if tg.enabled else "OFF (log only)",
        )

        tasks = [
            asyncio.create_task(scanner_loop(client, cfg, store, tg, stop_event)),
            asyncio.create_task(exit_guard_loop(client, cfg, store, tg, stop_event)),
            asyncio.create_task(telegram_command_loop(client, cfg, store, tg, stop_event)),
        ]

        if cfg.enable_pumpportal_stream:
            active_mints_cache: list[str] = []

            async def on_trade(evt: dict) -> None:
                logger.info(
                    "PumpPortal Trade: %s %s %.2f SOL ($%.0f) MC=$%.0f curve=%s%%",
                    evt["tx_type"].upper(),
                    evt["mint"][:8],
                    evt["sol_amount"],
                    evt["usd_amount"],
                    evt["market_cap_usd"],
                    evt["bonding_progress_pct"],
                )

            tasks.append(
                asyncio.create_task(
                    stream_token_trades(
                        mints_provider=lambda: active_mints_cache,
                        sol_price_provider=lambda: sol_price_cache[0],
                        on_large_trade=on_trade,
                        min_sol_trade=cfg.pumpportal_min_sol_trade,
                        stop_event=stop_event,
                    )
                )
            )

        await stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)
