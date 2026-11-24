"""Pengirim Alert & Command Handler Telegram (HTML-safe, non-blocking)."""

from __future__ import annotations

import html
import logging
from typing import Optional
import httpx

from .config import BotConfig, SOL_MINT
from .models import AuditResult, TrackedPosition

logger = logging.getLogger(__name__)


def _esc(val: object) -> str:
    return html.escape(str(val or ""))


def format_entry_alert(audit: AuditResult) -> str:
    p = audit.pair
    at = audit.anti_top
    ex = audit.execution
    sec = audit.security
    plan = audit.trade_plan

    age_days = at.age_minutes / 1440.0
    age_label = f"{age_days:.1f} hari" if age_days >= 1.0 else (f"{at.age_minutes / 60.0:.1f} jam" if at.age_minutes >= 60.0 else f"{at.age_minutes:.0f} menit")
    title_line = (
        f"⚡ <b>SLEEPER REVIVAL (PRE-ATH) — ${_esc(p.symbol)}</b> (Skor <b>{audit.total_score}/100</b>)"
        if at.is_sleeper_revival
        else f"🟢 <b>ENTRY READY — ${_esc(p.symbol)}</b> (Skor <b>{audit.total_score}/100</b>)"
    )

    lines = [
        title_line,
        f"Nama: {_esc(p.name)} | DEX: <code>{_esc(p.dex_id)}</code> | Quote: <b>{_esc(sec.hard_quote_symbol)}</b> | Usia: <b>{age_label}</b>",
        f"Mint: <code>{_esc(p.mint)}</code>",
        "",
        f"• Spot MC: <b>${at.spot_mc:,.0f}</b> → Proyeksi (+delay): <b>${at.projected_entry_mc:,.0f}</b>",
        f"• Koridor: {_esc(at.corridor_label)} (Headroom <b>+{at.net_headroom_pct:.1f}%</b>)",
        f"• 5m / 1h / 6h / 24h: <code>{at.p_m5:+.1f}%</code> / <code>{at.p_h1:+.1f}%</code> / <code>{at.p_h6:+.1f}%</code> / <code>{at.p_h24:+.1f}%</code>",
        f"• LP: <b>${at.liq_usd:,.0f}</b> ({at.liq_mc_ratio_pct:.0f}% MC)" + (
            f" | Wake-Up Vol: <b>x{at.vol_wake_5m_ratio:.1f}</b> (1h {at.vol_h1_share_24h_pct:.0f}% dr 24h)"
            if at.is_sleeper_revival
            else ""
        ),
        f"• Pareto: Top1 <code>{sec.top1_pct}%</code> · Top10 <code>{sec.top10_pct}%</code> · Dev <code>{sec.dev_pct}%</code>",
        f"• Jupiter: 0.1S <code>-{ex.round_trip_loss_01_sol_pct}%</code> | 1.0S <code>-{ex.depth_loss_1_sol_pct}%</code>",
    ]
    if plan:
        lines.extend(
            [
                "",
                "<b>Rencana Disiplin Modal (Real Money):</b>",
                f"• Batas Atas Entry: <b>&lt;= ${plan.max_safe_entry_mc:,.0f}</b> (jangan kejar di atas ini)",
                f"• Hard Stop-Loss: <b>${plan.hard_stop_mc:,.0f} (-{plan.hard_stop_pct:.0f}%)</b>",
                f"• TP1 (Jual 50% + BE): <b>${plan.tp1_mc:,.0f} (+{plan.tp1_pct:.0f}%)</b> [R:R {plan.risk_reward_to_tp1}x]",
                f"• TP2 (Jual 30% + Trail): <b>${plan.tp2_mc:,.0f} (+{plan.tp2_pct:.0f}%)</b> [R:R {plan.risk_reward_to_tp2}x]",
            ]
        )
    lines.append(
        f'\n<a href="https://jup.ag/swap/{SOL_MINT}-{p.mint}">Jupiter</a> · '
        f'<a href="https://dexscreener.com/solana/{p.mint}">DexScreener</a> · '
        f'<a href="https://rugcheck.xyz/tokens/{p.mint}">RugCheck</a>'
    )
    return "\n".join(lines)


