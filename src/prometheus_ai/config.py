"""Runtime configuration.

Everything is env-driven so the same image runs locally, in CI and on the demo
cluster. Secrets never live in code; the defaults below are development-only.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelBackend = Literal["fake", "ollama", "bedrock"]
PolicyMode = Literal["inprocess", "http"]
CheckpointBackend = Literal["memory", "postgres"]
InventoryBackend = Literal["auto", "fake", "kubectl"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROMETHEUS_AI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "dev"
    database_url: str | None = None
    approver_token: str = "dev-approval-token"
    webhook_token: str = ""
    enable_docs: bool = True
    max_request_bytes: int = Field(default=1_000_000, ge=1024, le=16_000_000)
    audit_input_redaction: bool = True

    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    demo_namespace: str = "demo"
    monitoring_namespace: str = "monitoring"

    model_backend: ModelBackend = "fake"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    bedrock_model: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    bedrock_region: str = "us-east-1"
    cost_cap_usd: float = 15.0

    max_tool_calls: int = 6
    max_rehypothesis: int = 2
    max_writes_per_hour: int = 3
    node_timeout_seconds: float = 60.0
    approval_ttl_minutes: int = 30
    kill_switch: bool = False
    dry_run: bool = True

    policy_mode: PolicyMode = "inprocess"
    policy_url: str = "http://localhost:8081"
    policy_token: str = "dev-policy-token"

    checkpoint_backend: CheckpointBackend = "memory"
    model_timeout_seconds: float = 45.0
    http_timeout_seconds: float = 10.0
    inventory_backend: InventoryBackend = "auto"

    allowed_namespaces: list[str] = Field(default_factory=lambda: ["demo"])
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["rollout_restart", "scale", "rollout_undo"]
    )

    @field_validator("approver_token")
    @classmethod
    def _no_empty_token(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approver token must not be empty")
        return value

    @model_validator(mode="after")
    def _refuse_insecure_defaults_outside_dev(self) -> Settings:
        """Fail fast instead of shipping demo credentials to a real environment."""
        if self.is_dev:
            return self
        insecure: list[str] = []
        if self.approver_token == "dev-approval-token":
            insecure.append("PROMETHEUS_AI_APPROVER_TOKEN is the development default")
        if not self.webhook_token:
            insecure.append("PROMETHEUS_AI_WEBHOOK_TOKEN is empty")
        if self.policy_mode == "http" and self.policy_token == "dev-policy-token":
            insecure.append("PROMETHEUS_AI_POLICY_TOKEN is the development default")
        if insecure:
            raise ValueError(
                "refusing to start with an insecure configuration: " + "; ".join(insecure)
            )
        return self

    @property
    def is_dev(self) -> bool:
        return self.env.lower() in {"dev", "test", "local"}

    @property
    def webhook_auth_required(self) -> bool:
        return bool(self.webhook_token.strip())

    def safe_summary(self) -> dict[str, object]:
        """Settings that are safe to log. Never includes tokens or URLs with creds."""
        return {
            "env": self.env,
            "model_backend": self.model_backend,
            "policy_mode": self.policy_mode,
            "checkpoint_backend": self.checkpoint_backend,
            "inventory_backend": self.inventory_backend,
            "kill_switch": self.kill_switch,
            "dry_run": self.dry_run,
            "enable_docs": self.enable_docs,
            "webhook_auth": self.webhook_auth_required,
            "demo_namespace": self.demo_namespace,
            "max_tool_calls": self.max_tool_calls,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
