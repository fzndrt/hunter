"""Vercel Cron / Scheduler Endpoint (GET & POST /api/scan) — 8-Feed Radar + Exit Guard."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
import httpx

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from bot.config import BotConfig
from bot.feeds import fetch_pair_for_mint
from bot.pipeline import run_scan_cycle
from bot.storage import create_store
from bot.telegram_notifier import (
    TelegramNotifier,
    format_entry_alert,
    format_exit_alert,
)

logger = logging.getLogger("corridor-desk.scan")
SCAN_HARD_TIMEOUT_SEC = 22.0


def is_authorized_cron_request(
    expected_secret: str, auth_header: str, cron_header: str
) -> bool:
    """Verifikasi CRON_SECRET secara constant-time dari Bearer atau X-Cron-Secret header."""
    if not expected_secret:
        return False
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    elif cron_header:
        token = cron_header.strip()
    if not token:
        return False
    return hmac.compare_digest(token, expected_secret)


async def execute_scan_and_exit_guard(cfg: BotConfig) -> dict:
    started = time.time()
    store = create_store(cfg)
    tg = TelegramNotifier(cfg)

    alerts_sent = 0
    exit_alerts_sent = 0
    exits_checked = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "CorridorDeskVercel/2.0"},
        follow_redirects=False,
    ) as client:
        # 1. Jalankan siklus pemindaian 8-feed (Fresh + Pre-ATH Sleeper Revival)
        results, feed_notes = await run_scan_cycle(client, cfg, max_stage2=10)
        ready = [r for r in results if r.verdict == "ENTRY_READY"]
        watch = [r for r in results if r.verdict == "WATCH_PULLBACK"]

        for audit in ready:
            if await store.should_send_alert(
                audit.pair.mint, "ENTRY_READY", cfg.alert_cooldown_min
            ):
                if tg.enabled:
                    sent = await tg.send_message(client, format_entry_alert(audit))
                    if sent:
                        alerts_sent += 1

        # 2. Jalankan Exit Guard untuk seluruh posisi aktif dalam invocation yang sama
        positions = await store.list_positions()
        for pos in positions:
            live_pair = await fetch_pair_for_mint(client, pos.mint, ("exit_guard",))
            if not live_pair:
                continue
            exits_checked += 1
            updated = await store.update_position_state(pos, live_pair)
            if updated.signal != "HOLDING_SAFE":
                cooldown = (
                    3
                    if updated.signal in {"HARD_STOP_LOSS", "LP_PULL_EMERGENCY"}
                    else 15
                )
                if await store.should_send_alert(updated.mint, updated.signal, cooldown):
                    if tg.enabled:
                        sent = await tg.send_message(client, format_exit_alert(updated))
                        if sent:
                            exit_alerts_sent += 1

    return {
        "ok": True,
        "storage_mode": getattr(store, "mode", "sqlite"),
        "telegram_enabled": tg.enabled,
        "audited": len(results),
        "entry_ready": len(ready),
        "watch_pullback": len(watch),
        "entry_alerts_sent": alerts_sent,
        "exits_checked": exits_checked,
        "exit_alerts_sent": exit_alerts_sent,
        "duration_ms": int((time.time() - started) * 1000),
        "feeds": feed_notes,
        "top_ready": [
            {
                "symbol": r.pair.symbol,
                "mint": r.pair.mint,
                "score": r.total_score,
                "sleeper_revival": r.anti_top.is_sleeper_revival,
            }
            for r in ready[:5]
        ],
    }


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _check_auth(self, cfg: BotConfig) -> tuple[int, dict] | None:
        if not cfg.cron_secret:
            return (
                503,
                {
                    "ok": False,
                    "error_code": "MISSING_CRON_SECRET",
                    "error": "CRON_SECRET belum diatur di Vercel Environment Variables. Tambahkan lalu klik Redeploy.",
                },
            )

        auth_header = self.headers.get("Authorization", "")
        cron_header = self.headers.get("X-Cron-Secret", "")
        if not is_authorized_cron_request(cfg.cron_secret, auth_header, cron_header):
            return (
                401,
                {
                    "ok": False,
                    "error_code": "INVALID_CRON_SECRET",
                    "error": "Header Authorization: Bearer <CRON_SECRET> atau X-Cron-Secret tidak cocok.",
                },
            )
        return None

    def _handle(self) -> None:
        cfg = BotConfig.from_env()
        auth_err = self._check_auth(cfg)
        if auth_err is not None:
            status, payload = auth_err
            self._send_json(status, payload)
            return

        try:
            summary = asyncio.run(
                asyncio.wait_for(
                    execute_scan_and_exit_guard(cfg),
                    timeout=SCAN_HARD_TIMEOUT_SEC,
                )
            )
            self._send_json(200, summary)
        except asyncio.TimeoutError:
            logger.warning("Siklus /api/scan melebihi timeout %.1fs", SCAN_HARD_TIMEOUT_SEC)
            self._send_json(
                504,
                {
                    "ok": False,
                    "error_code": "SCAN_TIMEOUT",
                    "error": "Siklus scan melebihi batas waktu 22 detik (upstream DEX/RugCheck lambat).",
                },
            )
        except Exception as exc:
            logger.exception("Error /api/scan")
            self._send_json(
                500,
                {
                    "ok": False,
                    "error_code": "SCAN_ERROR",
                    "error_type": type(exc).__name__,
                    "error": "Gagal menjalankan siklus scan. Cek Vercel Function Logs untuk detail.",
                },
            )

    def do_HEAD(self) -> None:
        cfg = BotConfig.from_env()
        auth_err = self._check_auth(cfg)
        if auth_err is not None:
            self._send_empty(auth_err[0])
            return
        self._send_empty(200)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()
