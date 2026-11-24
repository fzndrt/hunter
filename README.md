# Smart Money FOMO Radar v2 — Clean 5-Layer Architecture (Vercel + VPS Ready)

Arsitektur modular pengganti monolit `api/fomo.py` (17.011 baris) untuk trading Solana **real money** dengan fokus menangkap koin di awal gelombang (**Early-Entry Corridor** + **Pre-ATH Sleeper Revival**) sekaligus memblokir rugpull, honeypot, Nano-Swarm, dan FOMO di pucuk.

Mendukung penuh alur **GitHub → Vercel Serverless → Telegram Bot** (termasuk persistensi **Upstash Redis / Vercel KV** agar anti lupa posisi/alert) sekaligus tetap bisa dijalankan 24/7 di VPS melalui `main.py`.

## Struktur Repository (24 File)

```text
.
├── vercel.json                         # Konfigurasi Vercel Python Serverless (aman untuk Hobby Free)
├── requirements.txt                    # Dependensi minimal (httpx, websockets, python-dotenv, pytest)
├── .env.example                        # Template Environment Variables (Vercel & VPS)
├── .gitignore                          # Melindungi .env & file database lokal
├── .github/
│   └── workflows/
│       └── radar-cron.yml              # Scheduler GRATIS tiap 5 menit via GitHub Actions -> Vercel /api/scan
├── api/
│   ├── index.py                        # GET / -> Status kesiapan Environment & storage Vercel
│   ├── scan.py                         # GET/POST /api/scan -> Radar 8-feed + Alert Telegram + Exit Guard
│   └── webhook.py                      # POST /api/webhook -> Perintah Telegram instan (/scan, /inspect, dll)
├── main.py                             # Entrypoint worker 24/7 opsional jika kelak pindah ke VPS
├── bot/
│   ├── __init__.py
│   ├── config.py                       # Allowlist Hard-Quote Mint, 3 Koridor, & deteksi env Vercel
│   ├── models.py                       # Dataclass terstruktur seluruh 5 layer
│   ├── security.py                     # Layer 1: Veto keamanan biner (RugCheck, Token-2022, Pareto, LP)
│   ├── anti_top.py                     # Layer 2: Koridor, Red Zone, Delay 22s, Pre-ATH & Sleeper Revival
│   ├── execution.py                    # Layer 3: Jupiter 0.1 SOL + 1.0 SOL fail-closed round-trip
│   ├── scoring.py                      # Layer 4: Additive Score 0–100 (5 pilar) & Trade Plan R:R
│   ├── exit_guard.py                   # Layer 5: Evaluasi Hard SL, Breakeven, Trailing & AMM LP √(MC/MC0)
│   ├── storage.py                      # Persistensi otomatis: Upstash Redis Cloud (Vercel) / SQLite WAL (VPS)
│   ├── feeds.py                        # 8-Feed discovery (Fresh + Sleeper Revival 1h & Top Boosts)
│   ├── pipeline.py                     # Orkestrator audit paralel Layer 1–4 (teroptimasi latency Vercel)
│   ├── pumpportal_stream.py            # WebSocket PumpPortal (khusus VPS)
│   └── telegram_notifier.py            # Formatter alert HTML aman & command handler bersama
└── tests/
    └── test_engine.py                  # Unit test aturan anti-rugi, Sleeper Revival, AMM & Vercel auth
```

---

## Panduan Deploy Lengkap: GitHub → Vercel → Telegram Bot

### Langkah 1: Push 24 File ke GitHub
Upload seluruh isi repo ini ke root repository GitHub Anda (`main` branch). Pastikan file `.env` **tidak pernah** ikut ter-upload.

### Langkah 2: Import ke Vercel & Isi Environment Variables
Di dashboard **Vercel → Project → Settings → Environment Variables**, tambahkan variabel berikut:

