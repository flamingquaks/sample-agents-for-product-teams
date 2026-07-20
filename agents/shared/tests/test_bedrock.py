"""Unit tests for `agents.shared.bedrock.build_model` (Bedrock Mantle path)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def fake_openai_model(monkeypatch):
    """Patch strands.models.openai.OpenAIModel + the bearer-token mint so no
    network/boto call happens, and expose the captured constructor kwargs."""
    import shared.bedrock as mod

    # build_model imports OpenAIModel lazily from strands.models.openai and mints
    # a token via _bearer_token — stub both.
    monkeypatch.setattr(mod, "_bearer_token", lambda: "tok-123")
    fake_module = MagicMock()
    mock_cls = MagicMock(return_value=MagicMock())
    fake_module.OpenAIModel = mock_cls
    monkeypatch.setitem(sys.modules, "strands.models.openai", fake_module)
    return mock_cls


def _headers(mock_cls):
    return mock_cls.call_args.kwargs["client_args"]["default_headers"]


def test_guardrail_headers_attached(monkeypatch, fake_openai_model):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-x")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    import shared.bedrock as mod

    mod.build_model()
    h = _headers(fake_openai_model)
    assert h["X-Amzn-Bedrock-GuardrailIdentifier"] == "gr-x"
    assert h["X-Amzn-Bedrock-GuardrailVersion"] == "DRAFT"
    assert h["X-Amzn-Bedrock-Trace"] == "ENABLED"
    # Endpoint is the region's bedrock-mantle URL; auth is the minted bearer token.
    ca = fake_openai_model.call_args.kwargs["client_args"]
    assert ca["base_url"] == "https://bedrock-mantle.us-east-1.api.aws/v1"
    assert ca["api_key"] == "tok-123"


def test_default_model_is_sonnet_5(monkeypatch, fake_openai_model):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-x")
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    import shared.bedrock as mod

    mod.build_model()
    assert fake_openai_model.call_args.kwargs["model_id"] == "anthropic.claude-sonnet-5"


def test_project_arg_overrides_env(monkeypatch, fake_openai_model):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-x")
    monkeypatch.setenv("MANTLE_PROJECT_ID", "fleet-project")
    import shared.bedrock as mod

    # An explicit project arg still wins over the env default.
    mod.build_model(project="proj-override")
    assert _headers(fake_openai_model)["OpenAI-Project"] == "proj-override"


def test_project_defaults_to_env(monkeypatch, fake_openai_model):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-x")
    monkeypatch.setenv("MANTLE_PROJECT_ID", "fleet-project")
    import shared.bedrock as mod

    # No arg → the fleet-wide MANTLE_PROJECT_ID env is used for attribution.
    mod.build_model()
    assert _headers(fake_openai_model)["OpenAI-Project"] == "fleet-project"


def test_no_project_omits_header(monkeypatch, fake_openai_model):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-x")
    monkeypatch.delenv("MANTLE_PROJECT_ID", raising=False)
    import shared.bedrock as mod

    mod.build_model()
    assert "OpenAI-Project" not in _headers(fake_openai_model)


def test_missing_guardrail_id_raises(monkeypatch, fake_openai_model):
    monkeypatch.delenv("BEDROCK_GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("SDLC_ALLOW_MISSING_GUARDRAIL", raising=False)
    import shared.bedrock as mod

    with pytest.raises(RuntimeError, match="BEDROCK_GUARDRAIL_ID"):
        mod.build_model()
    fake_openai_model.assert_not_called()


def test_missing_guardrail_escape_hatch(monkeypatch, fake_openai_model, caplog):
    monkeypatch.delenv("BEDROCK_GUARDRAIL_ID", raising=False)
    monkeypatch.setenv("SDLC_ALLOW_MISSING_GUARDRAIL", "1")
    import shared.bedrock as mod

    with caplog.at_level("WARNING"):
        mod.build_model()
    # No guardrail headers when the escape hatch is on.
    h = _headers(fake_openai_model)
    assert "X-Amzn-Bedrock-GuardrailIdentifier" not in h
    assert any("SDLC_ALLOW_MISSING_GUARDRAIL" in r.message for r in caplog.records)


def test_extra_kwargs_forwarded(monkeypatch, fake_openai_model):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-x")
    import shared.bedrock as mod

    mod.build_model(max_tokens=16000)
    assert fake_openai_model.call_args.kwargs["max_tokens"] == 16000
