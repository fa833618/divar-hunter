"""Tests for the SQLite seen-token store."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from conftest import FIXTURE_NOW
from storage import RunStats, SeenStore


@pytest.fixture
def store():
    with SeenStore(":memory:", prune_days=14) as seen:
        yield seen


# --------------------------------------------------------------------------- #
def test_empty_store_knows_nothing(store, sample_posts):
    posts = [sample_posts(token="a"), sample_posts(token="b")]
    assert store.count() == 0
    assert store.unseen(posts) == posts


def test_mark_seen_then_unseen_filters_duplicates(store, sample_posts):
    first = sample_posts(token="a")
    second = sample_posts(token="b")
    store.mark_seen([first, second])
    assert store.unseen([first, second]) == []
    assert store.unseen([sample_posts(token="c")])[0].token == "c"
    assert store.contains("a") is True
    assert store.contains("zz") is False


def test_mark_seen_returns_the_row_count(store, sample_posts):
    assert store.mark_seen([sample_posts(token="a"), sample_posts(token="b")]) == 2
    assert store.mark_seen([]) == 0
    assert store.count() == 2


def test_mark_seen_stores_metadata(store, sample_posts):
    post = sample_posts(token="a", title="آیفون ۱۳", price=45_000_000, district="سعادت‌آباد")
    store.mark_seen([post])
    record = store.get("a")
    assert record.title == "آیفون ۱۳"
    assert record.price == 45_000_000
    assert record.district == "سعادت‌آباد"
    assert record.notified is False
    assert record.is_deal is False
    assert store.get("missing") is None


def test_notified_and_deal_flags_are_recorded(store, sample_posts):
    posts = [sample_posts(token="a"), sample_posts(token="b"), sample_posts(token="c")]
    store.mark_seen(posts, notified_tokens=["a"], deal_tokens=["a", "b"], now=FIXTURE_NOW)
    assert store.get("a").notified is True
    assert store.get("a").is_deal is True
    assert store.get("b").is_deal is True
    assert store.get("b").notified is False
    assert store.get("c").is_deal is False
    assert store.deal_count() == 2


def test_mark_notified_updates_existing_rows(store, sample_posts):
    store.mark_seen([sample_posts(token="a")])
    assert store.mark_notified(["a"], now=FIXTURE_NOW) >= 1
    record = store.get("a")
    assert record.notified is True
    assert record.notified_at.startswith("2026-09-21")
    assert store.mark_notified([]) == 0


def test_remarking_keeps_the_notified_flag(store, sample_posts):
    store.mark_seen([sample_posts(token="a")], notified_tokens=["a"], now=FIXTURE_NOW)
    store.mark_seen([sample_posts(token="a")], now=FIXTURE_NOW + timedelta(minutes=5))
    record = store.get("a")
    assert record.notified is True  # never un-set
    assert record.last_seen_at > record.first_seen_at


def test_known_tokens_handles_large_batches(store, sample_posts):
    tokens = [f"tok{i}" for i in range(1000)]
    store.mark_seen([sample_posts(token=token) for token in tokens[:500]])
    known = store.known_tokens(tokens)
    assert set(known) == set(tokens[:500])  # order follows the index, not the input


def test_bootstrap_flag(store, sample_posts):
    assert store.is_bootstrapped is False
    store.mark_bootstrapped(FIXTURE_NOW)
    assert store.is_bootstrapped is True
    assert store.meta_get("bootstrapped_at").startswith("2026-09-21")
    store.meta_set("custom", "value")
    assert store.meta_get("custom") == "value"
    assert store.meta_get("nope") is None


def test_prune_removes_only_old_tokens(store, sample_posts):
    old = FIXTURE_NOW - timedelta(days=20)
    store.mark_seen([sample_posts(token="old")], now=old)
    store.mark_seen([sample_posts(token="new")], now=FIXTURE_NOW)
    assert store.prune(now=FIXTURE_NOW) == 1
    assert store.contains("old") is False
    assert store.contains("new") is True


def test_prune_disabled_keeps_everything(sample_posts):
    with SeenStore(":memory:", prune_days=0) as store:
        store.mark_seen([sample_posts(token="old")], now=FIXTURE_NOW - timedelta(days=400))
        assert store.prune(now=FIXTURE_NOW) == 0
        assert store.count() == 1


def test_run_log_records_cycles(store):
    first = RunStats(fetched=11, fresh=4, deals=2, sent=2, failed=0, note="ok",
                     started_at=FIXTURE_NOW, finished_at=FIXTURE_NOW + timedelta(seconds=3))
    store.log_run(first)
    store.log_run(RunStats(fetched=9, fresh=0, deals=0, sent=0, failed=0, note="quiet"))
    runs = store.recent_runs(10)
    assert len(runs) == 2
    assert runs[0]["note"] == "quiet"  # newest first
    assert runs[1]["fetched"] == 11
    assert store.stats()["runs"] == 2


def test_long_notes_are_truncated(store):
    stats = RunStats(note="x" * 4000)
    store.log_run(stats)
    assert len(store.recent_runs(1)[0]["note"]) == 500


def test_stats_snapshot(store, sample_posts):
    store.mark_seen([sample_posts(token="a")], notified_tokens=["a"], deal_tokens=["a"])
    snapshot = store.stats()
    assert snapshot["tokens"] == 1
    assert snapshot["notified"] == 1
    assert snapshot["deals"] == 1
    assert snapshot["db_path"] == ":memory:"


def test_file_store_creates_its_directory(tmp_path, sample_posts):
    path = tmp_path / "nested" / "dir" / "seen.db"
    with SeenStore(str(path)) as store:
        store.mark_seen([sample_posts(token="a")])
    assert path.exists()
    # re-opening must see the previous state
    with SeenStore(str(path)) as reopened:
        assert reopened.contains("a")


def test_concurrent_writes_are_safe(store, sample_posts):
    errors = []

    def writer(index):
        try:
            store.mark_seen([sample_posts(token=f"t{index}-{i}") for i in range(50)])
        except Exception as exc:  # pragma: no cover - would mean the lock failed
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert store.count() == 300


def test_posts_without_tokens_are_ignored(store, sample_posts):
    assert store.mark_seen([sample_posts(token="")]) == 0
    assert store.unseen([sample_posts(token="")]) == []
