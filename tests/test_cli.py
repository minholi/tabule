import httpx
import pytest

from tabule.chatwoot import ChatwootClient
from tabule.cli import _sample_human_tabulated
from tabule.config import AccountConfig


def test_evaluation_sample_uses_only_human_values_and_skips_excluded_ids():
    conversations = [
        {"id": 1, "inbox_id": 16, "status": "resolved", "custom_attributes": {"tabulacao": "A"}, "last_activity_at": 5},
        {"id": 2, "inbox_id": 16, "status": "resolved", "custom_attributes": {"tabulacao": "A"}, "last_activity_at": 4},
        {"id": 3, "inbox_id": 16, "status": "resolved", "custom_attributes": {"tabulacao": "B"}, "last_activity_at": 3},
        {"id": 4, "inbox_id": 99, "status": "resolved", "custom_attributes": {"tabulacao": "A"}, "last_activity_at": 2},
        {"id": 5, "inbox_id": 16, "status": "resolved", "custom_attributes": {}, "last_activity_at": 1},
    ]

    def handler(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"data": {"meta": {}, "payload": conversations if page == 1 else []}})

    client = ChatwootClient("https://omni.example", "tok", 9, transport=httpx.MockTransport(handler))
    account = AccountConfig(
        id=9,
        name="FDC",
        mode="shadow",
        inbox_ids=frozenset({16}),
        attribute_key="tabulacao",
        review_label="tabulacao-revisar",
        confidence_threshold=0.8,
        context="",
        options={},
        api_token="tok",
        webhook_secret="sec",
    )
    sample = _sample_human_tabulated(client, account, ["A", "B"], limit=10, pages=3, exclude={2})
    assert [conversation["id"] for conversation in sample] == [1, 3]


def _describe_env(tmp_path, monkeypatch):
    import json

    from tabule import cli
    from tabule.config import Settings
    from tabule.describer import DescriptionWriter

    definitions = [
        {
            "attribute_key": "tabulacao",
            "attribute_display_type": "list",
            "attribute_values": ["RECUSA FINANCEIRO", "SIM"],
        }
    ]
    calls = []

    def chatwoot(request):
        return httpx.Response(200, json=definitions)

    def openai_handler(request):
        calls.append(json.loads(request.content))
        content = {"context": "Pós FDC.", "options": {"RECUSA FINANCEIRO": "Dinheiro.", "SIM": "Aceitou."}}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

    monkeypatch.setattr(
        cli,
        "ChatwootClient",
        lambda base, token, account, timeout: ChatwootClient(base, token, account, transport=httpx.MockTransport(chatwoot)),
    )
    monkeypatch.setattr(
        cli,
        "DescriptionWriter",
        lambda key, model, timeout: DescriptionWriter(key, model, transport=httpx.MockTransport(openai_handler)),
    )
    monkeypatch.setenv("TABULE_ACCOUNT_9_API_TOKEN", "tok")
    settings = Settings.from_env(
        {
            "TABULE_ALLOWED_ACCOUNT_IDS": "9",
            "TABULE_CHATWOOT_BASE_URL": "https://omni.example",
            "TABULE_ACCOUNTS_DIR": str(tmp_path),
            "TABULE_OPENAI_API_KEY": "k",
        }
    )
    return cli, settings, calls


def _describe_args(cli, *argv):
    return cli._parser().parse_args(["describe", "--account", "9", "--about", "FDC", *argv])


def test_describe_creates_missing_yaml_and_skips_second_run(tmp_path, monkeypatch, capsys):
    cli, settings, calls = _describe_env(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="--inbox-ids"):
        cli.cmd_describe(settings, _describe_args(cli))
    assert not calls

    assert cli.cmd_describe(settings, _describe_args(cli, "--inbox-ids", "16", "--name", "FDC")) == 0
    text = (tmp_path / "9.yaml").read_text(encoding="utf-8")
    assert "mode: shadow" in text and "Dinheiro." in text
    assert len(calls) == 1

    assert cli.cmd_describe(settings, _describe_args(cli)) == 0
    assert len(calls) == 1
    assert "Nada a preencher" in capsys.readouterr().out


def test_describe_dry_run_does_not_write(tmp_path, monkeypatch):
    cli, settings, calls = _describe_env(tmp_path, monkeypatch)
    assert cli.cmd_describe(settings, _describe_args(cli, "--inbox-ids", "16", "--dry-run")) == 0
    assert len(calls) == 1
    assert not (tmp_path / "9.yaml").exists()
