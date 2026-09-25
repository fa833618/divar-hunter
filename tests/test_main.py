"""Tests for the monitoring loop, CLI commands and graceful shutdown."""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import FIXTURE_NOW, FakeClient, FakeNotifier
from divar_client import DivarClient, DivarError, DivarPost
from main import Hunter, build_settings, main, parse_args, setup_logging
from storage import SeenStore


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def hunter_factory(settings_factory, fixture_posts):
    def factory(
        posts=None,
        pages=None,
        notifier=None,
        settings=None,
        client=None,
        **setting_overrides,
    ):
        defaults = dict(bootstrap_silent=True, max_age_minutes=0)
        defaults.update(setting_overrides)
        settings = settings or settings_factory(**defaults)
        client = client or FakeClient(pages=pages if pages is not None else [
            posts if posts is not None else fixture_posts
        ])
        notifier = notifier or FakeNotifier()
        store = SeenStore(":memory:", prune_days=settings.prune_days)
        hunter = Hunter(settings, client=client, notifier=notifier, store=store)
        return hunter, client, notifier, store

    return factory


def deals_expected():
    """The four listings in the fixture page that beat the 50M ceiling."""
    return ["gParts07", "hStale08", "kMiDec11", "aDeaL001"]


# --------------------------------------------------------------------------- #
# one cycle
# --------------------------------------------------------------------------- #
def test_first_cycle_bootstraps_silently(hunter_factory, fixture_posts):
    hunter, _client, notifier, store = hunter_factory()
    stats = hunter.run_cycle()

    assert notifier.alerts == []            # no flood on the very first run
    assert stats.fresh == len(fixture_posts)
    assert "bootstrap" in stats.note
    assert store.count() == len(fixture_posts)
    assert store.is_bootstrapped is True
    assert hunter.cycles == 1
    assert hunter.totals.fetched == len(fixture_posts)


def test_bootstrap_can_be_disabled(hunter_factory):
    hunter, _client, notifier, _store = hunter_factory(bootstrap_silent=False)
    stats = hunter.run_cycle()

    assert notifier.alerted_tokens == deals_expected()
    assert stats.deals == 4
    assert stats.sent == 4
    assert stats.failed == 0


def test_second_cycle_only_sees_new_posts(hunter_factory, fixture_posts, sample_posts):
    hunter, client, notifier, store = hunter_factory(bootstrap_silent=False)
    hunter.run_cycle()
    first_round = list(notifier.alerted_tokens)

    notifier.alerted_tokens.clear()
    stats = hunter.run_cycle()  # same page again
    assert stats.fresh == 0
    assert notifier.alerted_tokens == []
    assert store.count() == len(fixture_posts)

    # a genuinely new listing arrives on the next page
    client.pages = [fixture_posts + [sample_posts(token="brandNew", price=20_000_000)]]
    stats = hunter.run_cycle()
    assert stats.fresh == 1
    assert notifier.alerted_tokens == ["brandNew"]
    assert first_round  # sanity: the first round did alert


def test_deals_are_alerted_best_first(hunter_factory):
    hunter, _client, notifier, _store = hunter_factory(bootstrap_silent=False)
    hunter.run_cycle()
    scores = [
        (token, notifier.alerts[index].count("تومان"))
        for index, token in enumerate(notifier.alerted_tokens)
    ]
    assert notifier.alerted_tokens == deals_expected()
    assert all(count >= 1 for _token, count in scores)


def test_seen_flags_record_which_deals_were_notified(hunter_factory, sample_posts):
    hunter, _client, notifier, store = hunter_factory(bootstrap_silent=False)
    hunter.run_cycle()
    record = store.get("aDeaL001")
    assert record.notified is True
    assert record.is_deal is True
    assert store.get("bExpns02").is_deal is False  # above the ceiling
    assert store.get("bExpns02").notified is False


def test_alert_budget_holds_back_the_overflow(hunter_factory):
    hunter, _client, notifier, _store = hunter_factory(
        bootstrap_silent=False, max_alerts_per_cycle=2
    )
    stats = hunter.run_cycle()

    assert stats.deals == 4
    assert stats.sent == 2
    assert notifier.alerted_tokens == deals_expected()[:2]
    assert notifier.summaries == [deals_expected()[2:]]


def test_failed_alert_is_retried_on_the_next_cycle(hunter_factory):
    notifier = FakeNotifier(fail_times=1)
    hunter, _client, _notifier, _store = hunter_factory(
        bootstrap_silent=False, notifier=notifier
    )
    first = hunter.run_cycle()
    assert first.failed == 1
    assert first.sent == 3
    assert len(hunter._retry_queue) == 1

    second = hunter.run_cycle()  # nothing new, but the queue is flushed first
    assert second.sent == 1
    assert second.failed == 0
    assert hunter._retry_queue == []
    assert len(notifier.alerted_tokens) == 5  # 4 deals + 1 retry


