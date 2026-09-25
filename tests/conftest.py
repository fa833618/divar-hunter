"""Shared test fixtures: offline HTTP fakes, deterministic settings, sample posts."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"

#: The "now" every fixture timestamp is relative to.
FIXTURE_NOW = datetime(2026, 9, 21, 20, 36, 10, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# environment hygiene
# --------------------------------------------------------------------------- #
CONFIG_ENV_VARS = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_PROXY", "TELEGRAM_API_BASE",
    "TELEGRAM_TIMEOUT", "TELEGRAM_MAX_RETRIES", "ALERT_SPACING_SECONDS",
    "MAX_ALERTS_PER_CYCLE", "MAX_ALERT_ATTEMPTS", "CITY", "CITY_ID", "CATEGORY",
    "SEARCH_QUERY", "DISTRICT_IDS", "PRICE_MIN", "PRICE_MAX", "HAS_PHOTO_ONLY",
    "ONLY_PERSONAL_ADS", "PAGES_PER_CYCLE", "PAGE_SIZE", "MAX_POSTS_PER_CYCLE",
    "FETCH_DETAILS", "MAX_DETAIL_LOOKUPS", "MAX_AGE_MINUTES", "DEAL_MAX_PRICE",
    "DEAL_PRICE_FLOOR", "REQUIRE_PRICE", "SKIP_DOWN_PAYMENT_ADS", "INCLUDE_KEYWORDS",
    "EXCLUDE_KEYWORDS", "CHECK_INTERVAL_SECONDS", "REQUEST_DELAY_MIN", "REQUEST_DELAY_MAX",
    "REQUEST_TIMEOUT", "MAX_RETRIES", "BACKOFF_BASE", "BACKOFF_MAX", "DIVAR_PROXY",
    "PROXY_URL", "DIVAR_API_BASE", "DIVAR_WEB_BASE", "DIVAR_ENDPOINT_MODE", "DB_PATH",
    "PRUNE_DAYS", "BOOTSTRAP_SILENT", "LOG_LEVEL", "LOG_FILE", "DRY_RUN",
    "TZ_OFFSET_HOURS", "HTTPS_PROXY", "ALL_PROXY",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Every test starts from a clean environment (no ambient .env influence)."""
    for name in CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings_factory():
    """Build a deterministic :class:`Settings` with network waits removed."""
    from config import Settings

    def factory(**overrides: Any) -> Settings:
        base: Dict[str, Any] = dict(
            telegram_bot_token="123456:TEST-TOKEN",
            telegram_chat_id="42424242",
            dry_run=True,
            db_path=":memory:",
            city="tehran",
            city_id="1",
            category="mobile-phones",
            deal_max_price=50_000_000,
            deal_price_floor=5_000_000,
            max_age_minutes=0,
            request_delay_min=0.0,
            request_delay_max=0.0,
            request_timeout=5.0,
            max_retries=3,
            backoff_base=0.01,
            backoff_max=0.05,
            telegram_max_retries=3,
            telegram_timeout=5.0,
            alert_spacing_seconds=0.0,
            check_interval_seconds=5.0,
            bootstrap_silent=True,
            log_level="WARNING",
        )
        base.update(overrides)
        return Settings.load(env_file=None, overrides=base, validate=False)

    return factory


@pytest.fixture
def settings(settings_factory):
    return settings_factory()


