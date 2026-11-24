"""Vercel Health & Readiness Endpoint (GET / atau GET /api/index)."""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from bot.config import BotConfig
from bot.storage import validate_upstash_rest_url


class handler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        cfg = BotConfig.from_env()
        has_upstash = bool(
            validate_upstash_rest_url(cfg.upstash_redis_rest_url)
            and cfg.upstash_redis_rest_token
        )
        payload = {
            "service": "smart-money-fomo-radar-v2",
            "architecture": "5-layer-anti-top + pre-ath-sleeper-revival",
            "runtime": "vercel_serverless" if cfg.is_vercel else "python_http",
            "ready": bool(
                cfg.telegram_bot_token and cfg.telegram_chat_id and cfg.cron_secret
            ),
            "checks": {
                "telegram_bot_token": bool(cfg.telegram_bot_token),
                "telegram_chat_id": bool(cfg.telegram_chat_id),
                "cron_secret": bool(cfg.cron_secret),
                "telegram_webhook_secret": bool(cfg.telegram_webhook_secret),
                "persistent_storage": "upstash_redis" if has_upstash else "sqlite_tmp_ephemeral",
            },
            "endpoints": {
                "health": "/api/index",
                "cron_scan": "/api/scan",
                "telegram_webhook": "/api/webhook",
            },
        }
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