def format_exit_alert(pos: TrackedPosition) -> str:
    icon = "🚨" if pos.signal in {"HARD_STOP_LOSS", "LP_PULL_EMERGENCY"} else (
        "💰" if pos.signal in {"TP1_DERISK_BREAKEVEN", "TP2_TAKE_PROFIT"} else "⚠️"
    )
    return "\n".join(
        [
            f"{icon} <b>EXIT GUARD — {_esc(pos.signal)} (${_esc(pos.symbol)})</b>",
            f"Mint: <code>{_esc(pos.mint)}</code>",
            f"• Entry MC: <b>${pos.entry_mc:,.0f}</b> → Live MC: <b>${pos.live_mc:,.0f}</b> (<b>{pos.pnl_pct:+.1f}%</b>)",
            f"• Peak MC: <b>${pos.peak_mc:,.0f}</b> | LP Retention (AMM): <b>{pos.real_lp_retention_pct if pos.real_lp_retention_pct is not None else '—'}%</b>",
            f"• Detail: {_esc(pos.signal_detail)}",
            f'\n<a href="https://jup.ag/swap/{pos.mint}-{SOL_MINT}">Jual Cepat di Jupiter</a>',
        ]
    )


class TelegramNotifier:
    def __init__(self, cfg: BotConfig) -> None:
        self.token = cfg.telegram_bot_token
        self.chat_id = cfg.telegram_chat_id
        self.enabled = bool(self.token and self.chat_id)
        self._last_update_id = 0

    async def send_message(
        self, client: httpx.AsyncClient, text: str, chat_id: Optional[str] = None
    ) -> bool:
        target = chat_id or self.chat_id
        if not self.token or not target:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = await client.post(url, json=payload, timeout=10.0)
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("Gagal kirim pesan Telegram: %s", exc)
            return False

    async def poll_commands(self, client: httpx.AsyncClient) -> list[tuple[str, str]]:
        """Ambil perintah baru hanya dari chat_id pemilik yang terotorisasi."""
        if not self.enabled:
            return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"timeout": 1, "offset": self._last_update_id + 1}
        try:
            resp = await client.get(url, params=params, timeout=6.0)
            if resp.status_code != 200:
                return []
            data = resp.json()
            commands: list[tuple[str, str]] = []
            for upd in data.get("result") or []:
                self._last_update_id = max(self._last_update_id, int(upd.get("update_id", 0)))
                msg = upd.get("message") or {}
                cid = str((msg.get("chat") or {}).get("id") or "")
                text = str(msg.get("text") or "").strip()
                # Keamanan: hanya eksekusi perintah dari TELEGRAM_CHAT_ID pemilik
                if cid == self.chat_id and text.startswith("/"):
                    commands.append((cid, text))
            return commands
        except Exception:
            return []


