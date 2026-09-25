# 🎯 Divar Real-time Deal Hunter & Telegram Alert Bot

An automated Python service that watches [Divar](https://divar.ir) listings (e.g. phones and
electronics in Tehran), detects **underpriced deals the moment they appear**, and pushes a clean
Persian alert to your private Telegram chat or channel — with a one-tap button to open the ad.

```
🔥 شکار معامله در دیوار

📌 عنوان آگهی: آیفون ۱۳ پرومکس ۲۵۶ گیگ سالم
💰 قیمت اعلامی: ۴۵ میلیون تومان
📍 محل / محله: تهران، سعادت‌آباد
🕒 زمان انتشار: ۱۴۰۵/۰۷/۰۳ - ۱۹:۵۳ • لحظاتی پیش
🏷 دسته‌بندی: موبایل
📉 اختلاف با سقف: ۵ میلیون تومان (۱۰٪ ارزان‌تر از سقف)

✅ چرا معامله است:
  • 💰 ۴۵ میلیون تومان — ۱۰٪ زیر سقف ۵۰ میلیون تومان
  • 💬 چت دیوار فعال است

🔗 لینک مستقیم: https://divar.ir/v/aDeaL001
        ┌──────────────────────────┐
        │  مشاهده آگهی در دیوار   │   ← inline keyboard button
        └──────────────────────────┘
```

---

## Contents

