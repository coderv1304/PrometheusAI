from __future__ import annotations

import pytest
from pydantic import ValidationError

from prometheus_ai.config import Settings


def test_dev_defaults_are_allowed() -> None:
    settings = Settings(_env_file=None, env="dev")
    assert settings.is_dev
    assert settings.dry_run is True
    assert settings.webhook_auth_required is False


def test_production_refuses_default_secrets() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, env="prod")
    message = str(exc.value)
    assert "PROMETHEUS_AI_APPROVER_TOKEN" in message
    assert "PROMETHEUS_AI_WEBHOOK_TOKEN" in message


def test_production_accepts_explicit_secrets() -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        approver_token="a" * 32,
        webhook_token="w" * 32,
        policy_mode="http",
        policy_token="p" * 32,
    )
    assert settings.webhook_auth_required is True
    assert settings.policy_mode == "http"


def test_http_policy_mode_requires_a_real_token_outside_dev() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            env="production",
            approver_token="a" * 32,
            webhook_token="w" * 32,
            policy_mode="http",
        )
    assert "PROMETHEUS_AI_POLICY_TOKEN" in str(exc.value)


def test_empty_approver_token_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, env="test", approver_token="  ")


def test_safe_summary_never_contains_secrets() -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        approver_token="super-secret-approver",
        webhook_token="super-secret-webhook",
        policy_token="super-secret-policy",
    )
    rendered = str(settings.safe_summary())
    assert "super-secret" not in rendered
