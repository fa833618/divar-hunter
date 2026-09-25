#!/usr/bin/env python3
"""Divar real-time deal hunter — monitoring loop.

Wires the four pieces together::

    DivarClient  ->  fetch new listings (jittered, retried, browser-like)
    SeenStore    ->  remember tokens so nothing is alerted twice
    DealFilter   ->  resolve the real price and apply the deal rules
    Notifier     ->  send the Markdown alert to Telegram

The loop is asyncio-driven; the blocking HTTP work runs in a worker thread so
Ctrl-C / SIGTERM is answered immediately and the process exits cleanly. Every
cycle is wrapped in error handling: a network outage backs off and retries
instead of killing the service.

Run it::

    python main.py                # monitor forever
    python main.py --once         # a single cycle, then exit
    python main.py --check        # validate config, Telegram and Divar access
    python main.py --demo         # one cycle against a bundled fixture (offline)
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import ConfigError, Settings
from divar_client import DivarClient, DivarError, ShutdownRequested
from filters import CycleStats, DealFilter, Verdict
from notifier import BaseNotifier, build_notifier
from persian import to_persian_digits
from storage import RunStats, SeenStore

LOGGER = logging.getLogger("divar.hunter")
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s"
DEMO_FIXTURE = Path(__file__).with_name("tests") / "fixtures" / "postlist_page.json"


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """Console logging plus an optional size-rotated file."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(file_handler)

    # Third-party noise off.
    for noisy in ("urllib3", "requests", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# hunter
# --------------------------------------------------------------------------- #
class Hunter:
    """One monitoring service: fetch → dedupe → judge → alert."""

    def __init__(
        self,
        settings: Settings,
        client: Optional[DivarClient] = None,
        notifier: Optional[BaseNotifier] = None,
        store: Optional[SeenStore] = None,
        deal_filter: Optional[DealFilter] = None,
    ) -> None:
        self.settings = settings
        self._stop_event = threading.Event()
        should_stop = self._stop_event.is_set

        self.client = client or DivarClient(settings, should_stop=should_stop)
        self.notifier = notifier or build_notifier(settings, should_stop=should_stop)
        self.store = store or SeenStore(settings.db_path, prune_days=settings.prune_days)
        self.deal_filter = deal_filter or DealFilter(settings)

        # Alerts that failed to send are retried on later cycles: (verdict, attempts).
        self._retry_queue: List[Tuple[Verdict, int]] = []
        self._sent_tokens: set = set()
        self.cycles = 0
        self.totals = RunStats()
        self._cycle_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def request_stop(self, reason: str = "signal") -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        LOGGER.info("shutdown requested (%s) — finishing the current cycle", reason)

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    # ------------------------------------------------------------------ #
    async def run(self, once: bool = False) -> int:
        """Run monitoring cycles until stopped. Returns a process exit code."""
        loop = asyncio.get_running_loop()
        self._install_signal_handlers(loop)

        LOGGER.info("Divar deal hunter starting | %s", self.settings.describe())
        LOGGER.info("filters: %s", _filter_summary(self.settings))
        LOGGER.info("state: %s", self.store.stats())
        self.store.prune()

        consecutive_failures = 0
        exit_code = 0
        try:
            while not self.stopping:
                started = time.monotonic()
                try:
                    stats = await asyncio.to_thread(self.run_cycle)
                except ShutdownRequested:
                    LOGGER.info("interrupted mid-cycle by shutdown")
                    break
                except DivarError as exc:
                    consecutive_failures += 1
                    delay = min(self.settings.backoff_max * consecutive_failures, 300.0)
                    if once:  # --once means one attempt, not an endless retry
                        LOGGER.error("cycle %d failed: %s", self.cycles + 1, exc)
                        exit_code = 1
                        break
                    LOGGER.error(
                        "cycle %d failed: %s — next attempt in %.0fs",
                        self.cycles + 1, exc, delay,
                    )
                    if await self._sleep_or_stop(delay):
                        break
                    continue
                except Exception:  # noqa: BLE001 - the loop must survive anything
                    consecutive_failures += 1
                    LOGGER.exception("unexpected error in cycle %d", self.cycles + 1)
                    if once:
                        exit_code = 1
                        break
                    if await self._sleep_or_stop(min(60.0 * consecutive_failures, 300.0)):
                        break
                    continue

                consecutive_failures = 0
                elapsed = time.monotonic() - started
                LOGGER.info(
                    "cycle %d done in %.1fs | %s", self.cycles, elapsed, stats.note or "ok"
                )
                if once:
                    break
                if await self._sleep_or_stop(self.settings.check_interval_seconds):
                    break
        finally:
            # A signal stop (130) wins over a cycle failure (1); never lose a
            # failure code just because we shut down cleanly afterwards.
            exit_code = max(exit_code, self.shutdown())
        return exit_code

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop, sig.name)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
                signal.signal(sig, lambda *_: self.request_stop("signal"))

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep ``seconds`` unless a stop was requested. True when stopping."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.stopping:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.stopping
            await asyncio.sleep(min(0.25, remaining))
        return True

    def shutdown(self) -> int:
        LOGGER.info(
            "shutting down after %d cycle(s) | fetched=%d fresh=%d deals=%d sent=%d failed=%d",
            self.cycles, self.totals.fetched, self.totals.fresh,
            self.totals.deals, self.totals.sent, self.totals.failed,
        )
        for closer in (self.client.close, self.notifier.close, self.store.close):
            try:
                closer()
            except Exception:  # pragma: no cover - shutdown must not raise
                LOGGER.debug("closer failed", exc_info=True)
        return 130 if self.stopping else 0

    # ------------------------------------------------------------------ #
    # one cycle (runs in a worker thread)
    # ------------------------------------------------------------------ #
    def run_cycle(self) -> RunStats:
        """Fetch, dedupe, judge and alert. Blocking — called via ``to_thread``."""
        started_at = datetime.now(timezone.utc)
        stats = RunStats(started_at=started_at)
        notes: List[str] = []

        # 0) retry anything that failed to send last time
        stats.sent += self._flush_retry_queue()

        # 1) fetch
        posts = self.client.fetch_latest()
        stats.fetched = len(posts)
        if not posts:
            notes.append("no posts returned by Divar")

        # 2) dedupe against the local store
        fresh = self.store.unseen(posts)
        stats.fresh = len(fresh)

        # 3) first run: seed the store silently instead of firing 24 alerts
        if not self.store.is_bootstrapped:
            self.store.mark_bootstrapped(started_at)
            if fresh and self.settings.bootstrap_silent:
                self.store.mark_seen(fresh, now=started_at)
                stats.note = (
                    f"bootstrap: {len(fresh)} existing listing(s) marked seen without alerting"
                )
                LOGGER.info("%s", stats.note)
                self._finish(stats, started_at)
                return stats

        # 4) judge
        judgement = CycleStats()
        verdicts = [self.deal_filter.evaluate(post, judgement) for post in fresh]
        if judgement.rejection_reasons:
            LOGGER.info(
                "rejected %d post(s): %s",
                judgement.rejected,
                ", ".join(
                    f"{reason}×{count}"
                    for reason, count in sorted(
                        judgement.rejection_reasons.items(), key=lambda kv: -kv[1]
                    )
                ),
            )
        deals = sorted(
            (v for v in verdicts if v.is_deal),
            key=lambda v: (v.score, -(v.post.age_seconds() or 0.0)),
            reverse=True,
        )
        stats.deals = len(deals)
        if deals:
            LOGGER.info(
                "deal(s) found: %s",
                "; ".join(
                    f"{v.post.token} @ {v.price.display}" for v in deals[:5]
                ),
            )

        # 5) optionally enrich candidates with the detail page, then re-judge
        if deals and self.settings.fetch_details:
            deals = self._enrich_and_rejudge(deals, notes)
            stats.deals = len(deals)

        # 6) alert (budgeted) + summary for the overflow
        cap = max(1, self.settings.max_alerts_per_cycle)
        to_send, held = deals[:cap], deals[cap:]
        for verdict in to_send:
            if self.stopping:
                notes.append("shutdown during alerting")
                break
            if self._deliver(verdict):
                stats.sent += 1
            else:
                stats.failed += 1
        if held and not self.stopping:
            LOGGER.info("holding back %d deal(s) above the per-cycle budget", len(held))
            try:
                self.notifier.summary(held, self.settings)
            except Exception as exc:  # noqa: BLE001 - the summary is best effort
                LOGGER.warning("could not send the overflow summary: %s", exc)

        # 7) persist state
        notified = [v.post.token for v in to_send if v.is_deal and self._was_sent(v)]
        self.store.mark_seen(
            fresh,
            notified_tokens=notified,
            deal_tokens=[v.post.token for v in deals],
            now=started_at,
        )

        if not notes:
            notes.append(
                f"{stats.fetched} fetched, {stats.fresh} new, {stats.deals} deal(s), "
                f"{stats.sent} alert(s) sent"
            )
        return self._finish(stats, started_at, notes)

    def _finish(
        self, stats: RunStats, started_at: datetime, notes: Optional[Sequence[str]] = None
    ) -> RunStats:
        stats.finished_at = datetime.now(timezone.utc)
        if notes:
            stats.note = "; ".join(notes)
        with self._cycle_lock:
            self.cycles += 1
            self.totals.fetched += stats.fetched
            self.totals.fresh += stats.fresh
            self.totals.deals += stats.deals
            self.totals.sent += stats.sent
            self.totals.failed += stats.failed
        try:
            self.store.log_run(stats)
        except Exception as exc:  # noqa: BLE001 - run_log must never break a cycle
            LOGGER.warning("could not write run_log: %s", exc)
        return stats

    # ------------------------------------------------------------------ #
    def _enrich_and_rejudge(self, deals: List[Verdict], notes: List[str]) -> List[Verdict]:
        """Fetch detail pages for the best candidates and judge them again."""
        budget = max(1, self.settings.max_detail_lookups)
        candidates = deals[:budget]
        enriched = self.client.enrich([verdict.post for verdict in candidates], limit=budget)
        notes.append(f"enriched {enriched}/{len(candidates)} candidate(s) with detail pages")
        rejudged = [self.deal_filter.evaluate(verdict.post) for verdict in candidates]
        survivors = [verdict for verdict in rejudged if verdict.is_deal]
        dropped = len(candidates) - len(survivors)
        if dropped:
            notes.append(f"{dropped} candidate(s) dropped after reading the full description")
        # Deals we did not enrich keep their list-row verdict.
        return survivors + deals[budget:]

    def _deliver(self, verdict: Verdict) -> bool:
        """Send one alert; queue it for retry if delivery failed."""
        try:
            ok = bool(self.notifier.alert(verdict, self.settings))
        except Exception as exc:  # noqa: BLE001 - never lose a deal to an exception
            LOGGER.error("alert for %s raised: %s", verdict.post.token, exc)
            ok = False
        if ok:
            self._sent_tokens.add(verdict.post.token)
            LOGGER.info(
                "alert sent | %s | %s | %s",
                verdict.post.token,
                verdict.price.display,
                verdict.post.title[:60],
            )
        else:
            self._queue_for_retry(verdict)
        return ok

    def _queue_for_retry(self, verdict: Verdict) -> None:
        self._retry_queue.append((verdict, 1))
        LOGGER.warning(
            "alert for %s failed — queued for retry (%d pending)",
            verdict.post.token, len(self._retry_queue),
        )

    def _flush_retry_queue(self) -> int:
        """Retry previously failed alerts. Returns how many were delivered."""
        if not self._retry_queue:
            return 0
        pending, self._retry_queue = self._retry_queue, []
        delivered = 0
        for verdict, attempts in pending:
            if self.stopping:
                self._retry_queue.append((verdict, attempts))
                continue
            try:
                ok = bool(self.notifier.alert(verdict, self.settings))
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("retry for %s raised: %s", verdict.post.token, exc)
                ok = False
            if ok:
                delivered += 1
                self._sent_tokens.add(verdict.post.token)
                LOGGER.info("retry succeeded for %s", verdict.post.token)
            elif attempts + 1 > self.settings.max_alert_attempts:
                LOGGER.error(
                    "giving up on %s after %d attempts", verdict.post.token, attempts + 1
                )
            else:
                self._retry_queue.append((verdict, attempts + 1))
        return delivered

    def _was_sent(self, verdict: Verdict) -> bool:
        return verdict.post.token in self._sent_tokens


