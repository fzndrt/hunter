"""Vercel Telegram Webhook Endpoint (POST /api/webhook) — Perintah Bot Instan."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
import httpx

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from bot.config import BotConfig
from bot.storage import create_store
from bot.telegram_notifier import TelegramNotifier, handle_telegram_command

MAX_WEBHOOK_BODY_BYTES = 65_536


async def process_telegram_update(cfg: BotConfig, update: dict) -> bool:
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    text = str(msg.get("text") or "").strip()

    # Otorisasi ketat: hanya eksekusi perintah dari TELEGRAM_CHAT_ID pemilik
    if not chat_id or not cfg.telegram_chat_id or chat_id != cfg.telegram_chat_id:
        return False
    if not text.startswith("/"):
        return False

    store = create_store(cfg)
    tg = TelegramNotifier(cfg)
    if not tg.enabled:
        return False

    async with httpx.AsyncClient(
        headers={"User-Agent": "CorridorDeskWebhook/2.0"},
        follow_redirects=False,
    ) as client:
        await handle_telegram_command(client, cfg, store, tg, chat_id, text)
    return True


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send_json(
            200,
            {
                "ok": True,
                "webhook": "active",
                "hint": "Daftarkan URL ini lewat Telegram setWebhook dengan secret_token.",
            },
        )

    def do_POST(self) -> None:
        cfg = BotConfig.from_env()

        # Verifikasi secret token webhook jika dikonfigurasi (sangat disarankan)
        if cfg.telegram_webhook_secret:
            incoming_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "").strip()
            if not incoming_secret or not hmac.compare_digest(
                incoming_secret, cfg.telegram_webhook_secret
            ):
                self._send_json(401, {"ok": False, "error": "Invalid webhook secret"})
                return

        try:
            content_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"ok": False, "error": "Invalid Content-Length"})
            return

        if content_len <= 0 or content_len > MAX_WEBHOOK_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "Payload terlalu besar atau kosong"})
            return

        raw_body = self.rfile.read(content_len)
        try:
            update = json.loads(raw_body.decode("utf-8"))
            if not isinstance(update, dict):
                raise ValueError("Update must be JSON object")
        except Exception:
            self._send_json(400, {"ok": False, "error": "JSON tidak valid"})
            return

        try:
            handled = asyncio.run(process_telegram_update(cfg, update))
            self._send_json(200, {"ok": True, "handled": handled})
        except Exception:
            # Tetap balas 200 ke Telegram agar tidak terjadi retry storm berkepanjangan
            self._send_json(200, {"ok": False, "handled": False})
