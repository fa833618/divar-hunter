"""Tests for deal detection: price resolution and threshold rules."""

from __future__ import annotations

import pytest

from conftest import FIXTURE_NOW
from filters import CycleStats, DealFilter, PriceDecision


@pytest.fixture
def deal_filter(settings):
    return DealFilter(settings, now=lambda: FIXTURE_NOW)


def by_token(posts):
    return {post.token: post for post in posts}


# --------------------------------------------------------------------------- #
# the fixture page, judged case by case
# --------------------------------------------------------------------------- #
def test_clear_deal_is_accepted(fixture_posts, deal_filter):
    verdict = deal_filter.evaluate(by_token(fixture_posts)["aDeaL001"])
    assert verdict.is_deal is True
    assert verdict.price.value == 45_000_000
    assert verdict.score == 10  # 10% below the 50M ceiling
    assert any("زیر سقف" in reason for reason in verdict.reasons)


def test_price_above_the_ceiling_is_rejected(fixture_posts, deal_filter):
    verdict = deal_filter.evaluate(by_token(fixture_posts)["bExpns02"])
    assert verdict.is_deal is False
    assert "above threshold" in verdict.explain()


@pytest.mark.parametrize(
    "token,label",
    [
        ("cNoPrc03", "no_price:negotiable"),
        ("fFreez06", "no_price:free"),
        ("iSwapx09", "no_price:exchange"),
    ],
)
def test_unusable_prices_are_rejected(fixture_posts, deal_filter, token, label):
    stats = CycleStats()
    verdict = deal_filter.evaluate(by_token(fixture_posts)[token], stats)
    assert verdict.is_deal is False
    assert "no usable price" in verdict.rejections[0]
    assert label in stats.rejection_reasons  # the counter keeps the machine label


def test_price_below_the_floor_is_treated_as_suspicious(fixture_posts, deal_filter):
    verdict = deal_filter.evaluate(by_token(fixture_posts)["eCheap05"])
    assert verdict.is_deal is False
    assert "below floor" in verdict.explain()


def test_down_payment_listing_is_skipped_by_default(fixture_posts, deal_filter):
    verdict = deal_filter.evaluate(by_token(fixture_posts)["dDownP04"])
    assert verdict.is_deal is False
    assert "down payment" in verdict.explain()
    assert "پیش‌پرداخت" in verdict.explain()


def test_down_payment_listing_can_be_allowed_with_a_warning(fixture_posts, settings):
    permissive = DealFilter(settings.with_updates(skip_down_payment_ads=False), now=lambda: FIXTURE_NOW)
    verdict = permissive.evaluate(by_token(fixture_posts)["dDownP04"])
    assert verdict.is_deal is True
    assert verdict.price.value == 20_000_000
    assert any("پیش‌پرداخت" in warning for warning in verdict.warnings)


def test_total_price_is_recovered_from_the_title(fixture_posts, deal_filter):
    """kMiDec11 advertises a down payment but states the total in its title."""
    verdict = deal_filter.evaluate(by_token(fixture_posts)["kMiDec11"])
    assert verdict.is_deal is True
    assert verdict.price.value == 32_000_000
    assert verdict.price.source == "total_recovered"
    assert verdict.price.is_down_payment is False
    assert any("قیمت کل" in warning for warning in verdict.warnings)


def test_recovered_total_can_push_a_listing_over_the_ceiling(sample_posts, settings):
    post = sample_posts(
        token="over1", title="فروش خودرو", price=20_000_000,
        price_text="پیش‌پرداخت ۲۰٬۰۰۰٬۰۰۰ تومان",
        description="قیمت کل ۹۰۰٬۰۰۰٬۰۰۰ تومان",
    )
    verdict = DealFilter(settings, now=lambda: FIXTURE_NOW).evaluate(post)
    assert verdict.price.value == 900_000_000
    assert verdict.is_deal is False
    assert "above threshold" in verdict.explain()


def test_deposit_only_rental_listing_is_skipped(fixture_posts, deal_filter):
    verdict = deal_filter.evaluate(by_token(fixture_posts)["jDepst10"])
    assert verdict.is_deal is False
    assert "ودیعه" in verdict.explain()


def test_stale_listings_are_dropped_when_an_age_limit_is_set(fixture_posts, settings):
    strict = DealFilter(settings.with_updates(max_age_minutes=120), now=lambda: FIXTURE_NOW)
    verdict = strict.evaluate(by_token(fixture_posts)["hStale08"])  # 3 hours old
    assert verdict.is_deal is False
    assert "older than" in verdict.explain()

    fresh = strict.evaluate(by_token(fixture_posts)["aDeaL001"])  # 1 minute old
    assert fresh.is_deal is True


def test_unknown_age_produces_a_warning_not_a_rejection(sample_posts, settings):
    post = sample_posts(published_at=None)
    verdict = DealFilter(settings.with_updates(max_age_minutes=60), now=lambda: FIXTURE_NOW).evaluate(post)
    assert verdict.is_deal is True
    assert any("نامشخص" in warning for warning in verdict.warnings)


# --------------------------------------------------------------------------- #
# keyword & seller rules
# --------------------------------------------------------------------------- #
def test_exclude_keywords_reject(fixture_posts, settings):
    strict = DealFilter(
        settings.with_updates(exclude_keywords=["خراب", "قطعات"]), now=lambda: FIXTURE_NOW
    )
    assert strict.evaluate(by_token(fixture_posts)["gParts07"]).is_deal is False
    # a clean listing is unaffected
    assert strict.evaluate(by_token(fixture_posts)["aDeaL001"]).is_deal is True


