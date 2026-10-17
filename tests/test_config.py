import pytest

from tabule.config import ConfigError, Settings, load_accounts, parse_account_ids

YAML_OK = """
name: FDC
mode: shadow
inbox_ids: [16, 27]
options:
  "RECUSA FINANCEIRO": "Recusou por motivo financeiro."
  "PAROU DE RESPONDER PRODUTO": "Parou de responder sobre o produto."
"""

ENV_9 = {"TABULE_ACCOUNT_9_API_TOKEN": "tok-9", "TABULE_ACCOUNT_9_WEBHOOK_SECRET": "sec-9"}


def make_settings(tmp_path, ids="9"):
    env = {
        "TABULE_ALLOWED_ACCOUNT_IDS": ids,
        "TABULE_CHATWOOT_BASE_URL": "https://omni.example/",
        "TABULE_ACCOUNTS_DIR": str(tmp_path),
    }
    return Settings.from_env(env, require_openai=False)


def write_account(tmp_path, text, account_id=9):
    (tmp_path / f"{account_id}.yaml").write_text(text, encoding="utf-8")


def test_parse_account_ids_dedupes_and_sorts():
    assert parse_account_ids("12, 9,9,") == (9, 12)


def test_parse_account_ids_rejects_invalid_values():
    with pytest.raises(ConfigError):
        parse_account_ids("9, abc")
    with pytest.raises(ConfigError):
        parse_account_ids(" , ")


def test_settings_strip_trailing_slash_and_require_openai_only_when_asked():
    env = {"TABULE_ALLOWED_ACCOUNT_IDS": "9", "TABULE_CHATWOOT_BASE_URL": "https://omni.example/"}
    settings = Settings.from_env(env, require_openai=False)
    assert settings.chatwoot_base_url == "https://omni.example"
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_load_accounts_enables_valid_account_without_exposing_secrets(tmp_path):
    write_account(tmp_path, YAML_OK)
    accounts, errors = load_accounts(make_settings(tmp_path), environ=ENV_9)
    assert errors == {}
    account = accounts[9]
    assert account.mode == "shadow"
    assert account.inbox_ids == frozenset({16, 27})
    assert account.confidence_threshold == 0.8
    assert account.attribute_key == "tabulacao"
    assert "tok-9" not in repr(account)
    assert "sec-9" not in repr(account)


def test_load_accounts_reports_missing_file(tmp_path):
    accounts, errors = load_accounts(make_settings(tmp_path), environ=ENV_9)
    assert accounts == {}
    assert "arquivo ausente" in errors[9]


def test_load_accounts_reports_missing_secret(tmp_path):
    write_account(tmp_path, YAML_OK)
    accounts, errors = load_accounts(make_settings(tmp_path), environ={"TABULE_ACCOUNT_9_API_TOKEN": "tok"})
    assert accounts == {}
    assert "WEBHOOK_SECRET" in errors[9]


def test_load_accounts_rejects_invalid_mode(tmp_path):
    write_account(tmp_path, YAML_OK.replace("mode: shadow", "mode: automatico"))
    _, errors = load_accounts(make_settings(tmp_path), environ=ENV_9)
    assert "mode" in errors[9]


def test_load_accounts_rejects_threshold_out_of_range(tmp_path):
    write_account(tmp_path, YAML_OK + "confidence_threshold: 1.5\n")
    _, errors = load_accounts(make_settings(tmp_path), environ=ENV_9)
    assert "confidence_threshold" in errors[9]


def test_disabled_account_does_not_block_others(tmp_path):
    write_account(tmp_path, YAML_OK, account_id=9)
    env = {**ENV_9, "TABULE_ACCOUNT_12_API_TOKEN": "tok-12", "TABULE_ACCOUNT_12_WEBHOOK_SECRET": "sec-12"}
    accounts, errors = load_accounts(make_settings(tmp_path, ids="9,12"), environ=env)
    assert set(accounts) == {9}
    assert "arquivo ausente" in errors[12]
