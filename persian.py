"""Persian-aware helpers: digits, prices, relative time and Jalali dates.

Divar renders everything in Persian. A listing row carries its price as text
(``"۸۵٬۰۰۰٬۰۰۰ تومان"``, ``"پرداخت توافقی"``, ``"۴۵۰ میلیون تومان"``) and its
timestamp as relative text (``"لحظاتی پیش در بلوار کشاورز"``) or a Jalali string
(``"۳۱ شهریور ۱۴۰۵، ۰۰:۲۰"``). This module converts all of that into values the
deal-detection logic can compare, and converts numbers back into Persian text
for the Telegram alerts.

Everything here is pure and dependency-free so it can be unit-tested offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

__all__ = [
    "to_ascii_digits",
    "to_persian_digits",
    "format_number_fa",
    "human_toman",
    "ParsedPrice",
    "parse_price",
    "extract_price_from_text",
    "extract_total_price",
    "is_down_payment_text",
    "is_installment_text",
    "normalize_for_match",
    "split_time_and_place",
    "parse_relative_time",
    "gregorian_to_jalali",
    "jalali_to_gregorian",
    "parse_jalali_datetime",
    "format_datetime_fa",
    "format_datetime_en",
    "relative_time_fa",
    "find_keywords",
    "tehran_tz",
]

# --------------------------------------------------------------------------- #
# digits & separators
# --------------------------------------------------------------------------- #
_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_TO_ASCII = {ord(c): str(i) for i, c in enumerate(_PERSIAN_DIGITS)}
_TO_ASCII.update({ord(c): str(i) for i, c in enumerate(_ARABIC_DIGITS)})
_TO_PERSIAN = {ord(str(i)): c for i, c in enumerate(_PERSIAN_DIGITS)}

ZWNJ = "\u200c"  # نیم‌فاصله
ARABIC_THOUSANDS = "\u066c"  # ٬
ARABIC_DECIMAL = "\u066b"  # ٫
SEPARATORS = str.maketrans(
    {",": "", "،": "", ARABIC_THOUSANDS: "", "\u202f": "", "\u00a0": "", " ": "", ZWNJ: ""}
)

# Arabic letter variants that Iranian users mix into Persian text.
_LETTER_FIXES = str.maketrans(
    {"ي": "ی", "ك": "ک", "ة": "ه", "أ": "ا", "إ": "ا", "آ": "ا", "ؤ": "و", "ئ": "ی"}
)


def to_ascii_digits(text: object) -> str:
    """Convert Persian/Arabic-Indic digits in ``text`` to ASCII digits."""
    if text is None:
        return ""
    return str(text).translate(_TO_ASCII)


def to_persian_digits(text: object) -> str:
    """Convert ASCII digits in ``text`` to Persian digits (for display)."""
    if text is None:
        return ""
    return str(text).translate(_TO_PERSIAN)


def format_number_fa(value: Optional[float]) -> str:
    """``45000000`` -> ``"۴۵٬۰۰۰٬۰۰۰"`` (Persian digits + Persian separators)."""
    if value is None:
        return "—"
    if isinstance(value, float) and not value.is_integer():
        text = f"{value:,.2f}"
    else:
        text = f"{int(value):,}"
    # ASCII comma -> Persian thousands separator, then localize the digits.
    return to_persian_digits(text.replace(",", ARABIC_THOUSANDS))


def _format_amount(amount: float) -> str:
    """``1.5`` -> ``"۱٫۵"``, ``450.0`` -> ``"۴۵۰"`` (Persian digits & decimal)."""
    text = f"{amount:,.2f}".rstrip("0").rstrip(".")
    text = text.replace(",", ARABIC_THOUSANDS).replace(".", ARABIC_DECIMAL)
    return to_persian_digits(text)


def normalize_for_match(text: object) -> str:
    """Fold Persian text for keyword matching.

    Lowercases, maps Arabic letters to their Persian forms, replaces ZWNJ and
    every whitespace variant with a single space, and converts digits to ASCII
    so ``"آیفون ۱۳"`` matches ``"ایفون 13"``.
    """
    if text is None:
        return ""
    folded = to_ascii_digits(str(text)).translate(_LETTER_FIXES)
    folded = folded.replace(ZWNJ, " ").replace("\u200f", " ")
    folded = re.sub(r"[\s‌‏]+", " ", folded)
    return folded.strip().lower()


# --------------------------------------------------------------------------- #
# prices
# --------------------------------------------------------------------------- #
FREE_WORDS = ("رایگان", "مجانی", "مفت", "هدیه")
NEGOTIABLE_WORDS = ("توافقی", "توافق", "قابل مذاکره", "مذاکره", "تماس", "چانه")
EXCHANGE_WORDS = ("معاوضه", "تهاتر", "عوض می", "معوضه")
DEPOSIT_RENT_WORDS = ("ودیعه", "رهن", "اجاره", "قرض الحسنه", "پیش پرداخت اجاره")
DOWN_PAYMENT_WORDS = ("پیش پرداخت", "بیعانه", "پیش دریافت", "پرداخت اولیه", "پیش قسط", "مبلغ پیش")
INSTALLMENT_WORDS = ("اقساط", "قسطی", "چک صیادی", "لیزینگ", "چکی")
STARTING_PRICE_WORDS = ("قیمت از", "از مبلغ", "شروع از", "پایه قیمت", "کف قیمت")

UNIT_MULTIPLIERS: Tuple[Tuple[str, int], ...] = (
    ("میلیارد", 1_000_000_000),
    ("میلیون", 1_000_000),
    ("هزار", 1_000),
)

# "۲ میلیارد و ۳۰۰ میلیون تومان"
_COMPOUND_RE = re.compile(
    r"(\d+(?:[./٫]\d+)?)\s*میلیارد\s*و?\s*(\d+(?:[./٫]\d+)?)\s*میلیون"
)
# a number optionally followed by a scale word
_NUMBER_UNIT_RE = re.compile(r"(\d+(?:[./٫]\d+)?)\s*(میلیارد|میلیون|هزار)?")
_NUMBER_RE = re.compile(r"\d+(?:[./٫]\d+)?")


@dataclass(frozen=True)
class ParsedPrice:
    """The result of reading a Persian price string.

    ``value`` is always in **Toman** (the unit Divar displays), or ``None`` when
    the listing has no usable number (negotiable / free / exchange).
    """

    value: Optional[int] = None
    text: str = ""
    note: Optional[str] = None  # negotiable | free | exchange | deposit_rent | from_price
    is_down_payment: bool = False
    is_installment: bool = False
    is_deposit: bool = False

    @property
    def usable(self) -> bool:
        return self.value is not None

    def human(self) -> str:
        """Price as Iranians would read it, e.g. ``"۴۵۰ میلیون تومان"``."""
        if self.value is None:
            if self.note == "free":
                return "رایگان"
            if self.note == "exchange":
                return "معاوضه"
            if self.note == "negotiable":
                return "توافقی"
            return "بدون قیمت"
        return human_toman(self.value)


def human_toman(value: Optional[int]) -> str:
    """``450000000`` -> ``"۴۵۰ میلیون تومان"``; ``85000000`` -> ``"۸۵٬۰۰۰٬۰۰۰ تومان"``."""
    if value is None:
        return "—"
    value = int(value)
    if value >= 1_000_000_000:
        return f"{_format_amount(value / 1_000_000_000)} میلیارد تومان"
    if value >= 1_000_000:
        return f"{_format_amount(value / 1_000_000)} میلیون تومان"
    return f"{format_number_fa(value)} تومان"


def _to_int(number_text: str, multiplier: int = 1) -> int:
    """Turn ``"۱.۵"`` / ``"85,000,000"`` into an int scaled by ``multiplier``."""
    cleaned = to_ascii_digits(number_text).replace(",", "").replace("،", "").replace(ARABIC_THOUSANDS, "")
    cleaned = cleaned.replace(ARABIC_DECIMAL, ".").replace("/", ".")
    # Keep only the first dot: Persian users sometimes group with "."
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return int(round(float(cleaned) * multiplier))
    except ValueError:
        return 0


def _contains(haystack: str, needles) -> bool:
    return any(needle in haystack for needle in needles)


def parse_price(text: object) -> ParsedPrice:
    """Parse a Divar price string into a :class:`ParsedPrice`.

    Handles::

        "۸۵٬۰۰۰٬۰۰۰ تومان"        -> 85_000_000
        "۴۵۰ میلیون تومان"          -> 450_000_000
        "۲ میلیارد و ۳۰۰ میلیون"    -> 2_300_000_000
        "۱.۵ میلیارد"               -> 1_500_000_000
        "۴۵۰ هزار"                  -> 450_000
        "پرداخت توافقی"             -> None, note="negotiable"
        "رایگان"                    -> None, note="free"
        "معاوضه"                    -> None, note="exchange"
        "ودیعه ۱۰۰٬۰۰۰٬۰۰۰"        -> 100_000_000, is_deposit=True
        "پیش‌پرداخت ۲۰۰ میلیون"     -> 200_000_000, is_down_payment=True
    """
    if text is None:
        return ParsedPrice(None, "", None)

    original = str(text).strip()
    if not original:
        return ParsedPrice(None, "", None)

    ascii_text = to_ascii_digits(original)
    # Words are matched on a ZWNJ-normalized, space-padded copy so that
    # "پیش‌پرداخت" and "پیش پرداخت" behave identically.
    words = normalize_for_match(original)

    note: Optional[str] = None
    if _contains(words, FREE_WORDS):
        note = "free"
    elif _contains(words, EXCHANGE_WORDS):
        note = "exchange"
    elif _contains(words, NEGOTIABLE_WORDS):
        note = "negotiable"

    is_down_payment = _contains(words, DOWN_PAYMENT_WORDS)
    is_installment = _contains(words, INSTALLMENT_WORDS)
    is_deposit = _contains(words, ("ودیعه", "رهن کامل", "رهن"))

    value: Optional[int] = None

    # 1) compact form, e.g. "85,000,000 تومان" / "۸۵٬۰۰۰٬۰۰۰"
    compact = ascii_text.translate(SEPARATORS)
    compound = _COMPOUND_RE.search(compact)
    if compound:
        value = _to_int(compound.group(1), 1_000_000_000) + _to_int(compound.group(2), 1_000_000)
    else:
        match = _NUMBER_UNIT_RE.search(compact)
        if match:
            multiplier = 1
            for unit, factor in UNIT_MULTIPLIERS:
                if match.group(2) == unit:
                    multiplier = factor
                    break
            value = _to_int(match.group(1), multiplier)

    # 2) "1.5 میلیارد" written with a Persian decimal separator is already
    #    handled above; a stray unit with no attached number ("میلیون تومان")
    #    means we cannot know the amount.
    if value in (0, None):
        value = None

    if note is None and is_deposit and not is_down_payment:
        note = "deposit_rent"

    if note is None and _contains(words, STARTING_PRICE_WORDS):
        note = "from_price"

    return ParsedPrice(
        value=value,
        text=original,
        note=note,
        is_down_payment=is_down_payment,
        is_installment=is_installment,
        is_deposit=is_deposit,
    )


_PRICE_KEYWORD_RE = re.compile(
    r"(?:قیمت|مبلغ|بها|نرخ)\s*(?:کل|نهایی|پایه|فروش|اعلامی|پیش پرداخت|نقدی|کارشناسی)?\s*[:：=]?\s*"
    r"([۰-۹٠-٩0-9][۰-۹٠-٩0-9٬،,٫./\s]*(?:میلیارد|میلیون|هزار)?(?:\s*تومان)?)"
)
_REMAINING_RE = re.compile(
    r"(?:مابقی|باقی مانده|باقیمانده|بقیه)\s*[:：]?\s*"
    r"([۰-۹٠-٩0-9][۰-۹٠-٩0-9٬،,٫./\s]*(?:میلیارد|میلیون|هزار)?)"
)
_TOTAL_RE = re.compile(
    r"(?:قیمت\s*کل|کل\s*قیمت|مبلغ\s*کل|قیمت\s*نهایی|قیمت\s*تمام\s*شده|مبلغ\s*نهایی)"
    r"\s*[:：=]?\s*([۰-۹٠-٩0-9][۰-۹٠-٩0-9٬،,٫./\s]*(?:میلیارد|میلیون|هزار)?(?:\s*تومان)?)"
)


def extract_price_from_text(text: object) -> Optional[int]:
    """Pull a Toman amount out of free text anchored on a price keyword.

    Used for descriptions/titles where a bare number search would happily match
    a model number ("آیفون ۱۳") or a phone number.
    """
    if not text:
        return None
    haystack = to_ascii_digits(str(text))
    haystack = haystack.replace(ZWNJ, " ")
    for regex in (_PRICE_KEYWORD_RE,):
        match = regex.search(haystack)
        if match:
            parsed = parse_price(match.group(1))
            if parsed.value:
                return parsed.value
    return None


def extract_total_price(text: object, down_payment: Optional[int] = None) -> Optional[int]:
    """Recover the *total* price when the listed price is only a down payment.

    Three cases, in order of trust:

    1. an explicit total — ``"قیمت کل: ۱٬۲۰۰٬۰۰۰٬۰۰۰ تومان"``
    2. remaining balance (``"مابقی ۸۰۰ میلیون"``) plus a down payment that is
       either passed in or written next to a price keyword in the same text
    3. otherwise ``None`` — guessing a total from a bare number would be worse
       than admitting we do not know.
    """
    if not text:
        return None
    haystack = to_ascii_digits(str(text)).replace(ZWNJ, " ")

    match = _TOTAL_RE.search(haystack)
    if match:
        total = parse_price(match.group(1)).value
        if total:
            return total

    remaining = _REMAINING_RE.search(haystack)
    if remaining:
        remaining_value = parse_price(remaining.group(1)).value or 0
        if not remaining_value:
            return None
        down_value = down_payment or 0
        if not down_value:
            keyword_match = _PRICE_KEYWORD_RE.search(haystack)
            if keyword_match:
                down_value = parse_price(keyword_match.group(1)).value or 0
        return remaining_value + down_value if down_value else None
    return None


def is_down_payment_text(*texts: object) -> bool:
    """True when any of the given texts advertises a down payment."""
    for text in texts:
        if not text:
            continue
        if _contains(normalize_for_match(text), DOWN_PAYMENT_WORDS):
            return True
    return False


def is_installment_text(*texts: object) -> bool:
    """True when any of the given texts advertises installments."""
    for text in texts:
        if not text:
            continue
        if _contains(normalize_for_match(text), INSTALLMENT_WORDS):
            return True
    return False


# --------------------------------------------------------------------------- #
# relative time ("لحظاتی پیش در بلوار کشاورز")
# --------------------------------------------------------------------------- #
_JUST_NOW = ("لحظاتی پیش", "چند لحظه پیش", "دقایقی پیش", "همین حالا", "کمتر از یک دقیقه", "الان")
_YESTERDAY = (("پریروز", 48), ("دیروز", 24))

_RELATIVE_RE = re.compile(r"(\d+)\s*(دقیقه|ساعت|روز|هفته|ماه|سال)")
_RELATIVE_UNITS = {
    "دقیقه": 60,
    "ساعت": 3_600,
    "روز": 86_400,
    "هفته": 604_800,
    "ماه": 2_592_000,  # 30 days
    "سال": 31_536_000,  # 365 days
}


def split_time_and_place(bottom_text: object) -> Tuple[str, str]:
    """Split ``"لحظاتی پیش در بلوار کشاورز"`` into ``("لحظاتی پیش", "بلوار کشاورز")``."""
    if not bottom_text:
        return "", ""
    text = str(bottom_text).strip()
    if " در " in text:
        time_part, _, place_part = text.partition(" در ")
        return time_part.strip(), place_part.strip()
    return text, ""


def parse_relative_time(text: object) -> Optional[timedelta]:
    """Convert Divar's relative time text into an age (best effort)."""
    if not text:
        return None
    folded = to_ascii_digits(str(text)).replace(ZWNJ, " ")
    if any(word in folded for word in _JUST_NOW):
        return timedelta(0)
    match = _RELATIVE_RE.search(folded)
    if match:
        seconds = int(match.group(1)) * _RELATIVE_UNITS.get(match.group(2), 0)
        return timedelta(seconds=seconds) if seconds else None
    for word, hours in _YESTERDAY:
        if word in folded:
            return timedelta(hours=hours)
    return None