| Nama Variabel | Wajib? | Fungsi |
| :--- | :---: | :--- |
| `TELEGRAM_BOT_TOKEN` | **WAJIB** | Token bot dari `@BotFather` |
| `TELEGRAM_CHAT_ID` | **WAJIB** | Chat ID Telegram Anda (cek lewat `@userinfobot`) |
| `CRON_SECRET` | **WAJIB** | Password acak buatan Anda sendiri (misal 32 karakter) untuk mengunci `/api/scan` |
| `TELEGRAM_WEBHOOK_SECRET` | **Disarankan** | Password acak untuk memverifikasi request asli dari server Telegram |
| `UPSTASH_REDIS_REST_URL` | **Sangat Disarankan** | URL REST dari Upstash Redis gratis (atau Vercel Marketplace → Upstash Redis) |
| `UPSTASH_REDIS_REST_TOKEN` | **Sangat Disarankan** | Token REST dari Upstash Redis agar cooldown alert & Exit Guard tidak hilang saat cold start |
| `MIN_SCORE` | Opsional | Default `72` (ambang skor minimal 0–100 untuk kirim alert) |

Setelah klik **Deploy**, buka `https://<nama-project-anda>.vercel.app/` di browser. Pastikan keluar JSON `"ready": true`.

### Langkah 3: Aktifkan Jadwal Scan Otomatis (Masuk Telegram Otomatis)
Karena **Vercel Hobby (Gratis)** membatasi Vercel Cron bawaan maksimal 1x/hari, gunakan salah satu dari 2 cara gratis ini agar radar berjalan setiap **1–5 menit**:

#### Opsi A — Langsung Pakai GitHub Actions (Sudah Termasuk di Repo, Tiap 5 Menit Gratis)
1. Buka repo GitHub Anda → **Settings → Secrets and variables → Actions → New repository secret**.
2. Tambahkan 2 secret:
   - `VERCEL_APP_URL` = `https://<nama-project-anda>.vercel.app`
   - `CRON_SECRET` = *(harus sama persis dengan `CRON_SECRET` di Vercel)*
3. Buka tab **Actions** → pilih **Corridor Desk Radar Cron (Vercel Trigger)** → klik **Enable workflow** (dan bisa klik **Run workflow** untuk tes langsung).

#### Opsi B — Lebih Cepat Tiap 1 Menit Gratis via `cron-job.org`
1. Daftar gratis di `cron-job.org` → **Create cronjob**.
2. **URL**: `https://<nama-project-anda>.vercel.app/api/scan`
3. **Schedule**: Every `1` or `2` minutes.
4. Di tab **Advanced → Headers**, tambahkan header:
   - Key: `Authorization`
   - Value: `Bearer <CRON_SECRET_ANDA>`
5. Simpan dan aktifkan. Setiap kali ada koin lolos 4 layer keras + Pre-ATH, alert otomatis masuk ke Telegram Anda!

### Langkah 4: Hubungkan Perintah Telegram (`/scan`, `/inspect`, `/track`, `/positions`)
Jalankan perintah berikut sekali saja di browser / terminal agar bot Telegram langsung membalas perintah Anda lewat Vercel Webhook:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://<nama-project-anda>.vercel.app/api/webhook",
    "secret_token": "<TELEGRAM_WEBHOOK_SECRET>"
  }'
```

Setelah sukses, coba kirim `/status` atau `/scan` di chat Telegram bot Anda!

---

## Perintah Telegram
- `/status` — Cek status runtime Vercel, storage (`upstash_redis` / `sqlite`), & jumlah posisi aktif.
- `/scan` — Jalankan pemindaian 8-feed Solana (Fresh + Pre-ATH Sleeper Revival) sekarang juga.
- `/inspect <MINT>` — Audit penuh 4 layer sebelum entry manual.
- `/track <MINT> <ENTRY_MC> <MODAL_USD>` — Masukkan posisi ke Exit Guard (dipantau otomatis tiap siklus `/api/scan`).
- `/positions` — Lihat PnL, Peak MC, dan AMM LP Retention seluruh posisi aktif.
- `/close <MINT>` — Tutup pemantauan posisi setelah Anda jual.
