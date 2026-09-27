"""Tests for the Telegram alerting engine."""

from __future__ import annotations

import pytest
import requests

from conftest import FIXTURE_NOW, FakeResponse, FakeSession, RecordingSleep
from filters import DealFilter, PriceDecision, Verdict
from divar_client import DivarPost
from notifier import (
    CATEGORY_NAMES_FA,
    DryRunNotifier,
    TelegramError,
    TelegramNotifier,
    build_alert_text,
    build_notifier,
    build_summary_message,
    escape_markdown,
    _split_chat_ids,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def deal_verdict(settings):
    post = DivarPost(
        token="aDeaL001",
        title="آیفون ۱۳ پرومکس ۲۵۶ گیگ سالم",
        price=45_000_000,
        price_text="۴۵٬۰۰۰٬۰۰۰ تومان",
        city="تهران",
        district="سعادت‌آباد",
        category="mobile-phones",
        published_at=FIXTURE_NOW,
        time_is_exact=True,
        image_count=4,
        has_chat=True,
        description="کاملاً سالم، با جعبه و لوازم. تخفیف جزئی پای معامله.",
        condition="used",
        brand_model="apple iphone 13 pro max",
    )
    return DealFilter(settings, now=lambda: FIXTURE_NOW).evaluate(post)


# --------------------------------------------------------------------------- #
# escaping & message content
# --------------------------------------------------------------------------- #
def test_escape_markdown_escapes_every_special_character():
    raw = "_*[]()~`>#+-=|{}.!"
    escaped = escape_markdown(raw)
    assert escaped == "".join("\\" + char for char in raw)
    assert escape_markdown("سلام دنیا") == "سلام دنیا"
    assert escape_markdown(None) == ""


def test_alert_message_carries_every_required_field(deal_verdict, settings):
    message = build_alert_text(deal_verdict, settings, now=FIXTURE_NOW)
    for label in (
        "📌 عنوان آگهی",
        "💰 قیمت اعلامی",
        "📍 محل / محله",
        "🕒 زمان انتشار",
        "🔗 لینک مستقیم",
    ):
        assert label.replace(" ", "\\ ") in message.markdown or label in message.plain
    assert "آیفون ۱۳ پرومکس ۲۵۶ گیگ سالم" in message.plain
    assert "۴۵ میلیون تومان" in message.plain
    assert "تهران، سعادت‌آباد" in message.plain
    assert "https://divar.ir/v/aDeaL001" in message.plain
    assert "موبایل" in message.plain  # category translated to Persian
    assert "۱۴۰۵" in message.plain     # Jalali publish date
    assert "چرا معامله است" in message.plain


def test_markdown_and_plain_twins_differ_only_in_markup(deal_verdict, settings):
    message = build_alert_text(deal_verdict, settings, now=FIXTURE_NOW)
    assert "\\" not in message.plain
    assert "*" not in message.plain
    assert message.markdown.count("*") % 2 == 0  # bold markers are balanced
    assert escape_markdown(deal_verdict.post.title) in message.markdown


def test_user_content_is_escaped_not_interpreted(settings):
    hostile = DivarPost(
        token="esc1", title="_[خطر](http://evil)_ *bold* ~strike~",
        price=10_000_000, price_text="۱۰٬۰۰۰٬۰۰۰ تومان",
        published_at=FIXTURE_NOW, time_is_exact=True,
    )
    verdict = Verdict(
        post=hostile, is_deal=True,
        price=PriceDecision(value=10_000_000, source="api_text"),
        reasons=["زیر سقف"],
    )
    message = build_alert_text(verdict, settings, now=FIXTURE_NOW)
    assert "](http" not in message.markdown  # the link syntax was escaped, not parsed
    assert escape_markdown(hostile.title) in message.markdown


def test_inline_keyboard_has_the_persian_button(deal_verdict, settings):
    message = build_alert_text(deal_verdict, settings, now=FIXTURE_NOW)
    markup = message.inline_keyboard
    assert markup["inline_keyboard"][0][0]["text"] == "مشاهده آگهی در دیوار"
    assert markup["inline_keyboard"][0][0]["url"] == "https://divar.ir/v/aDeaL001"


def test_long_descriptions_are_truncated(settings):
    post = DivarPost(
        token="long1", title="ع" * 500, price=10_000_000, price_text="۱۰ میلیون تومان",
        description="ت" * 9000, published_at=FIXTURE_NOW, time_is_exact=True,
    )
    verdict = Verdict(post=post, is_deal=True,
                      price=PriceDecision(value=10_000_000, source="api_text"),
                      reasons=["r" * 500] * 10)
    message = build_alert_text(verdict, settings, now=FIXTURE_NOW)
    assert len(message.markdown) <= 4096
    assert len(message.plain) <= 4096


def test_approximate_publish_time_is_labeled(deal_verdict, settings):
    deal_verdict.post.time_is_exact = False
    deal_verdict.post.time_text = "۲ ساعت پیش"
    message = build_alert_text(deal_verdict, settings, now=FIXTURE_NOW)
    assert "تقریبی" in message.plain
    assert "۲ ساعت پیش" in message.plain


def test_summary_message_lists_the_overflow(deal_verdict, settings):
    text = build_summary_message([deal_verdict, deal_verdict], settings)
    link = escape_markdown("https://divar.ir/v/aDeaL001")
    assert text.count(link) == 2
    assert "سایر معاملات" in text.replace("\\", "")


def test_category_names_are_translated(settings):
    post = DivarPost(token="c1", title="t", price=10_000_000, category="tablet",
                     published_at=FIXTURE_NOW, time_is_exact=True)
    verdict = Verdict(post=post, is_deal=True, price=PriceDecision(value=10_000_000))
    assert "تبلت" in build_alert_text(verdict, settings, now=FIXTURE_NOW).plain
    assert CATEGORY_NAMES_FA["mobile-phones"] == "موبایل"


# --------------------------------------------------------------------------- #
# HTTP behaviour
# --------------------------------------------------------------------------- #
def ok(result=None):
    return FakeResponse({"ok": True, "result": result if result is not None else {"message_id": 1}})


def build_notifier_with(session, settings=None, settings_factory=None, sleep=None):
    settings = settings or settings_factory(
        dry_run=False, telegram_bot_token="123:TOKEN", telegram_chat_id="42"
    )
    return TelegramNotifier(settings, session=session, sleep=sleep or RecordingSleep())


def test_send_message_posts_to_the_bot_api(settings_factory):
    session = FakeSession([ok()])
    notifier = build_notifier_with(session, settings_factory=settings_factory)
    assert notifier.send_message("سلام", parse_mode=None) is True
    call = session.calls[0]
    assert call.url == "https://api.telegram.org/bot123:TOKEN/sendMessage"
    assert call.json["chat_id"] == "42"
    assert call.json["text"] == "سلام"
    assert call.timeout == settings_factory().telegram_timeout


def test_proxy_is_used_for_telegram(settings_factory):
    session = FakeSession([ok()])
    settings = settings_factory(dry_run=False, telegram_proxy="socks5h://127.0.0.1:10808")
    TelegramNotifier(settings, session=session, sleep=RecordingSleep()).send_message("hi", parse_mode=None)
    assert session.calls[0].proxies == {
        "http": "socks5h://127.0.0.1:10808",
        "https": "socks5h://127.0.0.1:10808",
    }


def test_several_chat_ids_all_receive_the_alert(settings_factory):
    session = FakeSession([ok(), ok(), ok()])
    settings = settings_factory(dry_run=False, telegram_chat_id="42, @channel,-100123")
    notifier = TelegramNotifier(settings, session=session, sleep=RecordingSleep())
    assert notifier.send_message("hi", parse_mode=None) is True
    assert [call.json["chat_id"] for call in session.calls] == ["42", "@channel", "-100123"]


def test_alert_sends_markdown_with_the_inline_keyboard(settings_factory, deal_verdict):
    session = FakeSession([ok()])
    notifier = build_notifier_with(session, settings_factory=settings_factory)
    settings = settings_factory(dry_run=False)
    assert notifier.alert(deal_verdict, settings, now=FIXTURE_NOW) is True
    payload = session.calls[0].json
    assert payload["parse_mode"] == "MarkdownV2"
    assert payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "مشاهده آگهی در دیوار"
    assert payload["disable_web_page_preview"] is True


def test_parse_errors_fall_back_to_plain_text(settings_factory, deal_verdict):
    session = FakeSession(
        [
            FakeResponse({"ok": False, "error_code": 400,
                          "description": "Bad Request: can't parse entities: unexpected end tag"}, 400),
            ok(),
        ]
    )
    notifier = build_notifier_with(session, settings_factory=settings_factory)
    settings = settings_factory(dry_run=False)
    assert notifier.alert(deal_verdict, settings, now=FIXTURE_NOW) is True
    assert len(session.calls) == 2
    assert "parse_mode" not in session.calls[1].json  # second try is plain text
    assert notifier.stats["plain_fallback"] == 1


def test_rate_limit_is_retried_using_retry_after(settings_factory):
    session = FakeSession(
        [
            FakeResponse({"ok": False, "error_code": 429, "description": "Too Many Requests",
                          "parameters": {"retry_after": 3}}, 429),
            ok(),
        ]
    )
    sleep = RecordingSleep()
    notifier = build_notifier_with(session, settings_factory=settings_factory, sleep=sleep)
    assert notifier.send_message("hi", parse_mode=None) is True
    assert sleep.total == pytest.approx(3.0, abs=0.01)


def test_server_errors_are_retried_then_succeed(settings_factory):
    session = FakeSession([FakeResponse({"ok": False, "error_code": 502, "description": "bad gateway"}, 502), ok()])
    notifier = build_notifier_with(session, settings_factory=settings_factory)
    assert notifier.send_message("hi", parse_mode=None) is True
    assert notifier.stats["retries"] >= 1


def test_bad_token_fails_fast(settings_factory):
    unauthorized = FakeResponse({"ok": False, "error_code": 401, "description": "Unauthorized"}, 401)
    session = FakeSession([unauthorized, unauthorized])
    notifier = build_notifier_with(session, settings_factory=settings_factory)
    # send_message reports failure, call() raises — the loop must never crash
    assert notifier.send_message("hi", parse_mode=None) is False
    assert len(session.calls) == 1  # no pointless retries for a bad token
    assert notifier.stats["failed"] == 1
    with pytest.raises(TelegramError) as excinfo:
        notifier.call("getMe", {})
    assert excinfo.value.status == 401


def test_giving_up_reports_failure(settings_factory):
    session = FakeSession([requests.ConnectionError("no route to api.telegram.org")] * 3)
    notifier = build_notifier_with(session, settings_factory=settings_factory)
    assert notifier.send_message("hi", parse_mode=None) is False
    assert len(session.calls) == 3  # it did retry a transport failure
    assert notifier.stats["failed"] == 1


def test_alert_spacing_throttles_sends(settings_factory):
    session = FakeSession([ok(), ok()])
    settings = settings_factory(dry_run=False, alert_spacing_seconds=1.5)
    sleep = RecordingSleep()
    notifier = TelegramNotifier(settings, session=session, sleep=sleep)
    notifier.send_message("a", parse_mode=None)
    notifier.send_message("b", parse_mode=None)
    assert sleep.total == pytest.approx(1.5, abs=0.01)


def test_missing_chat_id_is_an_error(settings_factory):
    settings = settings_factory(dry_run=False, telegram_chat_id="")
    notifier = TelegramNotifier(settings, session=FakeSession(), sleep=RecordingSleep())
    with pytest.raises(TelegramError, match="TELEGRAM_CHAT_ID"):
        notifier.send_message("hi")


# --------------------------------------------------------------------------- #
# verify / factory / dry run
# --------------------------------------------------------------------------- #
def test_verify_checks_the_token_and_the_chat(settings_factory):
    session = FakeSession([ok({"username": "deal_hunter_bot", "id": 1}),
                           ok({"title": "Deals", "type": "channel"})])
    notifier = build_notifier_with(session, settings_factory=settings_factory)
    report = notifier.verify()
    assert "@deal_hunter_bot" in report
    assert "reachable" in report
    assert session.calls[0].url.endswith("/getMe")
    assert session.calls[1].url.endswith("/getChat")


def test_verify_reports_an_unreachable_chat(settings_factory):
    session = FakeSession(
        [ok({"username": "bot"}), FakeResponse({"ok": False, "error_code": 400,
                                                "description": "Bad Request: chat not found"}, 400)]
    )
    notifier = build_notifier_with(session, settings_factory=settings_factory)
    with pytest.raises(TelegramError) as excinfo:
        notifier.verify()
    assert "NOT reachable" in str(excinfo.value)
    assert "/start" in str(excinfo.value)


def test_dry_run_notifier_logs_instead_of_sending(settings_factory, deal_verdict, caplog):
    settings = settings_factory(dry_run=True)
    notifier = DryRunNotifier(settings)
    with caplog.at_level("INFO", logger="divar.notifier"):
        assert notifier.alert(deal_verdict, settings, now=FIXTURE_NOW) is True
    assert len(notifier.sent) == 1
    assert "آیفون" in notifier.sent[0]
    assert "DRY-RUN alert" in caplog.text
    assert "dry-run" in notifier.verify()


def test_build_notifier_picks_the_right_implementation(settings_factory):
    assert isinstance(build_notifier(settings_factory(dry_run=True)), DryRunNotifier)
    assert isinstance(build_notifier(settings_factory(dry_run=False)), TelegramNotifier)
    # missing credentials degrade to dry-run instead of crashing
    fallback = build_notifier(settings_factory(dry_run=False, telegram_bot_token="", telegram_chat_id=""))
    assert isinstance(fallback, DryRunNotifier)


def test_chat_id_splitting_handles_persian_commas():
    assert _split_chat_ids("1, 2،3") == ["1", "2", "3"]
    assert _split_chat_ids("") == []
    assert _split_chat_ids("@chan") == ["@chan"]


def test_notifier_close_is_safe(settings_factory):
    session = FakeSession()
    notifier = build_notifier_with(session, settings_factory=settings_factory)
    notifier.close()
    assert session.closed