def _filter_summary(settings: Settings) -> str:
    return (
        f"deal_max_price={settings.deal_max_price} floor={settings.deal_price_floor} "
        f"require_price={settings.require_price} skip_down_payment={settings.skip_down_payment_ads} "
        f"max_age_min={settings.max_age_minutes} include={settings.include_keywords or '-'} "
        f"exclude={settings.exclude_keywords or '-'}"
    )


# --------------------------------------------------------------------------- #
# CLI commands
# --------------------------------------------------------------------------- #
def command_check(settings: Settings) -> int:
    """Validate configuration, Telegram credentials and Divar reachability."""
    problems = settings.problems()
    if problems:
        for problem in problems:
            LOGGER.error("config: %s", problem)
        return 2
    LOGGER.info("config OK | %s", settings.describe())

    notifier = build_notifier(settings)
    try:
        LOGGER.info("%s", notifier.verify())
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Telegram check failed: %s", exc)
        return 3
    finally:
        notifier.close()

    client = DivarClient(settings)
    try:
        LOGGER.info("Divar check: %s", client.probe())
    except DivarError as exc:
        LOGGER.error("Divar check failed: %s", exc)
        LOGGER.error(
            "Hint: Divar's listing API usually only answers traffic from inside "
            "Iran — set DIVAR_PROXY (or PROXY_URL) in .env."
        )
        return 4
    finally:
        client.close()
    return 0


