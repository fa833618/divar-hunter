"""Tests for the Divar client: parsing, headers, jitter, retries, fallback."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
import requests

from conftest import FIXTURE_NOW, FakeResponse, FakeSession, RecordingSleep
from divar_client import (
    CITY_IDS,
    CITY_SLUGS,
    DivarClient,
    DivarError,
    ShutdownRequested,
    list_known_cities,
    parse_timestamp,
    resolve_category,
)

# --------------------------------------------------------------------------- #
# row parsing
# --------------------------------------------------------------------------- #
def test_fixture_page_is_parsed_into_posts(fixture_posts):
    assert len(fixture_posts) == 11
    tokens = [post.token for post in fixture_posts]
    assert "aDeaL001" in tokens


def test_row_fields_are_normalized(fixture_posts):
    post = next(p for p in fixture_posts if p.token == "aDeaL001")
    assert post.title == "آیفون ۱۳ پرومکس ۲۵۶ گیگ سالم"
    assert post.price == 45_000_000
    assert post.city == "تهران"
    assert post.district == "سعادت‌آباد"
    assert post.category == "mobile-phones"  # from posts_metadata extra_info
    assert post.has_chat is True
    assert post.image_count == 4
    assert post.url == "https://divar.ir/v/aDeaL001"
    assert post.image_url.startswith("https://s100.divarcdn.com/")


def test_publish_time_comes_from_the_api_log(fixture_posts):
    post = next(p for p in fixture_posts if p.token == "aDeaL001")
    assert post.time_is_exact is True
    assert post.published_at == FIXTURE_NOW - timedelta(minutes=1)
    assert post.age_seconds(FIXTURE_NOW) == pytest.approx(60, abs=1)


def test_posts_are_sorted_newest_first(fixture_posts):
    stamps = [post.published_at for post in fixture_posts]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.parametrize(
    "token,expected_price,expected_note",
    [
        ("cNoPrc03", None, "negotiable"),
        ("fFreez06", None, "free"),
        ("iSwapx09", None, "exchange"),
        ("jDepst10", 100_000_000, "deposit_rent"),
    ],
)
def test_non_numeric_prices_are_labeled(fixture_posts, token, expected_price, expected_note):
    post = next(p for p in fixture_posts if p.token == token)
    assert post.price == expected_price
    assert post.price_note == expected_note


def test_down_payment_row_is_flagged(fixture_posts):
    post = next(p for p in fixture_posts if p.token == "dDownP04")
    assert post.price == 20_000_000
    assert post.price_is_down_payment is True


def test_relative_time_is_used_when_no_timestamp_exists(divar_client_factory):
    payload = {
        "list_widgets": [
            {
                "widget_type": "POST_ROW",
                "data": {
                    "token": "relOnly1",
                    "title": "گوشی سالم",
                    "middle_description_text": "۱۲٬۰۰۰٬۰۰۰ تومان",
                    "bottom_description_text": "۲ ساعت پیش در ونک",
                    "has_chat": True,
                    "image_count": 1,
                    "action": {"payload": {"web_info": {"city_persian": "تهران"}}},
                },
            }
        ],
        "pagination": {"has_next_page": False},
    }
    client = divar_client_factory(FakeSession([FakeResponse(payload)]))
    posts = client.fetch_latest()
    assert len(posts) == 1
    assert posts[0].time_is_exact is False
    assert posts[0].district == "ونک"
    assert posts[0].age_seconds(FIXTURE_NOW) == pytest.approx(7200, abs=1)


def test_rows_without_a_token_are_skipped(divar_client_factory):
    payload = {
        "list_widgets": [
            {"widget_type": "POST_ROW", "data": {"title": "no token here"}},
            {"widget_type": "UNKNOWN_WIDGET", "data": {"token": "x"}},
        ],
        "pagination": {"has_next_page": False},
    }
    client = divar_client_factory(FakeSession([FakeResponse(payload)]))
    assert client.fetch_latest() == []


def test_empty_payload_raises_a_helpful_error(divar_client_factory):
    client = divar_client_factory(FakeSession([FakeResponse({}, 200)]))
    with pytest.raises(DivarError, match="no widgets"):
        client.fetch_latest()


# --------------------------------------------------------------------------- #
# request body
# --------------------------------------------------------------------------- #
def test_search_body_matches_divars_own_shape(settings_factory):
    settings = settings_factory(
        category="mobile-phones", search_query="آیفون ۱۳", price_min=5_000_000, price_max=60_000_000
    )
    client = DivarClient(settings, session=FakeSession(), sleep=lambda _s: None)
    body = client.build_search_body(page=1)
    assert body["city_ids"] == ["1"]
    assert body["search_data"]["query"] == "آیفون ۱۳"
    data = body["search_data"]["form_data"]["data"]
    assert data["category"] == {"str": {"value": "mobile-phones"}}
    assert data["price"] == {"number_range": {"minimum": 5_000_000, "maximum": 60_000_000}}
    assert body["pagination_data"]["@type"] == "type.googleapis.com/post_list.PaginationData"
    assert body["pagination_data"]["page"] == 1
    assert body["pagination_data"]["page_size"] == settings.page_size


def test_category_aliases_are_resolved(settings_factory):
    client = DivarClient(settings_factory(category="mobile"), session=FakeSession(), sleep=lambda _s: None)
    assert client.category == "mobile-phones"
    body = client.build_search_body()
    assert body["search_data"]["form_data"]["data"]["category"]["str"]["value"] == "mobile-phones"
    assert resolve_category("موبایل") == "mobile-phones"
    assert resolve_category("brand-new-slug") == "brand-new-slug"
    assert resolve_category(None) is None


def test_has_photo_filter_is_only_sent_when_enabled(settings_factory):
    client = DivarClient(settings_factory(), session=FakeSession(), sleep=lambda _s: None)
    assert "has-photo" not in client.build_search_body()["search_data"]["form_data"]["data"]
    client = DivarClient(settings_factory(has_photo_only=True), session=FakeSession(), sleep=lambda _s: None)
    assert client.build_search_body()["search_data"]["form_data"]["data"]["has-photo"] == {"boolean": {}}


def test_cursor_is_cleaned_before_being_echoed_back(settings_factory, postlist_payload):
    client = DivarClient(
        settings_factory(pages_per_cycle=2), session=FakeSession(), sleep=lambda _s: None
    )
    page_two = copy.deepcopy(postlist_payload)
    page_two["pagination"]["has_next_page"] = False
    for widget, index in zip(page_two["list_widgets"], range(100, 111)):
        widget["data"]["token"] = f"page2tok{index}"
        widget["action_log"]["server_side_info"]["info"]["post_token"] = f"page2tok{index}"
        widget["data"]["action"]["payload"]["token"] = f"page2tok{index}"
    page_two["action_log"]["server_side_info"]["info"]["posts_metadata"] = [
        dict(item, token=f"page2tok{100 + i}") for i, item in enumerate(page_two["action_log"]["server_side_info"]["info"]["posts_metadata"])
    ]

    session = FakeSession([FakeResponse(postlist_payload), FakeResponse(page_two)])
    client = DivarClient(settings_factory(pages_per_cycle=2), session=session, sleep=lambda _s: None)
    posts = client.fetch_latest()
    assert len(posts) == 22
    assert len(session.calls) == 2

    second_body = session.bodies[1]
    pagination = second_body["pagination_data"]
    assert pagination["page"] == 2
    assert pagination["last_post_date"].startswith("2026-")
    # per-session fields must not be echoed back
    assert "@type" not in pagination or pagination["@type"] == "type.googleapis.com/post_list.PaginationData"
    assert "search_uid" not in pagination
    assert "viewed_tokens" not in pagination


def test_pages_stop_at_the_configured_limit(settings_factory, postlist_payload):
    page_two = copy.deepcopy(postlist_payload)
    session = FakeSession([FakeResponse(postlist_payload), FakeResponse(page_two)])
    client = DivarClient(settings_factory(pages_per_cycle=1), session=session, sleep=lambda _s: None)
    client.fetch_latest()
    assert len(session.calls) == 1


def test_max_posts_per_cycle_caps_the_batch(settings_factory, postlist_payload):
    session = FakeSession([FakeResponse(postlist_payload)])
    client = DivarClient(
        settings_factory(pages_per_cycle=3, max_posts_per_cycle=5), session=session, sleep=lambda _s: None
    )
    assert len(client.fetch_latest()) == 5


# --------------------------------------------------------------------------- #
# headers, rotation and jitter
# --------------------------------------------------------------------------- #
def test_headers_look_like_a_browser(settings_factory, postlist_payload):
    session = FakeSession([FakeResponse(postlist_payload)])
    client = DivarClient(settings_factory(), session=session, sleep=lambda _s: None)
    client.fetch_latest()
    headers = session.calls[0].headers
    assert headers["accept"] == "application/json, text/plain, */*"
    assert headers["origin"] == "https://divar.ir"
    assert headers["referer"] == "https://divar.ir/"
    assert headers["accept-language"].startswith("fa-IR")
    assert headers["content-type"].startswith("application/json")
    assert "Mozilla/5.0" in headers["user-agent"]


def test_user_agent_rotates_between_requests(settings_factory):
    client = DivarClient(settings_factory(), session=FakeSession(), sleep=lambda _s: None)
    agents = {client.headers()["user-agent"] for _ in range(60)}
    assert len(agents) > 3


def test_proxy_is_passed_to_requests(settings_factory, postlist_payload):
    settings = settings_factory(divar_proxy="socks5h://127.0.0.1:10808")
    session = FakeSession([FakeResponse(postlist_payload)])
    DivarClient(settings, session=session, sleep=lambda _s: None).fetch_latest()
    assert session.calls[0].proxies == {
        "http": "socks5h://127.0.0.1:10808",
        "https": "socks5h://127.0.0.1:10808",
    }


def test_jittered_delay_between_requests(settings_factory, postlist_payload):
    settings = settings_factory(request_delay_min=4, request_delay_max=9, pages_per_cycle=3)
    page_two = copy.deepcopy(postlist_payload)
    page_two["pagination"]["has_next_page"] = False
    session = FakeSession([FakeResponse(postlist_payload), FakeResponse(page_two)])
    sleep = RecordingSleep()
    client = DivarClient(settings, session=session, sleep=sleep)
    client.fetch_latest()
    # No delay before the very first request, then one per request.
    assert len(sleep.durations) == 1
    assert 4.0 <= sleep.durations[0] <= 9.0


# --------------------------------------------------------------------------- #
# retries & backoff
# --------------------------------------------------------------------------- #
def test_rate_limit_is_retried_with_retry_after(settings_factory, postlist_payload):
    settings = settings_factory(backoff_max=30, max_retries=3)
    session = FakeSession(
        [FakeResponse({}, 429, headers={"Retry-After": "7"}), FakeResponse(postlist_payload)]
    )
    sleep = RecordingSleep()
    client = DivarClient(settings, session=session, sleep=sleep)
    posts = client.fetch_latest()
    assert posts
    assert len(session.calls) == 2
    # the 429's Retry-After is honoured exactly (sliced into 0.5s chunks)
    assert sleep.total == pytest.approx(7.0, abs=0.01)
    assert max(sleep.durations) <= 0.5 + 1e-9
    assert client.stats["retries"] >= 1


def test_server_errors_are_retried_with_exponential_backoff(settings_factory, postlist_payload):
    settings = settings_factory(max_retries=4, backoff_base=1.0, backoff_max=100.0)
    session = FakeSession(
        [FakeResponse({}, 503), FakeResponse({}, 500), FakeResponse(postlist_payload)]
    )
    sleep = RecordingSleep()
    client = DivarClient(settings, session=session, sleep=sleep)
    assert client.fetch_latest()
    # backoff(0) = 1s + jitter, backoff(1) = 2s + jitter  ->  3..5s total
    assert sleep.total == pytest.approx(sum(d for d in sleep.durations), abs=1e-9)
    assert 3.0 <= sleep.total <= 5.0
    assert len(session.calls) == 3


def test_timeouts_and_connection_errors_are_retried(settings_factory, postlist_payload):
    session = FakeSession(
        [requests.Timeout("timed out"), requests.ConnectionError("reset"), FakeResponse(postlist_payload)]
    )
    client = DivarClient(settings_factory(max_retries=3), session=session, sleep=RecordingSleep())
    assert client.fetch_latest()
    assert len(session.calls) == 3


def test_giving_up_raises_divar_error_not_requests_error(settings_factory):
    session = FakeSession([requests.Timeout("timed out")] * 3)
    client = DivarClient(settings_factory(max_retries=3), session=session, sleep=RecordingSleep())
    with pytest.raises(DivarError, match="giving up"):
        client.fetch_latest()


def test_client_errors_are_not_retried(settings_factory):
    session = FakeSession([FakeResponse({"message": "bad city"}, 400)])
    client = DivarClient(settings_factory(), session=session, sleep=RecordingSleep())
    with pytest.raises(DivarError) as excinfo:
        client.fetch_latest()
    assert excinfo.value.status == 400
    modern_calls = [c for c in session.calls if "postlist" in c.url]
    assert len(modern_calls) == 1  # a client error is never retried


def test_non_json_response_is_retried_then_fails(settings_factory):
    session = FakeSession([FakeResponse(None, 200, text="<html>captcha</html>")] * 3)
    client = DivarClient(settings_factory(max_retries=3), session=session, sleep=RecordingSleep())
    with pytest.raises(DivarError, match="non-JSON"):
        client.fetch_latest()


def test_shutdown_interrupts_the_backoff(settings_factory):
    stop = {"flag": False}
    session = FakeSession([FakeResponse({}, 503)] * 5)

    def sleep(_seconds):
        stop["flag"] = True  # the operator pressed Ctrl-C while we were waiting

    client = DivarClient(
        settings_factory(max_retries=5, backoff_base=1.0),
        session=session,
        sleep=sleep,
        should_stop=lambda: stop["flag"],
    )
    with pytest.raises(ShutdownRequested):
        client.fetch_latest()
    assert len(session.calls) < 5


# --------------------------------------------------------------------------- #
# endpoint fallback
# --------------------------------------------------------------------------- #
def test_falls_back_to_the_legacy_endpoint(settings_factory, legacy_payload):
    session = FakeSession([FakeResponse({"message": "not found"}, 404), FakeResponse(legacy_payload)])
    client = DivarClient(settings_factory(), session=session, sleep=RecordingSleep())
    posts = client.fetch_latest()
    assert posts, "legacy rows should still produce posts"
    assert client.stats["legacy_pages"] == 1
    assert session.calls[1].url.endswith("/v8/search/1/mobile-phones")
    first = next(p for p in posts if p.token == "aDeaL001")
    assert first.price == 45_000_000


def test_sticky_endpoint_avoids_a_second_probe(settings_factory, postlist_payload, legacy_payload):
    session = FakeSession(
        [
            FakeResponse({"message": "gone"}, 410),
            FakeResponse(legacy_payload),
            FakeResponse(legacy_payload),
        ]
    )
    client = DivarClient(settings_factory(pages_per_cycle=1), session=session, sleep=RecordingSleep())
    client.fetch_latest()
    client.fetch_latest()
    assert all("/v8/search/" in call.url for call in session.calls[1:])


def test_modern_only_mode_never_falls_back(settings_factory):
    session = FakeSession([FakeResponse({"message": "nope"}, 404)])
    client = DivarClient(settings_factory(divar_endpoint_mode="modern"), session=session, sleep=RecordingSleep())
    with pytest.raises(DivarError):
        client.fetch_latest()
    assert len(session.calls) == 1


def test_legacy_mode_posts_directly(settings_factory, legacy_payload):
    session = FakeSession([FakeResponse(legacy_payload)])
    client = DivarClient(settings_factory(divar_endpoint_mode="legacy"), session=session, sleep=RecordingSleep())
    posts = client.fetch_latest()
    assert posts
    assert session.calls[0].url.endswith("/v8/search/1/mobile-phones")
    body = session.bodies[0]
    assert body["json_schema"]["category"] == {"value": "mobile-phones"}
    assert isinstance(body["last-post-date"], int)


# --------------------------------------------------------------------------- #
# detail enrichment
# --------------------------------------------------------------------------- #
def test_detail_payload_is_parsed(divar_client_factory, detail_payload):
    client = divar_client_factory(FakeSession([FakeResponse(detail_payload)]))
    detail = client.fetch_detail("aDeaL001")
    assert detail["price"] == 45_000_000
    assert detail["category"] == "mobile-phones"
    assert detail["business_type"] == "personal"
    assert detail["condition"] == "used"
    assert detail["brand_model"] == "apple iphone 13 pro max"
    assert "سالم" in detail["description"]
    assert detail["attributes"]["برند"] == "Apple"
    assert len(detail["images"]) == 2
    assert detail["posted_at"].startswith("۳۰ شهریور ۱۴۰۵")


def test_enrich_merges_detail_into_the_row(sample_posts, divar_client_factory, detail_payload):
    post = sample_posts(token="aDeaL001", price=None, price_text="پرداخت توافقی", description=None)
    client = divar_client_factory(FakeSession([FakeResponse(detail_payload)]))
    assert client.enrich([post]) == 1
    assert post.description is not None
    assert post.price == 45_000_000
    assert post.business_type == "personal"
    assert post.image_count == 2


def test_enrich_survives_a_failed_detail_lookup(sample_posts, divar_client_factory):
    session = FakeSession([FakeResponse({"message": "gone"}, 404)])
    client = divar_client_factory(session)
    post = sample_posts(price=45_000_000)
    assert client.enrich([post]) == 0
    assert post.price == 45_000_000  # untouched, alert can still go out


# --------------------------------------------------------------------------- #
# misc helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-09-21T20:36:10.203722Z", datetime(2026, 9, 21, 20, 36, 10, 203722, tzinfo=timezone.utc)),
        ("1790022970203722", datetime(2026, 9, 21, 20, 36, 10, 203722, tzinfo=timezone.utc)),
        (1790022970, datetime(2026, 9, 21, 20, 36, 10, tzinfo=timezone.utc)),
        (1790022970203, datetime(2026, 9, 21, 20, 36, 10, 203722, tzinfo=timezone.utc)),
        # Jalali text is Tehran-local midnight -> previous day in UTC
        ("۳۰ شهریور ۱۴۰۵، ۰۰:۰۵", datetime(2026, 9, 20, 20, 35, tzinfo=timezone.utc)),
        ("", None),
        (None, None),
        ("not a date", None),
    ],
)
def test_parse_timestamp_forms(value, expected):
    parsed = parse_timestamp(value)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert abs((parsed - expected).total_seconds()) < 1.5


def test_probe_reports_a_summary(divar_client_factory, postlist_payload):
    client = divar_client_factory(FakeSession([FakeResponse(postlist_payload)]))
    summary = client.probe()
    assert summary.startswith("OK via modern")
    assert "11 rows" in summary


def test_city_map_is_consistent():
    assert CITY_IDS["تهران"] == "1"
    assert CITY_SLUGS["tehran"] == "1"
    known = list_known_cities()
    assert ("tehran", "1", "تهران") in known
    # every slug points at an id that also has a Persian name
    assert all(name for _slug, _id, name in known)


def test_close_is_idempotent(settings_factory):
    session = FakeSession()
    client = DivarClient(settings_factory(), session=session, sleep=lambda _s: None)
    client.close()
    client.close()
    assert session.closed