- [How it works](#how-it-works)
- [Quickstart](#quickstart)
- [Try it without a bot token](#try-it-without-a-bot-token)
- [Telegram setup](#telegram-setup)
- [Divar setup: cities, categories, proxies](#divar-setup-cities-categories-proxies)
- [Deal detection rules](#deal-detection-rules)
- [Configuration reference](#configuration-reference)
- [CLI reference](#cli-reference)
- [Running it in production](#running-it-in-production)
- [Operations: state, logs, stats](#operations-state-logs-stats)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Disclaimer](#disclaimer)

---

## How it works

```
                    ┌───────────────────────────────────────────────────┐
                    │                     main.py                       │
                    │   asyncio loop • signals • logging • cycle stats   │
                    └───────┬───────────────┬───────────────┬───────────┘
                            │               │               │
              ┌─────────────▼──┐   ┌────────▼────────┐  ┌───▼──────────────┐
   every      │ divar_client.py│   │   storage.py    │  │   notifier.py    │
   N seconds  │  UA rotation   │   │ SQLite tokens   │  │  Bot API + proxy │
   ─────────► │  jitter 4–9s   │   │  run_log, prune │  │  retries, retry_ │
              │  backoff 429/5x│   └────────┬────────┘  │  after, MarkdownV2│
              │  endpoint fail-│            │           │  → plain fallback │
              │  over          │   ┌────────▼────────┐  └───▲──────────────┘
              └────────┬───────┘   │   filters.py    │      │
                       │           │ Persian price   │──────┘
                       └──────────►│ parsing, down-  │   alerts (best deals first)
                       new posts   │ payment logic,  │
                                   │ thresholds      │
                                   └─────────────────┘
```

Each cycle: **fetch** → **dedupe** (SQLite `seen_tokens`) → **resolve the real price** →
**apply deal rules** → **alert** (budgeted) → **persist state**. Every step is failure-tolerant:
a Divar outage backs the loop off instead of crashing it, a Telegram hiccup queues the alert for
retry on a later cycle, and nothing is ever alerted twice.

| Module | Responsibility |
| --- | --- |
| `config.py` | Loads `.env` / environment into a validated, immutable `Settings` object. No secrets in code. |
| `divar_client.py` | Talks to Divar's JSON endpoints with browser-like headers, rotating User-Agents, jittered delays, exponential backoff and a legacy-endpoint fallback. Normalizes widget payloads into `DivarPost`. |
| `persian.py` | Persian digits/separators, price text → Toman, down-payment detection, Jalali calendar, relative time. |
| `filters.py` | Deal detection: recovers a trustworthy price, then applies floor/ceiling/keyword/age/seller rules. Every accept and reject is explained. |
| `storage.py` | SQLite `seen_tokens` (+ `run_log`, `meta`), thread-safe, prunable. |
| `notifier.py` | Telegram Bot API sender: proxy support, retries, `retry_after` handling, MarkdownV2 with automatic plain-text fallback, inline keyboard. |
| `main.py` | asyncio monitoring loop, CLI, graceful shutdown, logging. |

---

## Quickstart

Requires **Python 3.9+**.

```bash
git clone https://github.com/fa833618/divar-hunter.git
cd divar-hunter

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env            # at minimum: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CITY, CATEGORY, DEAL_MAX_PRICE
```

Then:

```bash
python main.py --check   # validates config, the bot token, the chat, and Divar reachability
python main.py --once    # a single cycle, so you can see what happens
python main.py           # monitor forever (Ctrl-C exits cleanly)
```

A sensible first `.env` for phone deals in Tehran:

```dotenv
TELEGRAM_BOT_TOKEN=1234567890:AA...
TELEGRAM_CHAT_ID=123456789
CITY=tehran
CATEGORY=mobile-phones
DEAL_MAX_PRICE=50000000     # alert at or below 50 million Toman
DEAL_PRICE_FLOOR=5000000    # ignore anything cheaper (scams, deposits, accessories)
CHECK_INTERVAL_SECONDS=60
EXCLUDE_KEYWORDS=خراب,اوراق,قطعات,شکسته,معیوب,اقساطی
```

> **First run is silent by design.** `BOOTSTRAP_SILENT=true` marks everything currently listed as
> "seen" without alerting, so you don't get 24 messages the moment you start. Only *new* listings
> are alerted from then on.

---

## Try it without a bot token

Two offline commands exercise the whole pipeline against a bundled fixture — no network, no
credentials, nothing written to disk:

```bash
python main.py --demo        # full cycle: fetch → dedupe → filter → render alerts
python main.py --test-alert  # renders one sample alert (uses DRY_RUN if no token is set)
```

`--demo` deliberately applies the sample rules from `.env.example`, so you can watch the filter
reject listings for the right reasons:

```
rejected 8 post(s): above_threshold×1, no_price:negotiable×1, not_total_price:down payment×1,
                    below_floor×1, no_price:free×1, excluded_keyword×1, no_price:exchange×1,
                    not_total_price:deposit×1
deal(s) found: hStale08 @ ۱۸ میلیون تومان; kMiDec11 @ ۳۲ میلیون تومان (قیمت کل، از متن آگهی);
               aDeaL001 @ ۴۵ میلیون تومان
```

Set `DRY_RUN=true` in `.env` to run the real loop against live Divar while logging alerts instead
of sending them — the safest way to tune your thresholds.

---

## Telegram setup

1. **Create the bot**: talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. **Find your chat id**: message [@userinfobot](https://t.me/userinfobot) and copy the numeric id into
   `TELEGRAM_CHAT_ID`.
3. **Press /start on your bot once.** A bot cannot message a user who never started it — this is the
   #1 cause of `403 Forbidden`. `python main.py --check` tells you exactly this if it's the problem.
4. **Channels**: add the bot as an administrator, then use `@channel_username` or the numeric
   `-100…` id. Multiple targets are supported: `TELEGRAM_CHAT_ID=123456789, @backup_channel`.

**Proxies.** Telegram is blocked in Iran, so the sender supports HTTP and SOCKS5:

```dotenv
TELEGRAM_PROXY=socks5h://127.0.0.1:10808   # "h" = resolve DNS inside the proxy (recommended)
# TELEGRAM_PROXY=http://127.0.0.1:8080
```

SOCKS5 requires `PySocks`, which is already in `requirements.txt`. A shared `PROXY_URL` is used for
both Telegram and Divar unless `TELEGRAM_PROXY` / `DIVAR_PROXY` override it.

---

## Divar setup: cities, categories, proxies

Divar has no public API. This client calls the same private JSON endpoints the divar.ir web app
uses, with the headers a browser sends (`User-Agent` rotation, `Accept`, `Accept-Language: fa-IR`,
`Origin`/`Referer: https://divar.ir`):

| Endpoint | Used for |
| --- | --- |
| `POST /v8/postlist/w/search` | listing search with filters + cursor pagination (primary) |
| `POST /v8/search/{city_id}/{category}` | legacy listing search (automatic fallback if the primary one breaks) |
| `GET /v8/posts-v2/web/{token}` | post detail: description, exact price, brand/model, seller type (optional, `FETCH_DETAILS=true`) |

**Cities.** `CITY` accepts a numeric Divar id, an ASCII slug or the Persian name — `1`, `tehran` and
`تهران` are equivalent. Run `python divar_client.py` to print the built-in map (Tehran = 1,
Karaj = 2, Mashhad = 3, Isfahan = 4, …). A city outside the map needs an explicit `CITY_ID`.

**Categories.** `CATEGORY` takes a Divar slug (`mobile-phones`, `tablet`, `electronic-devices`,
`audio-video`, `tv-projector`, `camera-camcoders`, `game-consoles-and-video-games`, `cars`,
`real-estate`, …) or a friendly alias — `python divar_client.py --list-categories` prints them, and
unknown slugs are passed through untouched, so new Divar categories work without a code change.
Leave `CATEGORY` empty to watch everything in the city.

**Proxies — important.** Divar's listing gateway generally **only answers requests coming from
inside Iran**. From abroad you will see 403/timeout errors until you route through an Iranian exit:

```dotenv
DIVAR_PROXY=socks5h://127.0.0.1:10808
```

**Politeness is built in**, and it is deliberately conservative:

- a jittered delay of `REQUEST_DELAY_MIN`–`REQUEST_DELAY_MAX` seconds (default **4–9s**) between
  every request, not just between cycles;
- a rotating pool of 8 realistic browser profiles (UA + matching `sec-ch-ua` hints);
- exponential backoff with jitter on `429/5xx`/timeouts, honouring the server's `Retry-After`;
- non-retryable `4xx` fails fast with a readable message instead of hammering the endpoint;
- one page per cycle by default (`PAGES_PER_CYCLE=1`, 24 rows).

Keep `CHECK_INTERVAL_SECONDS` at 60 or more. Deals stay listed for minutes; being 3× more
aggressive mostly buys you an IP block.

---

## Deal detection rules

The hard part of hunting deals on Divar is knowing **what the number means**. Rules are applied in
this order, and every decision is recorded (`--stats`, debug logs, and the alert itself):

1. **Freshness** — posts older than `MAX_AGE_MINUTES` are dropped, so a restart after downtime does
   not replay stale listings.
2. **Price resolution** — the structured price is preferred; otherwise the Persian price text is
   parsed (`۸۵٬۰۰۰٬۰۰۰ تومان`, `۴۵۰ میلیون`, `۲ میلیارد و ۳۰۰ میلیون`, `۱٫۵ میلیارد`,
   `۴۵۰ هزار`); otherwise a keyword-anchored amount is read from the description
   (`قیمت: ۴۵٬۰۰۰٬۰۰۰ تومان`). Bare numbers in text are *never* trusted as prices — that is how
   you end up "buying" an آیفون **۱۳** for 13 Toman.
3. **Down payments / installments / deposits** — `پیش‌پرداخت`, `اقساطی`, `چک صیادی`, `ودیعه`,
   `رهن کامل` mean the displayed number is **not** the total. The filter first tries to recover the
   real total from the text (`قیمت کل: …`, or `مابقی …` + the down payment); if it can't, the post is
   skipped (`SKIP_DOWN_PAYMENT_ADS=true`).
4. **Non-prices** — `توافقی`, `رایگان`, `معاوضه` are rejected when `REQUIRE_PRICE=true`.
5. **Floor** — below `DEAL_PRICE_FLOOR` means scam, deposit-only or a mis-categorised accessory.
6. **Ceiling** — at or below `DEAL_MAX_PRICE` is the deal. The discount percentage becomes the
   alert's score and the sort order (biggest discount first, newest first on a tie).
7. **Keywords** — `EXCLUDE_KEYWORDS` rejects (`خراب`, `اوراق`, `قطعات`, `اجاره`, …);
   `INCLUDE_KEYWORDS`, when set, requires a match in title **or description**. Matching is folded:
   Persian/Arabic digits, `ی/ي`, `ک/ك`, `آ/ا` and half-spaces are normalized, so `ایفون 13` matches
   `آیفون ۱۳`.
8. **Seller & media** — `ONLY_PERSONAL_ADS=true` drops dealers (needs `FETCH_DETAILS=true` to know),
   `HAS_PHOTO_ONLY=true` drops photo-less ads.

Publish times come from Divar's own API timestamps when available (exact) and fall back to parsing
relative text (`لحظاتی پیش`, `۲ ساعت پیش`, `دیروز`) — approximate times are labelled as such in the
alert. All displayed dates are converted to the **Jalali** calendar in Asia/Tehran time, computed
with a built-in converter (no extra dependency).

---

## Configuration reference

`.env.example` is the complete, commented list — this is the short version.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Bot token (required unless `DRY_RUN=true`) |
| `TELEGRAM_CHAT_ID` | — | Chat id, `@channel`, or comma-separated list |
| `TELEGRAM_PROXY` / `DIVAR_PROXY` / `PROXY_URL` | — | `http://…` or `socks5h://…` |
| `CITY` / `CITY_ID` | `tehran` | id, slug or Persian name |
| `CATEGORY` | `mobile-phones` | Divar slug or alias; empty = everything |
| `SEARCH_QUERY` | — | Free text, e.g. `آیفون ۱۳` |
| `PRICE_MIN` / `PRICE_MAX` | — | Server-side price window (Toman) |
| `DEAL_MAX_PRICE` | `50000000` | **Alert at or below this price** |
| `DEAL_PRICE_FLOOR` | `1000000` | Ignore absurdly cheap listings |
| `REQUIRE_PRICE` | `true` | Reject negotiable/free/exchange ads |
| `SKIP_DOWN_PAYMENT_ADS` | `true` | Reject پیش‌پرداخت / اقساطی / ودیعه |
| `INCLUDE_KEYWORDS` / `EXCLUDE_KEYWORDS` | — | Comma-separated keyword rules |
| `MAX_AGE_MINUTES` | `180` | Drop older posts (0 disables) |
| `FETCH_DETAILS` | `false` | Read each candidate's full post (slower, sharper) |
| `MAX_DETAIL_LOOKUPS` | `10` | Detail-page budget per cycle |
| `CHECK_INTERVAL_SECONDS` | `60` | Seconds between cycles |
| `REQUEST_DELAY_MIN` / `MAX` | `4` / `9` | Jitter between HTTP requests |
| `MAX_RETRIES` / `BACKOFF_BASE` / `BACKOFF_MAX` | `5` / `1.5` / `60` | Retry policy |
| `DIVAR_ENDPOINT_MODE` | `auto` | `auto` \| `modern` \| `legacy` |
| `DB_PATH` | `data/seen_tokens.db` | SQLite state file (`:memory:` allowed) |
| `PRUNE_DAYS` | `14` | Forget tokens older than this |
| `BOOTSTRAP_SILENT` | `true` | Don't alert on the very first run |
| `MAX_ALERTS_PER_CYCLE` | `20` | Alert budget; the rest go into a summary message |
| `MAX_ALERT_ATTEMPTS` | `3` | Retries for a failed alert before dropping it |
| `ALERT_SPACING_SECONDS` | `1.5` | Gap between Telegram messages |
| `LOG_LEVEL` / `LOG_FILE` | `INFO` / stdout only | Logging; `LOG_FILE` (set in `.env.example`) adds a handler rotating at 5 MB × 3 |
| `DRY_RUN` | `false` | Log alerts instead of sending |

Configuration is validated at startup and the errors are actionable — contradictory rules
(`PRICE_MIN` above `DEAL_MAX_PRICE`, an unknown `DIVAR_ENDPOINT_MODE`, a bad proxy scheme, an
unmapped city) stop the process with a list of problems instead of failing silently at 3 a.m.

---

## CLI reference

```
python main.py [options] [command]

options
  --env PATH          .env location (default .env)
  --once              run a single cycle and exit
  --dry-run           log alerts instead of sending them
  --details           fetch full post details for candidates
  --city / --category / --query / --max-price / --interval
                      override the matching .env value for this run
  -v, --verbose       debug logging
  --log-level / --log-file

commands
  --check             validate config + bot token + chat + Divar reachability
  --test-alert        send (or render) one sample alert
  --demo              full offline cycle from the bundled fixture
  --stats [--json]    seen-token store + last 10 cycles
```

`python divar_client.py` lists known cities, `--list-categories` lists category aliases, and
`--probe` fetches a single live page and prints a summary.

---

## Running it in production

**systemd** (see `deploy/divar-hunter.service`):

```bash
sudo cp deploy/divar-hunter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now divar-hunter
journalctl -u divar-hunter -f
```

The service handles `SIGTERM`/`SIGINT` gracefully: it finishes the in-flight cycle, flushes state to
SQLite and exits (backoff sleeps are interrupted within ~0.5s).

**Docker**:

```bash
docker build -t divar-hunter .
docker run -d --name divar-hunter --restart unless-stopped \
  --env-file .env \
  -v $PWD/data:/app/data -v $PWD/logs:/app/logs \
  divar-hunter

# or
docker compose up -d
```

State (`data/seen_tokens.db`) and logs live on named volumes, so a container restart never re-alerts
old listings. If you run the proxy on the host, use `host.docker.internal` (add
`--add-host=host.docker.internal:host-gateway` on Linux) instead of `127.0.0.1`.

---

## Operations: state, logs, stats

```bash
python main.py --stats
```

```
Seen-token store
  db_path          data/seen_tokens.db
  tokens           1284
  notified         37
  deals            52
  runs             96
  oldest           2026-09-20T08:11:02+00:00
  newest           2026-09-25T13:40:11+00:00
  bootstrapped_at  2026-09-20T08:11:02+00:00

Last runs (newest first)
  2026-09-25T13:40:09+00:00  fetched=24 fresh=3 deals=1 sent=1 failed=0  24 fetched, 3 new, 1 deal(s), 1 alert(s) sent
```

- `seen_tokens` — one row per listing ever seen, with `notified` / `is_deal` flags. Delete the file
  to start over (the next run bootstraps silently again).
- `run_log` — one row per cycle; the fastest way to answer "did it run while I was asleep?".
- Logs go to stdout **and** `LOG_FILE` (5 MB × 3 rotated). Tokens and proxies are redacted wherever
  settings are logged (`Settings.masked()`).

---

## Testing

The suite is fully offline — no network, no bot token, no Divar access needed:

```bash
pip install -r requirements-dev.txt
pytest -q            # 244 tests
```

Coverage includes Persian price parsing (millions/billions/compound amounts, deposits,
down payments), Jalali conversion (round-trip over 40 years + historical anchors), row
normalization from real-shaped payloads, request-body shape, User-Agent rotation, jitter windows,
`Retry-After` and exponential backoff, endpoint fallback, MarkdownV2 escaping with the plain-text
fallback path, SQLite dedupe/prune/thread-safety, and the monitoring loop end-to-end (bootstrap
silence, dedupe, alert budget, retry queue, graceful shutdown).

CI runs it on every push: `.github/workflows/tests.yml`.

---

## Troubleshooting

| Symptom | Cause & fix |
| --- | --- |
| `Divar rejected the request: HTTP 403` / timeouts | Traffic is leaving outside Iran. Set `DIVAR_PROXY` (or `PROXY_URL`) to an Iranian SOCKS5/HTTP proxy. |
| `HTTP 429` in the logs | You're being rate-limited. Raise `REQUEST_DELAY_MIN/MAX` and `CHECK_INTERVAL_SECONDS`. Backoff is automatic; the loop keeps running. |
| `no widgets` / `no post_list` | Divar changed the payload. The client already falls back to the legacy endpoint (`DIVAR_ENDPOINT_MODE=auto`); if both fail, open `--verbose` and check the raw shape. |
| `Telegram 401 Unauthorized` | Wrong/expired `TELEGRAM_BOT_TOKEN`. |
| `Telegram 403 Forbidden` | You never pressed `/start` on the bot, or the bot isn't an admin of the channel. |
| `chat not found` from `--check` | Same as above; `--check` prints the `/start` hint. |
| Alerts arrive without formatting | Telegram rejected the MarkdownV2 payload; the bot automatically re-sent it as plain text (`plain_fallback` counter). Nothing was lost. |
| `socks5h` proxy errors | `pip install PySocks` (already in `requirements.txt`). |
| Too many alerts at first start | Set `BOOTSTRAP_SILENT=true` (default) and `MAX_ALERTS_PER_CYCLE`. |
| No alerts at all | Run `python main.py --once --dry-run -v` and read the `rejected N post(s): …` line — it names the exact rule for every skipped listing. |

---

## Project layout

```
.
├── main.py                 # asyncio loop, CLI, graceful shutdown
├── config.py               # .env → validated Settings
├── divar_client.py         # Divar endpoints, headers, jitter, backoff, normalization
├── filters.py              # deal detection + price resolution
├── notifier.py             # Telegram sender + Persian alert formatting
├── persian.py              # digits, prices, relative time, Jalali calendar
├── storage.py              # SQLite seen_tokens / run_log
├── requirements.txt        # requests, python-dotenv, PySocks
├── requirements-dev.txt    # + pytest
├── .env.example            # every knob, documented
├── Dockerfile, docker-compose.yml
├── deploy/divar-hunter.service
├── .github/workflows/tests.yml
└── tests/                  # 244 offline tests + realistic API fixtures
```

---

## Disclaimer

This project reads publicly listed advertisements through the same endpoints the divar.ir web app
uses. It is intended for **personal use**: keep the polling interval polite (≥ 60s), keep the
jittered request delay enabled, and don't resell or republish the data. Scraping may violate
Divar's terms of service, endpoints and payload shapes can change without notice, and prices or
availability in listings are the sellers' responsibility — always verify an ad before paying
anyone. Use at your own risk.