# --------------------------------------------------------------------------- #
# offline HTTP
# --------------------------------------------------------------------------- #
class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(
        self,
        payload: Any = None,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        text: Optional[str] = None,
        json_error: bool = False,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self._json_error = json_error
        if text is None:
            text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        self.text = text

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("no JSON object could be decoded")
        if self.payload is None:
            raise ValueError("no JSON object could be decoded")
        return self.payload


@dataclass
class Call:
    method: str
    url: str
    json: Any = None
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: Any = None
    proxies: Any = None


class FakeSession:
    """Records calls and replays queued responses (or raises queued exceptions)."""

    def __init__(self, responses: Optional[List[Any]] = None, default: Any = None) -> None:
        self.responses: List[Any] = list(responses or [])
        self.default = default if default is not None else FakeResponse({}, 200)
        self.calls: List[Call] = []
        self.closed = False

    # requests.Session surface used by the code under test
    def request(self, method, url, json=None, headers=None, timeout=None, proxies=None, **kwargs):
        self.calls.append(Call(method, url, json, headers or {}, timeout, proxies))
        return self._next()

    def post(self, url, json=None, headers=None, timeout=None, proxies=None, **kwargs):
        return self.request("POST", url, json=json, headers=headers, timeout=timeout, proxies=proxies)

    def get(self, url, headers=None, timeout=None, proxies=None, **kwargs):
        return self.request("GET", url, headers=headers, timeout=timeout, proxies=proxies)

    def close(self):
        self.closed = True

    def _next(self):
        if not self.responses:
            return self.default
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item

    # test helpers
    @property
    def urls(self) -> List[str]:
        return [call.url for call in self.calls]

    @property
    def bodies(self) -> List[Any]:
        return [call.json for call in self.calls]


class RecordingSleep:
    """Collects sleep durations instead of actually sleeping."""

    def __init__(self) -> None:
        self.durations: List[float] = []

    def __call__(self, seconds: float) -> None:
        self.durations.append(float(seconds))

    @property
    def total(self) -> float:
        return sum(self.durations)


# --------------------------------------------------------------------------- #
# fixtures / sample data
# --------------------------------------------------------------------------- #
@pytest.fixture
def postlist_payload() -> Dict[str, Any]:
    return json.loads((FIXTURES / "postlist_page.json").read_text(encoding="utf-8"))


@pytest.fixture
def legacy_payload() -> Dict[str, Any]:
    return json.loads((FIXTURES / "postlist_legacy.json").read_text(encoding="utf-8"))


@pytest.fixture
def detail_payload() -> Dict[str, Any]:
    return json.loads((FIXTURES / "post_detail.json").read_text(encoding="utf-8"))


@pytest.fixture
def divar_client_factory(settings_factory):
    """Build a DivarClient wired to a FakeSession with deterministic time/jitter."""
    import random

    from divar_client import DivarClient

    def factory(session: FakeSession, settings=None, sleep=None, seed: int = 7, **kwargs):
        settings = settings or settings_factory()
        return DivarClient(
            settings,
            session=session,
            sleep=sleep if sleep is not None else (lambda _s: None),
            rng=random.Random(seed),
            now=lambda: FIXTURE_NOW,
            **kwargs,
        )

    return factory


@pytest.fixture
def sample_posts():
    """A few hand-made posts for filter tests."""
    from divar_client import DivarPost

    def make(token="tok1", title="آیفون ۱۳", price=45_000_000, price_text=None,
             age_minutes=1, description=None, district="سعادت‌آباد", city="تهران",
             category="mobile-phones", **kwargs):
        published_at = kwargs.pop("published_at", FIXTURE_NOW - timedelta(minutes=age_minutes))
        return DivarPost(
            token=token,
            title=title,
            price=price,
            price_text=price_text or (f"{price:,}".replace(",", "٬") + " تومان" if price else ""),
            city=city,
            district=district,
            category=category,
            description=description,
            published_at=published_at,
            time_is_exact=True,
            time_text="لحظاتی پیش",
            image_count=kwargs.pop("image_count", 2),
            has_chat=kwargs.pop("has_chat", True),
            **kwargs,
        )

    return make


@pytest.fixture
def fixture_posts(divar_client_factory, postlist_payload):
    """Parse the fixture page into DivarPost objects."""
    client = divar_client_factory(FakeSession([FakeResponse(postlist_payload)]))
    return client.fetch_latest()


# --------------------------------------------------------------------------- #
# fakes for the monitoring loop
# --------------------------------------------------------------------------- #
class FakeNotifier:
    """In-memory notifier: records alerts, can be told to fail."""

    def __init__(self, fail_tokens=(), fail_times: int = 0) -> None:
        from notifier import build_alert_text

        self._build = build_alert_text
        self.fail_tokens = set(fail_tokens)
        self.fail_times = fail_times
        self.alerts = []          # rendered plain-text messages
        self.alerted_tokens = []  # tokens in delivery order
        self.messages = []        # anything sent via send_text/summary
        self.summaries = []
        self.verify_calls = 0

    def alert(self, verdict, settings, **kwargs):
        message = self._build(verdict, settings, **kwargs)
        token = verdict.post.token
        self.alerted_tokens.append(token)
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        if token in self.fail_tokens:
            return False
        self.alerts.append(message.plain)
        return True

    def send_text(self, text, **kwargs):
        self.messages.append(text)
        return True

    def summary(self, verdicts, settings):
        self.summaries.append([v.post.token for v in verdicts])
        return self.send_text(f"summary of {len(verdicts)}")

    def verify(self):
        self.verify_calls += 1
        return "fake notifier OK"

    def close(self):
        return None


class FakeClient:
    """Duck-typed DivarClient: serves canned pages and records enrichments."""

    def __init__(self, pages=None, detail_updates=None, on_fetch=None, error=None):
        self.error = error
        self.pages = list(pages or [])
        self.detail_updates = detail_updates or {}
        self.on_fetch = on_fetch
        self.fetch_calls = 0
        self.enriched = []
        self.closed = False

    def fetch_latest(self, max_posts=None):
        self.fetch_calls += 1
        if self.on_fetch:
            self.on_fetch(self.fetch_calls)
        if self.error is not None:
            raise self.error
        if not self.pages:
            return []
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]

    def enrich(self, posts, limit=None):
        count = 0
        for post in posts[: limit if limit is not None else len(posts)]:
            self.enriched.append(post.token)
            updates = self.detail_updates.get(post.token)
            if updates:
                for key, value in updates.items():
                    setattr(post, key, value)
            count += 1
        return count

    def close(self):
        self.closed = True