def test_alerts_are_dropped_after_too_many_failures(hunter_factory):
    notifier = FakeNotifier(fail_tokens={"gParts07"})
    hunter, _client, _notifier, _store = hunter_factory(
        bootstrap_silent=False, notifier=notifier, max_alert_attempts=2
    )
    hunter.run_cycle()
    hunter.run_cycle()
    third = hunter.run_cycle()
    assert hunter._retry_queue == []      # given up
    assert third.sent == 0
    assert "gParts07" not in notifier.alerts


def test_detail_enrichment_can_drop_a_candidate(hunter_factory):
    """FETCH_DETAILS: the real price on the post page is above the ceiling."""
    hunter, client, notifier, _store = hunter_factory(
        bootstrap_silent=False, fetch_details=True, max_detail_lookups=2,
    )
    client.detail_updates = {"gParts07": {"price": 90_000_000,
                                          "description": "قیمت واقعی ۹۰ میلیون تومان"}}
    stats = hunter.run_cycle()

    assert client.enriched == deals_expected()[:2]
    assert stats.deals == 3                       # gParts07 dropped after reading it
    assert notifier.alerted_tokens == deals_expected()[1:]
    assert "dropped" in stats.note


def test_cycle_survives_a_notifier_explosion(hunter_factory, monkeypatch):
    hunter, _client, notifier, _store = hunter_factory(bootstrap_silent=False)

    def boom(*_args, **_kwargs):
        raise RuntimeError("telegram is on fire")

    monkeypatch.setattr(notifier, "alert", boom)
    stats = hunter.run_cycle()
    assert stats.failed == 4
    assert len(hunter._retry_queue) == 4


def test_empty_fetch_is_not_an_error(hunter_factory):
    hunter, _client, notifier, _store = hunter_factory(posts=[])
    stats = hunter.run_cycle()
    assert stats.fetched == 0
    assert notifier.alerts == []
    assert "no posts" in stats.note


def test_cycle_is_logged_into_the_run_log(hunter_factory):
    hunter, _client, _notifier, store = hunter_factory(bootstrap_silent=False)
    hunter.run_cycle()
    runs = store.recent_runs(5)
    assert len(runs) == 1
    assert runs[0]["deals"] == 4
    assert runs[0]["sent"] == 4


# --------------------------------------------------------------------------- #
# the async loop
# --------------------------------------------------------------------------- #
def test_run_once_returns_zero(hunter_factory, caplog):
    hunter, _client, notifier, _store = hunter_factory(bootstrap_silent=False)
    with caplog.at_level("INFO", logger="divar.hunter"):
        assert asyncio.run(hunter.run(once=True)) == 0
    assert hunter.cycles == 1
    assert "cycle 1 done" in caplog.text
    assert notifier.alerted_tokens == deals_expected()


def test_once_mode_exits_nonzero_when_the_cycle_fails(hunter_factory):
    broken = FakeClient(error=DivarError("divar is unreachable"))
    hunter, _client, notifier, _store = hunter_factory(client=broken)
    assert asyncio.run(hunter.run(once=True)) == 1
    assert hunter.cycles == 0
    assert notifier.alerts == []


def test_run_keeps_going_until_stopped(hunter_factory):
    hunter, client, _notifier, _store = hunter_factory(
        bootstrap_silent=False, check_interval_seconds=0.05
    )
    # ask for a stop as soon as the third fetch starts
    client.on_fetch = lambda count: hunter.request_stop("test") if count >= 3 else None
    assert asyncio.run(hunter.run()) == 130
    assert hunter.cycles == 3
    assert hunter.stopping is True


def test_divar_outage_backs_off_instead_of_crashing(hunter_factory, caplog):
    broken = FakeClient(error=DivarError("no route to divar.ir", status=None))
    hunter, _client, _notifier, _store = hunter_factory(client=broken)
    broken.on_fetch = lambda _count: hunter.request_stop("test")

    with caplog.at_level("ERROR", logger="divar.hunter"):
        assert asyncio.run(hunter.run()) == 130
    assert "cycle 1 failed" in caplog.text  # cycles are reported 1-based
    assert hunter.cycles == 0  # a failed cycle is not counted as done


def test_shutdown_closes_every_resource(hunter_factory):
    hunter, client, notifier, store = hunter_factory()
    hunter.shutdown()
    assert client.closed is True
    assert store._conn is not None
    with pytest.raises(Exception):
        store.count()  # the connection was closed


def test_request_stop_is_idempotent(hunter_factory, caplog):
    hunter, _client, _notifier, _store = hunter_factory()
    with caplog.at_level("INFO", logger="divar.hunter"):
        hunter.request_stop("SIGINT")
        hunter.request_stop("SIGTERM")
    assert caplog.text.count("shutdown requested") == 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_parse_args_defaults():
    args = parse_args([])
    assert args.env == ".env"
    assert not args.once and not args.dry_run and not args.demo