def command_stats(settings: Settings, as_json: bool = False) -> int:
    with SeenStore(settings.db_path, prune_days=settings.prune_days) as store:
        snapshot = store.stats()
        runs = [dict(row) for row in store.recent_runs(10)]
    if as_json:
        print(json.dumps({"store": snapshot, "recent_runs": runs}, ensure_ascii=False, indent=2))
        return 0

    print("Seen-token store")
    for key, value in snapshot.items():
        print(f"  {key:16} {value}")
    print("\nLast runs (newest first)")
    if not runs:
        print("  (none yet)")
    for run in runs:
        print(
            "  {started_at}  fetched={fetched} fresh={fresh} deals={deals} "
            "sent={sent} failed={failed}  {note}".format(
                started_at=run.get("started_at") or "-",
                fetched=run.get("fetched", 0),
                fresh=run.get("fresh", 0),
                deals=run.get("deals", 0),
                sent=run.get("sent", 0),
                failed=run.get("failed", 0),
                note=(run.get("note") or "")[:90],
            )
        )
    return 0


def command_test_alert(settings: Settings) -> int:
    """Send a synthetic alert so the operator can see the message layout."""
    from divar_client import DivarPost
    from filters import PriceDecision

    post = DivarPost(
        token="TESTALERT",
        title="آیفون ۱۳ پرومکس ۲۵۶ گیگ (پیام آزمایشی)",
        price=45_000_000,
        price_text="۴۵٬۰۰۰٬۰۰۰ تومان",
        city="تهران",
        district="سعادت‌آباد",
        category="mobile-phones",
        published_at=datetime.now(timezone.utc),
        time_is_exact=True,
        image_count=3,
        has_chat=True,
        description="این یک پیام آزمایشی است تا قالب هشدارها را ببینید.",
    )
    verdict = Verdict(
        post=post,
        is_deal=True,
        price=PriceDecision(value=45_000_000, source="api_text", raw_text=post.price_text),
        reasons=["💰 پیام آزمایشی — قالب هشدار", "🕒 آگهی تازه: لحظاتی پیش"],
        warnings=["این آگهی واقعی نیست"],
        score=10,
    )
    notifier = build_notifier(settings)
    try:
        ok = notifier.alert(verdict, settings)
        LOGGER.info("test alert %s", "sent" if ok else "FAILED")
        return 0 if ok else 3
    finally:
        notifier.close()


