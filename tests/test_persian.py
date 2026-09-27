"""Tests for the Persian price/number/date helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from persian import (
    extract_price_from_text,
    extract_total_price,
    find_keywords,
    format_datetime_fa,
    format_number_fa,
    gregorian_to_jalali,
    human_toman,
    is_down_payment_text,
    is_installment_text,
    jalali_to_gregorian,
    normalize_for_match,
    parse_jalali_datetime,
    parse_price,
    parse_relative_time,
    relative_time_fa,
    split_time_and_place,
    to_ascii_digits,
    to_persian_digits,
)


# --------------------------------------------------------------------------- #
# digits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [("۱۲۳۴۵۶۷۸۹۰", "1234567890"), ("٠١٢٣٤٥٦٧٨٩", "0123456789"), ("abc123", "abc123"), (None, "")],
)
def test_to_ascii_digits(raw, expected):
    assert to_ascii_digits(raw) == expected


def test_to_persian_digits_roundtrip():
    assert to_persian_digits("45000000") == "۴۵۰۰۰۰۰۰"
    assert to_ascii_digits(to_persian_digits("45000000")) == "45000000"


def test_format_number_fa_uses_persian_separator():
    assert format_number_fa(85_000_000) == "۸۵٬۰۰۰٬۰۰۰"
    assert format_number_fa(0) == "۰"
    assert format_number_fa(None) == "—"


@pytest.mark.parametrize(
    "value,expected",
    [
        (450_000_000, "۴۵۰ میلیون تومان"),
        (1_500_000_000, "۱٫۵ میلیارد تومان"),
        (2_000_000_000, "۲ میلیارد تومان"),
        (85_000_000, "۸۵ میلیون تومان"),
        (450_000, "۴۵۰٬۰۰۰ تومان"),
        (None, "—"),
    ],
)
def test_human_toman(value, expected):
    assert human_toman(value) == expected


def test_normalize_for_match_folds_letters_digits_and_zwnj():
    assert normalize_for_match("آیفون‌ ۱۳ پرومکس") == "ایفون 13 پرومکس"
    assert normalize_for_match("سامسونگ گلکسي") == "سامسونگ گلکسی"


# --------------------------------------------------------------------------- #
# price parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("۸۵٬۰۰۰٬۰۰۰ تومان", 85_000_000),
        ("85,000,000 تومان", 85_000_000),
        ("۴۵۰ میلیون تومان", 450_000_000),
        ("۴۵۰میلیون", 450_000_000),
        ("۱.۵ میلیارد تومان", 1_500_000_000),
        ("۲ میلیارد و ۳۰۰ میلیون تومان", 2_300_000_000),
        ("۴۵۰ هزار تومان", 450_000),
        ("۱۲۰۰۰۰۰۰۰", 120_000_000),
        ("۱٬۲۰۰٬۰۰۰ تومان", 1_200_000),
    ],
)
def test_parse_price_numbers(text, expected):
    assert parse_price(text).value == expected


@pytest.mark.parametrize(
    "text,note",
    [
        ("پرداخت توافقی", "negotiable"),
        ("توافقی", "negotiable"),
        ("قیمت تماسی", "negotiable"),
        ("رایگان", "free"),
        ("معاوضه با خودرو", "exchange"),
        ("تهاتر", "exchange"),
    ],
)
def test_parse_price_notes(text, note):
    parsed = parse_price(text)
    assert parsed.value is None
    assert parsed.note == note


def test_parse_price_down_payment_and_installment_flags():
    parsed = parse_price("پیش‌پرداخت ۲۰٬۰۰۰٬۰۰۰ تومان")
    assert parsed.value == 20_000_000
    assert parsed.is_down_payment is True
    assert parsed.is_installment is False

    installments = parse_price("قسطی ۵ میلیون تومان")
    assert installments.value == 5_000_000
    assert installments.is_installment is True

    deposit = parse_price("ودیعه ۱۰۰٬۰۰۰٬۰۰۰ تومان")
    assert deposit.value == 100_000_000
    assert deposit.is_deposit is True
    assert deposit.note == "deposit_rent"


def test_parse_price_starting_price_note():
    parsed = parse_price("قیمت از ۱٬۰۰۰٬۰۰۰ تومان")
    assert parsed.value == 1_000_000
    assert parsed.note == "from_price"


@pytest.mark.parametrize("text", ["", None, "   ", "بدون قیمت"])
def test_parse_price_no_information(text):
    parsed = parse_price(text)
    assert parsed.value is None
    assert parsed.usable is False


def test_parsed_price_display_for_non_numeric():
    assert parse_price("رایگان").human() == "رایگان"
    assert parse_price("توافقی").human() == "توافقی"
    assert parse_price("معاوضه").human() == "معاوضه"


# --------------------------------------------------------------------------- #
# down-payment recovery
# --------------------------------------------------------------------------- #
def test_extract_total_price_explicit_total():
    text = "پیش‌پرداخت ۲۰۰ میلیون تومان، قیمت کل: ۱٬۲۰۰٬۰۰۰٬۰۰۰ تومان"
    assert extract_total_price(text) == 1_200_000_000


def test_extract_total_price_from_remaining_and_known_down_payment():
    text = "پیش پرداخت ۲۰۰ میلیون، مابقی ۸۰۰ میلیون تومان"
    assert extract_total_price(text, down_payment=200_000_000) == 1_000_000_000


def test_extract_total_price_refuses_to_guess():
    assert extract_total_price("مابقی طی چک پرداخت می‌شود") is None
    assert extract_total_price("فروش نقدی، تخفیف جزئی") is None
    assert extract_total_price(None) is None


def test_extract_price_from_text_needs_a_keyword():
    # A model number must not be mistaken for a price.
    assert extract_price_from_text("آیفون ۱۳ پرومکس ۲۵۶ گیگ") is None
    assert extract_price_from_text("قیمت: ۴۵٬۰۰۰٬۰۰۰ تومان") == 45_000_000
    assert extract_price_from_text("مبلغ ۳۲ میلیون تومان") == 32_000_000


def test_is_down_payment_and_installment_text():
    assert is_down_payment_text("پیش‌پرداخت ۲۰۰ میلیون") is True
    assert is_down_payment_text("فروش نقدی") is False
    assert is_installment_text("فروش اقساطی با چک صیادی") is True
    assert is_installment_text("نقد") is False


# --------------------------------------------------------------------------- #
# relative time & place
# --------------------------------------------------------------------------- #
def test_split_time_and_place():
    assert split_time_and_place("لحظاتی پیش در بلوار کشاورز") == ("لحظاتی پیش", "بلوار کشاورز")
    assert split_time_and_place("دیروز در تهران") == ("دیروز", "تهران")
    assert split_time_and_place("") == ("", "")
    assert split_time_and_place(None) == ("", "")


@pytest.mark.parametrize(
    "text,expected_minutes",
    [
        ("لحظاتی پیش", 0),
        ("دقایقی پیش", 0),
        ("۵ دقیقه پیش", 5),
        ("۲ ساعت پیش", 120),
        ("۳ روز پیش", 3 * 24 * 60),
        ("دیروز", 24 * 60),
        ("پریروز", 48 * 60),
        ("۱ هفته پیش", 7 * 24 * 60),
    ],
)
def test_parse_relative_time(text, expected_minutes):
    assert parse_relative_time(text) == timedelta(minutes=expected_minutes)


def test_parse_relative_time_unknown():
    assert parse_relative_time("نامشخص") is None
    assert parse_relative_time(None) is None


def test_relative_time_fa():
    now = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)
    assert relative_time_fa(now - timedelta(seconds=10), now) == "لحظاتی پیش"
    assert relative_time_fa(now - timedelta(minutes=5), now) == "۵ دقیقه پیش"
    assert relative_time_fa(now - timedelta(hours=3), now) == "۳ ساعت پیش"
    assert relative_time_fa(now - timedelta(days=1), now) == "دیروز"
    assert relative_time_fa(now - timedelta(days=10), now) == "۱۰ روز پیش"
    assert relative_time_fa(None) == "نامشخص"


# --------------------------------------------------------------------------- #
# Jalali calendar
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "gregorian,jalali",
    [
        ((1979, 2, 11), (1357, 11, 22)),   # انقلاب
        ((2024, 3, 20), (1403, 1, 1)),     # نوروز ۱۴۰۳ (1402 was a leap year)
        ((2026, 9, 21), (1405, 6, 30)),
        ((2000, 1, 1), (1378, 10, 11)),
    ],
)
def test_gregorian_to_jalali_anchors(gregorian, jalali):
    assert gregorian_to_jalali(*gregorian) == jalali


def test_jalali_roundtrip_over_40_years():
    day = datetime(1990, 1, 1)
    for _ in range(4000):
        triple = gregorian_to_jalali(day.year, day.month, day.day)
        assert jalali_to_gregorian(*triple) == (day.year, day.month, day.day)
        day += timedelta(days=3)


def test_parse_jalali_datetime():
    parsed = parse_jalali_datetime("۳۰ شهریور ۱۴۰۵، ۲۰:۳۵")
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == jalali_to_gregorian(1405, 6, 30)
    assert (parsed.hour, parsed.minute) == (20, 35)
    assert parsed.utcoffset() == timedelta(hours=3, minutes=30)
    assert parse_jalali_datetime("بی‌معنی") is None


def test_format_datetime_fa_converts_to_tehran_time():
    moment = datetime(2026, 9, 21, 20, 35, tzinfo=timezone.utc)  # 00:05 next day in Tehran
    assert format_datetime_fa(moment) == "۱۴۰۵/۰۶/۳۱ - ۰۰:۰۵"
    assert format_datetime_fa(None) == "نامشخص"
    assert "شنبه" in format_datetime_fa(moment, with_weekday=True) or "سه‌شنبه" in format_datetime_fa(
        moment, with_weekday=True
    )


def test_find_keywords_matches_folded_text():
    text = "آیفون ۱۳ پرومکس ۲۵۶ گیگابایت"
    assert find_keywords(text, ["ایفون 13", "سامسونگ"]) == ["ایفون 13"]
    assert find_keywords(text, []) == []
    assert find_keywords(None, ["چیزی"]) == []