def test_build_settings_applies_cli_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DEAL_MAX_PRICE", "999999999")
    args = parse_args(["--city", "mashhad", "--max-price", "12345678", "--dry-run",
                       "--query", "آیفون", "--category", "tablet", "--interval", "30",
                       "--details", "--env", str(tmp_path / "missing.env")])
    settings = build_settings(args)
    assert settings.city_id == "3"
    assert settings.deal_max_price == 12_345_678
    assert settings.dry_run is True
    assert settings.search_query == "آیفون"
    assert settings.category == "tablet"
    assert settings.check_interval_seconds == 30.0
    assert settings.fetch_details is True


def test_build_settings_rejects_an_unknown_city(tmp_path):
    args = parse_args(["--city", "atlantis", "--dry-run", "--env", str(tmp_path / "missing.env")])
    from config import ConfigError

    with pytest.raises(ConfigError):
        build_settings(args)


def test_demo_command_runs_offline_and_prints_alerts(capsys, tmp_path):
    code = main(["--demo", "--env", str(tmp_path / "missing.env")])
    assert code == 0
    out = capsys.readouterr().out
    assert "alert(s) rendered" in out
    assert "📌 عنوان آگهی" in out
    assert "💰 قیمت اعلامی" in out
    assert "مشاهده آگهی در دیوار" in out or "divar.ir/v/" in out


def test_demo_command_does_not_hit_the_network(capsys, tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - must never be called
        raise AssertionError("demo mode performed a network call")

    monkeypatch.setattr(DivarClient, "request", explode)
    assert main(["--demo", "--env", str(tmp_path / "missing.env")]) == 0
    assert "alert(s) rendered" in capsys.readouterr().out


def test_stats_command_without_a_database(monkeypatch, capsys):
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("DRY_RUN", "true")
    assert main(["--stats"]) == 0
    assert "Seen-token store" in capsys.readouterr().out


def test_stats_command_json(monkeypatch, capsys):
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("DRY_RUN", "true")
    assert main(["--stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["store"]["tokens"] == 0
    assert payload["recent_runs"] == []


def test_main_refuses_to_start_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert main([]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_check_command_reports_a_bad_telegram_setup(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert main(["--check"]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_test_alert_command_uses_the_dry_run_notifier(monkeypatch, capsys):
    """main() reconfigures logging, so the message lands on stdout, not caplog."""
    monkeypatch.setenv("DRY_RUN", "true")
    assert main(["--test-alert"]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN alert" in out
    assert "پیام آزمایشی" in out
    assert "📌 عنوان آگهی" in out


def test_setup_logging_writes_a_rotating_file(tmp_path, caplog):
    log_file = tmp_path / "logs" / "hunter.log"
    setup_logging("INFO", str(log_file))
    import logging

    logging.getLogger("divar.hunter").info("hello from the file handler")
    assert log_file.exists()
    assert "hello from the file handler" in log_file.read_text(encoding="utf-8")


def test_setup_logging_accepts_an_unknown_level(tmp_path):
    setup_logging("NOPE")  # falls back to INFO instead of raising


def test_hunter_starts_with_a_real_client_and_store(settings_factory, tmp_path):
    """Without injection, the pieces are wired from the settings."""
    settings = settings_factory(db_path=str(tmp_path / "state.db"), dry_run=True)
    hunter = Hunter(settings)
    try:
        assert isinstance(hunter.client, DivarClient)
        assert hunter.client.category == "mobile-phones"
        assert isinstance(hunter.store, SeenStore)
        assert (tmp_path / "state.db").exists()
        assert hunter.notifier.__class__.__name__ == "DryRunNotifier"
    finally:
        hunter.shutdown()


def test_stop_during_alerting_marks_the_cycle(hunter_factory):
    hunter, _client, notifier, _store = hunter_factory(bootstrap_silent=False)
    original_alert = notifier.alert

    def alert_then_stop(verdict, settings, **kwargs):
        result = original_alert(verdict, settings, **kwargs)
        hunter.request_stop("test")  # operator pressed Ctrl-C mid-cycle
        return result

    notifier.alert = alert_then_stop
    stats = hunter.run_cycle()
    assert "shutdown" in stats.note
    assert stats.sent == 1  # only the first alert went out


def test_post_model_helpers(sample_posts):
    post = sample_posts(token="t1", title="آیفون", description="سالم")
    assert post.url == "https://divar.ir/v/t1"
    assert post.location() == "تهران، سعادت‌آباد"
    assert post.searchable_text() == "آیفون سالم"
    assert post.age_seconds(FIXTURE_NOW) == pytest.approx(60, abs=1)
    assert post.to_dict()["token"] == "t1"
    assert DivarPost(token="").url == ""