def relative_time_fa(moment: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Human age in Persian, e.g. ``"۵ دقیقه پیش"`` / ``"لحظاتی پیش"``."""
    if moment is None:
        return "نامشخص"
    now = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = int((now - moment).total_seconds())
    if seconds < 60:
        return "لحظاتی پیش"
    if seconds < 3_600:
        return to_persian_digits(seconds // 60) + " دقیقه پیش"
    if seconds < 86_400:
        return to_persian_digits(seconds // 3_600) + " ساعت پیش"
    days = seconds // 86_400
    if days == 1:
        return "دیروز"
    if days < 30:
        return to_persian_digits(days) + " روز پیش"
    if days < 365:
        return to_persian_digits(days // 30) + " ماه پیش"
    return to_persian_digits(days // 365) + " سال پیش"


# --------------------------------------------------------------------------- #
# Jalali (Solar Hijri) calendar — pure arithmetic, no third-party dependency
# --------------------------------------------------------------------------- #
_G_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_J_DAYS_IN_MONTH = (31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29)
JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)
_MONTH_TO_NUMBER = {name: index + 1 for index, name in enumerate(JALALI_MONTHS)}
# Common alternative spellings.
_MONTH_TO_NUMBER.update({"حمل": 1, "ثور": 2, "جوزا": 3, "سرطان": 4, "اسد": 5, "سنبله": 6})


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> Tuple[int, int, int]:
    """Convert a Gregorian date to ``(jalali_year, jalali_month, jalali_day)``."""
    gy = gy - 1600
    gm = gm - 1
    gd = gd - 1

    g_day_no = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    for i in range(gm):
        g_day_no += _G_DAYS_IN_MONTH[i]
    if gm > 1 and ((gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)):
        g_day_no += 1  # leap year, past February
    g_day_no += gd

    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365

    jm = 0
    for i in range(11):
        if j_day_no < _J_DAYS_IN_MONTH[i]:
            jm = i + 1
            break
        j_day_no -= _J_DAYS_IN_MONTH[i]
    else:
        jm = 12
    return jy, jm, j_day_no + 1


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> Tuple[int, int, int]:
    """Convert a Jalali date to ``(gregorian_year, month, day)``."""
    jy -= 979
    jm -= 1
    jd -= 1

    j_day_no = 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4
    for i in range(jm):
        j_day_no += _J_DAYS_IN_MONTH[i]
    j_day_no += jd

    g_day_no = j_day_no + 79
    gy = 1600 + 400 * (g_day_no // 146097)
    g_day_no %= 146097
    leap = True
    if g_day_no >= 36525:
        g_day_no -= 1
        gy += 100 * (g_day_no // 36524)
        g_day_no %= 36524
        if g_day_no >= 365:
            g_day_no += 1
        else:
            leap = False

    gy += 4 * (g_day_no // 1461)
    g_day_no %= 1461
    if g_day_no >= 366:
        leap = False
        g_day_no -= 1
        gy += g_day_no // 365
        g_day_no %= 365

    months = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    gm = 0
    while gm < 12 and g_day_no >= months[gm]:
        g_day_no -= months[gm]
        gm += 1
    return gy, gm + 1, g_day_no + 1


def tehran_tz(offset_hours: float = 3.5) -> timezone:
    """A ``timezone`` for Iran (UTC+3:30 by default; Iran dropped DST in 2022)."""
    return timezone(timedelta(hours=offset_hours))


_JALALI_RE = re.compile(
    r"(\d{1,2})\s*([آ-ی]+)\s*(\d{4})\s*[،,]?\s*(?:(\d{1,2})\s*[:：]\s*(\d{2}))?"
)


def parse_jalali_datetime(
    text: object, tz_offset_hours: float = 3.5
) -> Optional[datetime]:
    """Parse ``"۳۱ شهریور ۱۴۰۵، ۰۰:۲۰"`` into an aware ``datetime``."""
    if not text:
        return None
    folded = to_ascii_digits(str(text)).replace(ZWNJ, " ")
    match = _JALALI_RE.search(folded)
    if not match:
        return None
    day, month_name, year = int(match.group(1)), match.group(2), int(match.group(3))
    month = _MONTH_TO_NUMBER.get(month_name.strip())
    if not month:
        return None
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    try:
        gy, gm, gd = jalali_to_gregorian(year, month, day)
        return datetime(
            gy, gm, gd, hour, minute, tzinfo=tehran_tz(tz_offset_hours)
        )
    except ValueError:
        return None


def format_datetime_fa(
    moment: Optional[datetime], tz_offset_hours: float = 3.5, with_weekday: bool = False
) -> str:
    """Format a datetime as ``"۱۴۰۵/۰۶/۳۰ - ۲۰:۳۵"`` in Iran local time."""
    if moment is None:
        return "نامشخص"
    tz = tehran_tz(tz_offset_hours)
    local = moment.astimezone(tz) if moment.tzinfo else moment.replace(tzinfo=tz)
    jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
    date_part = to_persian_digits(f"{jy:04d}/{jm:02d}/{jd:02d}")
    time_part = to_persian_digits(f"{local.hour:02d}:{local.minute:02d}")
    if with_weekday:
        weekdays = ("شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه")
        # 1970-01-01 was a Thursday == "پنجشنبه"; Python's Monday=0.
        weekday = weekdays[(local.weekday() + 2) % 7]
        return f"{weekday} {date_part} - {time_part}"
    return f"{date_part} - {time_part}"


def format_datetime_en(moment: Optional[datetime], tz_offset_hours: float = 3.5) -> str:
    """ISO-ish local time, for logs and the SQLite store."""
    if moment is None:
        return ""
    tz = tehran_tz(tz_offset_hours)
    local = moment.astimezone(tz) if moment.tzinfo else moment.replace(tzinfo=tz)
    return local.isoformat(timespec="seconds")


def find_keywords(text: object, keywords: List[str]) -> List[str]:
    """Return the subset of ``keywords`` that appear in ``text`` (folded match)."""
    if not text or not keywords:
        return []
    folded = normalize_for_match(text)
    return [keyword for keyword in keywords if normalize_for_match(keyword) in folded]
