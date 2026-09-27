"""Tests for configuration loading and validation."""

from __future__ import annotations

import pytest

from config import ConfigError, Settings, load_or_die, resolve_city_id


def test_defaults_apply_without_any_environment(settings):
    assert settings.telegram_chat_id == "42424242"
    assert settings.city_id == "1"
    assert settings.divar_api_base == "https://api.divar.ir"
    assert settings.divar_web_base == "https://divar.ir"
    assert settings.page_size == 24
    assert settings.request_delay_min == 0.0


def test_environment_variables_are_read(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:AAA")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@deals_channel")
    monkeypatch.setenv("CITY", "mashhad")
    monkeypatch.setenv("CATEGORY", "tablet")
    monkeypatch.setenv("SEARCH_QUERY", "آیفون ۱۳")
    monkeypatch.setenv("DEAL_MAX_PRICE", "30000000")
    monkeypatch.setenv("CHECK_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("EXCLUDE_KEYWORDS", "خراب، اقساطی , معیوب")
    monkeypatch.setenv("DRY_RUN", "true")

    loaded = Settings.load(env_file=None)
    assert loaded.telegram_bot_token == "111:AAA"
    assert loaded.telegram_chat_id == "@deals_channel"
    assert loaded.city_id == "3"
    assert loaded.category == "tablet"
    assert loaded.search_query == "آیفون ۱۳"
    assert loaded.deal_max_price == 30_000_000
    assert loaded.check_interval_seconds == 45.0
    assert loaded.exclude_keywords == ["خراب", "اقساطی", "معیوب"]
    assert loaded.dry_run is True
    assert loaded.problems() == []


def test_missing_credentials_are_reported(monkeypatch):
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(env_file=None)
    message = str(excinfo.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "TELEGRAM_CHAT_ID" in message


def test_dry_run_does_not_require_credentials(settings_factory):
    settings = settings_factory(telegram_bot_token="", telegram_chat_id="", dry_run=True)
    assert settings.problems() == []


def test_dry_run_can_be_turned_on_by_env(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "yes")
    assert Settings.load(env_file=None, validate=False).dry_run is True


def test_proxy_url_is_shared_between_targets(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("PROXY_URL", "socks5h://127.0.0.1:10808")
    settings = Settings.load(env_file=None)
    assert settings.telegram_proxy == "socks5h://127.0.0.1:10808"
    assert settings.divar_proxy == "socks5h://127.0.0.1:10808"
    assert settings.proxies_for("telegram") == {
        "http": "socks5h://127.0.0.1:10808",
        "https": "socks5h://127.0.0.1:10808",
    }


def test_target_specific_proxy_wins(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("PROXY_URL", "http://shared:8080")
    monkeypatch.setenv("TELEGRAM_PROXY", "socks5h://127.0.0.1:10808")
    settings = Settings.load(env_file=None)
    assert settings.telegram_proxy == "socks5h://127.0.0.1:10808"
    assert settings.divar_proxy == "http://shared:8080"
    assert settings.proxies_for("divar") == {"http": "http://shared:8080", "https": "http://shared:8080"}


def test_https_proxy_environment_is_picked_up(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    settings = Settings.load(env_file=None)
    assert settings.telegram_proxy == "http://127.0.0.1:3128"


def test_unsupported_proxy_scheme_is_rejected(settings_factory):
    settings = settings_factory(telegram_proxy="ftp://proxy:21")
    assert any("unsupported scheme" in problem for problem in settings.problems())


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"request_delay_min": 10, "request_delay_max": 4}, "REQUEST_DELAY_MIN"),
        ({"price_min": 90_000_000, "deal_max_price": 50_000_000}, "PRICE_MIN"),
        ({"price_max": 1_000_000, "deal_price_floor": 5_000_000}, "PRICE_MAX"),
        ({"divar_endpoint_mode": "turbo"}, "DIVAR_ENDPOINT_MODE"),
        ({"include_keywords": ["آیفون"], "exclude_keywords": ["آیفون"]}, "overlap"),
    ],
)
def test_contradictory_settings_are_rejected(settings_factory, overrides, needle):
    settings = settings_factory(**overrides)
    assert any(needle in problem for problem in settings.problems())


def test_invalid_numbers_raise_config_error(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DEAL_MAX_PRICE", "cheap")
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(env_file=None)
    assert "DEAL_MAX_PRICE" in str(excinfo.value)


def test_invalid_boolean_raises_config_error(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "maybe")
    with pytest.raises(ConfigError):
        Settings.load(env_file=None)


def test_page_size_is_clamped_to_divar_maximum(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("PAGE_SIZE", "500")
    assert Settings.load(env_file=None).page_size == 60


def test_price_values_accept_separators(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DEAL_MAX_PRICE", "50_000_000")
    monkeypatch.setenv("PRICE_MIN", "5,000,000")
    settings = Settings.load(env_file=None)
    assert settings.deal_max_price == 50_000_000
    assert settings.price_min == 5_000_000


def test_overrides_win_over_environment(monkeypatch, settings_factory):
    monkeypatch.setenv("DEAL_MAX_PRICE", "1000000")
    settings = settings_factory(deal_max_price=25_000_000)
    assert settings.deal_max_price == 25_000_000


def test_unknown_override_is_an_error(settings_factory):
    with pytest.raises(ConfigError):
        settings_factory(this_is_not_a_setting=True)


def test_with_updates_returns_a_copy(settings):
    updated = settings.with_updates(dry_run=False, city_id="3")
    assert updated.dry_run is False
    assert updated.city_id == "3"
    assert settings.dry_run is True  # original untouched
    with pytest.raises(ConfigError):
        settings.with_updates(nope=1)


def test_masked_settings_hide_secrets(settings_factory):
    settings = settings_factory(
        telegram_bot_token="1234567890:SECRETVALUE", telegram_proxy="socks5h://127.0.0.1:10808"
    )
    masked = settings.masked()
    assert "SECRETVALUE" not in str(masked)
    assert masked["telegram_bot_token"] == "1234***ALUE"
    assert "10808" not in masked["telegram_proxy"]


def test_describe_mentions_the_important_knobs(settings_factory):
    text = settings_factory(deal_max_price=50_000_000).describe()
    assert "tehran" in text and "mobile-phones" in text and "۵۰٬۰۰۰٬۰۰۰" in text


@pytest.mark.parametrize(
    "city,expected_id",
    [("1", "1"), ("tehran", "1"), ("TEHRAN", "1"), ("تهران", "1"), ("mashhad", "3"), ("اصفهان", "4")],
)
def test_resolve_city_id(city, expected_id):
    city_id, problems = resolve_city_id(city)
    assert city_id == expected_id
    assert problems == []


def test_resolve_city_id_explicit_override_wins():
    city_id, problems = resolve_city_id("tehran", explicit_id="777")
    assert city_id == "777"
    assert problems == []


def test_resolve_city_id_unknown_city_explains_itself():
    city_id, problems = resolve_city_id("atlantis")
    assert city_id == "atlantis"
    assert problems and "CITY_ID" in problems[0]


def test_load_or_die_exits_with_code_2(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        load_or_die(env_file=None)
    assert excinfo.value.code == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_env_file_is_loaded(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=999:FROM_FILE\nTELEGRAM_CHAT_ID=55\nDRY_RUN=true\nCITY=shiraz\n",
        encoding="utf-8",
    )
    settings = Settings.load(env_file=env)
    assert settings.telegram_bot_token == "999:FROM_FILE"
    assert settings.city_id == "6"  # shiraz


def test_process_environment_beats_env_file(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("DEAL_MAX_PRICE=1000\nDRY_RUN=true\n", encoding="utf-8")
    monkeypatch.setenv("DEAL_MAX_PRICE", "777")
    assert Settings.load(env_file=env).deal_max_price == 777