def test_include_keywords_are_required_when_set(fixture_posts, settings):
    picky = DealFilter(settings.with_updates(include_keywords=["آیفون"]), now=lambda: FIXTURE_NOW)
    assert picky.evaluate(by_token(fixture_posts)["aDeaL001"]).is_deal is True
    assert picky.evaluate(by_token(fixture_posts)["bExpns02"]).is_deal is False


def test_keyword_matching_looks_at_the_description_too(sample_posts, settings):
    post = sample_posts(title="گوشی سالم", description="برند سامسونگ، در حد نو")
    verdict = DealFilter(
        settings.with_updates(include_keywords=["سامسونگ"]), now=lambda: FIXTURE_NOW
    ).evaluate(post)
    assert verdict.is_deal is True


def test_only_personal_ads_setting(sample_posts, settings):
    dealer = sample_posts(business_type="business")
    private = sample_posts(business_type="personal")
    strict = DealFilter(settings.with_updates(only_personal_ads=True), now=lambda: FIXTURE_NOW)
    assert strict.evaluate(dealer).is_deal is False
    assert strict.evaluate(private).is_deal is True
    # unknown seller type must not be rejected outright
    assert strict.evaluate(sample_posts(token="unknown1")).is_deal is True


def test_has_photo_only_setting(sample_posts, settings):
    strict = DealFilter(settings.with_updates(has_photo_only=True), now=lambda: FIXTURE_NOW)
    assert strict.evaluate(sample_posts(image_count=0, image_url=None)).is_deal is False
    assert strict.evaluate(sample_posts(image_count=3)).is_deal is True


# --------------------------------------------------------------------------- #
# price resolution details
# --------------------------------------------------------------------------- #
def test_price_is_taken_from_the_description_when_the_api_has_none(sample_posts, settings):
    post = sample_posts(price=None, price_text="پرداخت توافقی",
                        description="فوری، قیمت ۴۵٬۰۰۰٬۰۰۰ تومان")
    deal_filter = DealFilter(settings, now=lambda: FIXTURE_NOW)
    decision = deal_filter.resolve_price(post)
    assert decision.value == 45_000_000
    assert decision.source == "description"
    assert decision.warnings
    assert deal_filter.evaluate(post).is_deal is True


def test_require_price_false_lets_negotiable_listings_through(sample_posts, settings):
    post = sample_posts(price=None, price_text="پرداخت توافقی")
    permissive = DealFilter(settings.with_updates(require_price=False), now=lambda: FIXTURE_NOW)
    verdict = permissive.evaluate(post)
    assert verdict.is_deal is True
    assert verdict.price.value is None
    assert verdict.price.display == "توافقی"


def test_zero_price_is_treated_as_missing(sample_posts, deal_filter):
    post = sample_posts(price=0, price_text="")
    decision = deal_filter.resolve_price(post)
    assert decision.value is None
    assert decision.note == "negotiable"


def test_no_ceiling_configured_still_produces_a_deal(sample_posts, settings):
    open_ended = DealFilter(settings.with_updates(deal_max_price=None), now=lambda: FIXTURE_NOW)
    verdict = open_ended.evaluate(sample_posts())
    assert verdict.is_deal is True
    assert verdict.score == 50


def test_price_display_and_detail_line(settings):
    decision = PriceDecision(value=45_000_000, source="api_text", raw_text="۴۵٬۰۰۰٬۰۰۰ تومان")
    assert decision.display == "۴۵ میلیون تومان"
    assert "متن اصلی" in decision.detail_line()
    assert PriceDecision(value=None, note="free").display == "رایگان"
    assert PriceDecision(value=None).display == "بدون قیمت"


# --------------------------------------------------------------------------- #
# batch filtering & stats
# --------------------------------------------------------------------------- #
def test_filter_posts_returns_deals_best_first(fixture_posts, deal_filter):
    deals, stats = deal_filter.filter_posts(fixture_posts)
    assert stats.fetched == len(fixture_posts)
    assert stats.deals == len(deals)
    assert stats.rejected == stats.fetched - stats.deals
    scores = [verdict.score for verdict in deals]
    assert scores == sorted(scores, reverse=True)
    assert deals[0].post.token == "gParts07"  # cheapest relative to the ceiling


def test_stats_summary_lists_the_top_rejection_reasons(fixture_posts, deal_filter):
    _deals, stats = deal_filter.filter_posts(fixture_posts)
    summary = stats.summary()
    assert "fetched=11" in summary
    assert "no_price" in summary
    assert isinstance(stats.rejection_reasons, dict)


def test_verdict_explain_and_summary_are_readable(fixture_posts, deal_filter):
    deal = deal_filter.evaluate(by_token(fixture_posts)["aDeaL001"])
    assert deal.explain().startswith("DEAL: ")
    assert deal.summary_fa().startswith("💰")

    rejected = deal_filter.evaluate(by_token(fixture_posts)["bExpns02"])
    assert rejected.explain().startswith("skip: ")
    assert rejected.to_dict()["is_deal"] is False


def test_empty_batch(fixture_posts, deal_filter):
    deals, stats = deal_filter.filter_posts([])
    assert deals == []
    assert stats.fetched == 0


def test_cycle_stats_defaults():
    stats = CycleStats()
    stats.note_rejection("above_threshold")
    stats.note_rejection("above_threshold")
    stats.note_rejection("no_price")
    assert stats.rejection_reasons == {"above_threshold": 2, "no_price": 1}
    assert "rejected=0" in stats.summary()
