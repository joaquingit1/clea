from pathlib import Path
from types import SimpleNamespace

import pytest

from ap import providers


@pytest.mark.parametrize("pid,key_slot,url,model", [
    ("kimi-go", "opencode-go-api-key", "https://opencode.ai/inference/go/anthropic", "kimi-k2.7-code"),
    ("kimi-api", "moonshot-api-key", "https://api.moonshot.ai/anthropic", "kimi-k3.1-pro"),
])
def test_kimi_uses_its_own_credential_and_pinned_route(monkeypatch, pid, key_slot, url, model):
    keys = {"opencode-go-api-key": "oc-test", "moonshot-api-key": "sk-moonshot-test", "anthropic-api-key": "sk-ant-test"}
    monkeypatch.setitem(providers.CFG, "provider", {"engine": "claude", "auth": pid.replace("-", "_")})
    monkeypatch.setattr(providers.vault, "get", keys.get)
    monkeypatch.setattr(providers, "DATA", Path("/scratch/clea"))

    assert providers.selected_id() == pid
    assert {providers.model_id(t) for t in ("haiku", "haiku5", "sonnet", "opus", "fable")} == {model}
    env = providers.claude_env()
    assert env["ANTHROPIC_BASE_URL"] == url
    assert env["ANTHROPIC_AUTH_TOKEN"] == keys[key_slot]
    assert env["ANTHROPIC_API_KEY"] is None and env["CLAUDE_CODE_OAUTH_TOKEN"] is None
    assert env["CLAUDE_CONFIG_DIR"] == f"/scratch/clea/var/claude-{pid}"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == model

    del keys[key_slot]
    with pytest.raises(providers.NotReady):
        providers.claude_env()


@pytest.mark.parametrize(("pid", "cost"), [("kimi-go", 0.00348), ("kimi-api", 0.0064)])
def test_kimi_cost_uses_provider_rates_for_cache_and_output(pid, cost):
    assert providers.kimi_cost(pid, {"input_tokens": 1000, "cache_read_input_tokens": 7000, "output_tokens": 300}) == cost


def test_moonshot_key_validation_uses_moonshot_even_when_go_is_selected(monkeypatch):
    monkeypatch.setitem(providers.CFG, "provider", {"engine": "claude", "auth": "kimi_go"})
    monkeypatch.setattr(providers, "claude_cli", lambda: {"installed": True})

    def run(cmd, **kwargs):
        assert cmd[cmd.index("--model") + 1] == "kimi-k3.1-pro"
        assert "--json-schema" in cmd
        assert kwargs["env"]["ANTHROPIC_BASE_URL"] == "https://api.moonshot.ai/anthropic"
        assert kwargs["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-moonshot-test"
        return SimpleNamespace(stdout='{"is_error":false,"result":"ok"}', stderr="", returncode=0)

    monkeypatch.setattr(providers.subprocess, "run", run)
    with pytest.raises(ValueError, match="structured answer"):
        providers.check_key("kimi-api", "sk-moonshot-test")