async def handle_telegram_command(
    client: httpx.AsyncClient,
    cfg: BotConfig,
    store: object,
    tg: TelegramNotifier,
    chat_id: str,
    text: str,
) -> None:
    """Handler perintah bersama untuk Vercel Webhook (/api/webhook) maupun VPS (main.py)."""
    from .config import is_valid_solana_mint
    from .feeds import fetch_pair_for_mint
    from .pipeline import inspect_mint, run_scan_cycle

    parts = text.strip().split()
    if not parts:
        return
    # Strip @BotUsername suffix if invoked in group/menu (e.g. /scan@MyBot -> /scan)
    cmd = parts[0].split("@")[0].lower()

    if cmd in {"/start", "/help"}:
        await tg.send_message(
            client,
            "\n".join(
                [
                    "<b>Corridor Desk v2 — Smart Money Radar (5-Layer + Pre-ATH Revival)</b>",
                    f"Runtime: <code>{'Vercel Serverless' if cfg.is_vercel else 'VPS Worker'}</code> | Storage: <code>{getattr(store, 'mode', 'sqlite')}</code>",
                    "",
                    "<b>Daftar Perintah:</b>",
                    "• <code>/scan</code> — Pindai 8 feed Solana sekarang (Fresh + Sleeper Revival)",
                    "• <code>/inspect &lt;MINT&gt;</code> — Audit penuh 4 layer sebelum entry manual",
                    "• <code>/track &lt;MINT&gt; &lt;ENTRY_MC&gt; &lt;MODAL_USD&gt;</code> — Masukkan ke Exit Guard",
                    "• <code>/positions</code> — Lihat PnL &amp; AMM LP Retention posisi aktif",
                    "• <code>/close &lt;MINT&gt;</code> — Hapus posisi dari Exit Guard",
                    "• <code>/status</code> — Cek konfigurasi bot &amp; penyimpanan",
                ]
            ),
            chat_id,
        )
        return

    if cmd == "/status":
        positions = await store.list_positions()  # type: ignore[attr-defined]
        await tg.send_message(
            client,
            "\n".join(
                [
                    "<b>Status Bot Corridor Desk v2</b>",
                    f"• Lingkungan: <code>{'Vercel Serverless' if cfg.is_vercel else 'VPS'}</code>",
                    f"• Penyimpanan: <code>{getattr(store, 'mode', 'sqlite')}</code>",
                    f"• Posisi Aktif Exit Guard: <b>{len(positions)}</b>",
                    f"• Ambang Skor Minimal: <b>{cfg.min_score}/100</b> | Delay: <b>{cfg.reaction_delay_sec}s</b>",
                    f"• Koridor C ($220K–$335K): <b>{'Aktif' if cfg.include_midcap_corridor else 'Nonaktif'}</b>",
                ]
            ),
            chat_id,
        )
        return

    if cmd == "/scan":
        await tg.send_message(client, "Memindai 8 feed Solana (Fresh + Sleeper Revival)...", chat_id)
        results, _ = await run_scan_cycle(client, cfg)
        ready = [r for r in results if r.verdict == "ENTRY_READY"]
        watch = [r for r in results if r.verdict == "WATCH_PULLBACK"]
        if not ready:
            extra = ""
            if watch:
                top_w = watch[0]
                extra = f"\nKandidat Watch teratas: <b>${_esc(top_w.pair.symbol)}</b> ({top_w.total_score}/100) — {_esc(top_w.headline)}"
            await tg.send_message(
                client,
                f"Scan selesai ({len(results)} diaudit | {len(watch)} Watch Pullback). Belum ada token yang lolos 4 layer keras.{extra}",
                chat_id,
            )
        else:
            for audit in ready[:3]:
                await tg.send_message(client, format_entry_alert(audit), chat_id)
        return

    if cmd == "/inspect" and len(parts) >= 2:
        mint = parts[1].strip()
        if not is_valid_solana_mint(mint):
            await tg.send_message(client, "Format mint Base58 tidak valid (32–44 karakter).", chat_id)
            return
        audit = await inspect_mint(client, mint, cfg)
        if not audit:
            await tg.send_message(client, "Pair Solana tidak ditemukan di DexScreener.", chat_id)
            return
        summary = (
            format_entry_alert(audit)
            if audit.verdict == "ENTRY_READY"
            else f"<b>{_esc(audit.verdict)}</b> — ${_esc(audit.pair.symbol)} ({audit.total_score}/100)\n{_esc(audit.headline)}"
        )
        await tg.send_message(client, summary, chat_id)
        return

    if cmd == "/track" and len(parts) >= 4:
        mint = parts[1].strip()
        if not is_valid_solana_mint(mint):
            await tg.send_message(client, "Format mint Base58 tidak valid.", chat_id)
            return
        try:
            entry_mc = float(parts[2])
            pos_usd = float(parts[3])
        except ValueError:
            await tg.send_message(client, "Format: <code>/track &lt;MINT&gt; &lt;ENTRY_MC&gt; &lt;MODAL_USD&gt;</code>", chat_id)
            return
        pair = await fetch_pair_for_mint(client, mint, ("manual_track",))
        pos = await store.add_position(  # type: ignore[attr-defined]
            mint=mint,
            symbol=pair.symbol if pair else "TOKEN",
            name=pair.name if pair else "Tracked Token",
            entry_mc=entry_mc,
            entry_liq_usd=pair.liquidity_usd if pair else 0.0,
            position_usd=pos_usd,
            entry_price_usd=pair.price_usd if pair else 0.0,
            pair_address=pair.pair_address if pair else "",
        )
        await tg.send_message(
            client,
            f"Tersimpan di Exit Guard (<code>{getattr(store, 'mode', 'sqlite')}</code>): "
            f"<b>${_esc(pos.symbol)}</b> | Entry ${pos.entry_mc:,.0f} | SL -{pos.hard_stop_pct:.0f}%",
            chat_id,
        )
        return

    if cmd == "/positions":
        positions = await store.list_positions()  # type: ignore[attr-defined]
        if not positions:
            await tg.send_message(client, "Belum ada posisi aktif di Exit Guard.", chat_id)
        else:
            lines = ["<b>Posisi Aktif Exit Guard:</b>"]
            for p in positions:
                lp_txt = f"{p.real_lp_retention_pct:.1f}%" if p.real_lp_retention_pct is not None else "—"
                lines.append(
                    f"• <b>${_esc(p.symbol)}</b> (<code>{_esc(p.mint[:6])}…</code>): "
                    f"<b>{p.pnl_pct:+.1f}%</b> | LP Ret {lp_txt} | {_esc(p.signal)}"
                )
            await tg.send_message(client, "\n".join(lines), chat_id)
        return

    if cmd == "/close" and len(parts) >= 2:
        ok = await store.remove_position_by_mint_or_id(parts[1])  # type: ignore[attr-defined]
        await tg.send_message(
            client,
            "Posisi ditutup dari Exit Guard." if ok else "Posisi tidak ditemukan.",
            chat_id,
        )