def _rebase_fixture(payload: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Shift every timestamp in a captured payload so it looks freshly published.

    Keeps the relative ages between rows (1 minute … 3 hours) but moves the whole
    page to ``now``, so the demo always renders realistic Persian timestamps.
    """
    from divar_client import parse_timestamp

    now = now or datetime.now(timezone.utc)
    payload = copy.deepcopy(payload)
    widgets = payload.get("list_widgets") or []

    stamps = []
    for widget in widgets:
        info = ((widget.get("action_log") or {}).get("server_side_info") or {}).get("info") or {}
        moment = parse_timestamp(info.get("sort_date"))
        if moment:
            stamps.append(moment)
    if not stamps:
        return payload

    offset = now - max(stamps)

    def shift(moment: datetime) -> datetime:
        return moment + offset

    for widget in widgets:
        info = ((widget.get("action_log") or {}).get("server_side_info") or {}).get("info") or {}
        moment = parse_timestamp(info.get("sort_date"))
        if moment:
            info["sort_date"] = shift(moment).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    page_info = (((payload.get("action_log") or {}).get("server_side_info") or {}).get("info")) or {}
    for row in page_info.get("posts_metadata") or []:
        moment = parse_timestamp(row.get("sort_date"))
        if moment:
            row["sort_date"] = str(int(shift(moment).timestamp() * 1_000_000))
    moment = parse_timestamp(page_info.get("last_post_date_epoch"))
    if moment:
        page_info["last_post_date_epoch"] = str(int(shift(moment).timestamp() * 1_000_000))

    cursor = (payload.get("pagination") or {}).get("data") or {}
    moment = parse_timestamp(cursor.get("last_post_date"))
    if moment:
        cursor["last_post_date"] = shift(moment).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return payload


def command_demo(settings: Settings) -> int:
    """Run one full cycle against a bundled fixture — no network needed.

    The demo applies the sample rules from ``.env.example`` (price floor and
    exclude keywords) so every branch of the deal filter is visible.
    """
    if not DEMO_FIXTURE.exists():
        LOGGER.error("demo fixture missing: %s", DEMO_FIXTURE)
        return 2
    payload = _rebase_fixture(json.loads(DEMO_FIXTURE.read_text(encoding="utf-8")))

    demo_settings = settings.with_updates(
        dry_run=True,
        db_path=":memory:",
        fetch_details=False,
        max_age_minutes=0,
        deal_price_floor=settings.deal_price_floor or 5_000_000,
        exclude_keywords=settings.exclude_keywords or ["خراب", "قطعات", "اوراق"],
    )
    client = DivarClient(demo_settings, sleep=lambda _seconds: None)
    client.request = lambda method, path, body=None: payload  # type: ignore[assignment]
    store = SeenStore(":memory:", prune_days=demo_settings.prune_days)
    store.mark_bootstrapped()  # demo: alert instead of silently seeding the store
    hunter = Hunter(demo_settings, client=client, store=store)

    LOGGER.info("=== DEMO CYCLE (offline fixture, no network) ===")
    stats = hunter.run_cycle()
    LOGGER.info("demo result: %s", stats.note)

    alerts = getattr(hunter.notifier, "sent", [])
    print(f"\n{to_persian_digits(len(alerts))} alert(s) rendered:\n")
    for index, text in enumerate(alerts, start=1):
        print(f"──────────── alert {index} " + "─" * 40)
        print(text)
    hunter.shutdown()
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="divar-hunter",
        description="Monitor Divar for underpriced listings and alert on Telegram.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--env", default=".env", help="path to the .env file")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="log alerts instead of sending")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--log-level", default=None, help="override LOG_LEVEL")
    parser.add_argument("--log-file", default=None, help="override LOG_FILE")
    parser.add_argument("--city", default=None, help="override CITY (slug, id or Persian name)")
    parser.add_argument("--category", default=None, help="override CATEGORY slug")
    parser.add_argument("--query", default=None, help="override SEARCH_QUERY")
    parser.add_argument(
        "--max-price", type=int, default=None, help="override DEAL_MAX_PRICE (Toman)"
    )
    parser.add_argument(
        "--interval", type=float, default=None, help="override CHECK_INTERVAL_SECONDS"
    )
    parser.add_argument(
        "--details", action="store_true", help="override FETCH_DETAILS=true (slower, sharper)"
    )

    commands = parser.add_argument_group("commands", "run something other than the loop")
    commands.add_argument("--check", action="store_true", help="validate config + Telegram + Divar")
    commands.add_argument("--stats", action="store_true", help="print store/run statistics")
    commands.add_argument("--json", action="store_true", help="with --stats: emit JSON")
    commands.add_argument("--test-alert", action="store_true", help="send a sample alert")
    commands.add_argument("--demo", action="store_true", help="offline demo cycle from a fixture")
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace) -> Settings:
    """Load settings from .env, then apply CLI overrides."""
    overrides: Dict[str, Any] = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.city:
        overrides["city"] = args.city
    if args.category:
        overrides["category"] = args.category
    if args.query:
        overrides["search_query"] = args.query
    if args.max_price is not None:
        overrides["deal_max_price"] = args.max_price
    if args.interval is not None:
        overrides["check_interval_seconds"] = max(5.0, args.interval)
    if args.details:
        overrides["fetch_details"] = True

    # --demo and --stats are offline commands: they must work without a bot token.
    needs_credentials = not (args.demo or args.stats)
    settings = Settings.load(
        env_file=args.env, overrides=overrides or None, validate=needs_credentials
    )
    if args.city:
        # Re-resolve the numeric id for the overridden city name/slug.
        from config import resolve_city_id

        city_id, problems = resolve_city_id(args.city)
        if problems:
            raise ConfigError(problems)
        settings = settings.with_updates(city_id=city_id)
    return settings


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    level = "DEBUG" if args.verbose else (args.log_level or None)

    try:
        settings = build_settings(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    setup_logging(level or settings.log_level, args.log_file or settings.log_file)
    LOGGER.debug("settings: %s", json.dumps(settings.masked(), ensure_ascii=False, default=str))

    if args.check:
        return command_check(settings)
    if args.stats:
        return command_stats(settings, as_json=args.json)
    if args.test_alert:
        return command_test_alert(settings)
    if args.demo:
        return command_demo(settings)

    hunter = Hunter(settings)
    try:
        return asyncio.run(hunter.run(once=args.once))
    except KeyboardInterrupt:  # pragma: no cover - handled by signal handlers
        hunter.request_stop("keyboard interrupt")
        return 130


if __name__ == "__main__":
    sys.exit(main())
