"""SQLite model registry keyed by (provider, model_id).

The registry is the single source of truth for model configuration:

- which model is active (exactly one row has ``is_active = 1``);
- per-model metadata: endpoint, API protocol, key env var, context window;
- per-model request parameters: max output tokens and reasoning effort.

At launch the adapter renders the enabled rows into a DSH ``llm-pi-ai`` patch
and passes it to the SDK, and builds the request parameters from the active
row. Nothing model-specific lives in Python code or env vars.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    provider          TEXT NOT NULL,
    model_id          TEXT NOT NULL,
    display_name      TEXT NOT NULL,
    api               TEXT NOT NULL DEFAULT 'openai-completions',
    base_url          TEXT NOT NULL,
    api_key_env       TEXT NOT NULL DEFAULT 'LLM_API_KEY',
    context_window    INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL DEFAULT 2048,
    reasoning_effort  TEXT NOT NULL DEFAULT 'low',
    capabilities      TEXT NOT NULL DEFAULT '{}',
    enabled           INTEGER NOT NULL DEFAULT 1,
    is_active         INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY (provider, model_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_models_single_active
    ON models (is_active) WHERE is_active = 1;
"""

# Columns added after the first release; created on existing databases.
_MIGRATIONS = {
    "reasoning_effort": "ALTER TABLE models ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'low'",
    "is_active": "ALTER TABLE models ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0",
}

PROVIDER_DISPLAY = {
    "opencode": "OpenCode Zen",
    "opencode-go": "OpenCode Go",
    "fake": "Fake (local test)",
}

_EFFORTS = {
    "reasoning_efforts": ["low", "medium", "high"],
    # The active OpenCode model accepts images; declaring it also enables the
    # DSH read_image tool for on-demand reads of stored originals.
    "input": ["text", "image"],
}

DEFAULT_ACTIVE = ("opencode-go", "deepseek-v4.1-flash")

# Providers DSH already defines. Their route patch may only override connection
# metadata: setting `api` would replace the built-in adapter, and with it the
# wrappers (OpenCode Go adds x-opencode-session from the DSH session id).
BUILTIN_PROVIDERS = {"opencode", "opencode-go"}

# Seeded only when the table is empty. Edit rows with --set-model afterwards.
DEFAULTS: list[dict[str, Any]] = [
    {
        "provider": "opencode-go", "model_id": "deepseek-v4.1-flash",
        "display_name": "DeepSeek V4.1 Flash",
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key_env": "OPENCODE_API_KEY", "context_window": 1_000_000,
        "max_output_tokens": 2048, "reasoning_effort": "low", "capabilities": _EFFORTS,
    },
    # Only the paid Flash is seeded. The Flash free tier only works inside the
    # OpenCode client (403 FreeTierError elsewhere), beam-free is geo-blocked
    # from cn-hangzhou (403 RegionError), and local test routes such as the fake
    # model are registered ad hoc with --set-model.
]

# Fields --set-model may change, with their parsers.
EDITABLE: dict[str, Any] = {
    "display_name": str,
    "api": str,
    "base_url": str,
    "api_key_env": str,
    "context_window": int,
    "max_output_tokens": int,
    "reasoning_effort": str,
    "capabilities": json.loads,
    "enabled": lambda v: 1 if str(v).strip().lower() in ("1", "true", "yes", "on") else 0,
}


def parse_key(key: str) -> tuple[str, str]:
    provider, sep, model_id = key.partition("/")
    if not sep or not provider or not model_id:
        raise ValueError(f"model key must look like provider/model_id, got {key!r}")
    return provider, model_id


@dataclass(frozen=True)
class ModelRecord:
    provider: str
    model_id: str
    display_name: str
    api: str
    base_url: str
    api_key_env: str
    context_window: int
    max_output_tokens: int
    reasoning_effort: str
    capabilities: dict[str, Any]
    enabled: bool
    is_active: bool
    updated_at: int

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model_id}"

    @property
    def reasoning_efforts(self) -> list[str]:
        efforts = self.capabilities.get("reasoning_efforts") or []
        return [str(e) for e in efforts if str(e).strip()]

    @property
    def effective_effort(self) -> str:
        """The effort sent upstream: empty when unset or not declared by the model."""
        effort = self.reasoning_effort.strip()
        return effort if effort and effort in self.reasoning_efforts else ""


def _record(row: sqlite3.Row) -> ModelRecord:
    try:
        capabilities = json.loads(row["capabilities"] or "{}")
    except json.JSONDecodeError:
        capabilities = {}
    return ModelRecord(
        provider=row["provider"],
        model_id=row["model_id"],
        display_name=row["display_name"],
        api=row["api"],
        base_url=row["base_url"],
        api_key_env=row["api_key_env"],
        context_window=int(row["context_window"]),
        max_output_tokens=int(row["max_output_tokens"]),
        reasoning_effort=row["reasoning_effort"] or "",
        capabilities=capabilities if isinstance(capabilities, dict) else {},
        enabled=bool(row["enabled"]),
        is_active=bool(row["is_active"]),
        updated_at=int(row["updated_at"]),
    )


class ModelRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self.seed_defaults()
        self.ensure_active()

    # ------------------------------------------------------------------ setup

    def _migrate(self) -> None:
        exists = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'models'"
        ).fetchone()
        if not exists:
            return
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(models)")}
        added = [name for name in _MIGRATIONS if name not in columns]
        for name in added:
            self._db.execute(_MIGRATIONS[name])
        # The seeded OpenCode rows moved from the pay-as-you-go `opencode`
        # route to the built-in `opencode-go` route.
        self._db.execute(
            """
            UPDATE models SET provider = 'opencode-go'
            WHERE provider = 'opencode'
              AND model_id IN ('deepseek-v4.1-flash', 'deepseek-v4.1-flash-free')
              AND NOT EXISTS (
                  SELECT 1 FROM models other
                  WHERE other.provider = 'opencode-go' AND other.model_id = models.model_id
              )
            """
        )
        if "reasoning_effort" in added:
            # Older rows had no effort metadata; give seeded models their defaults.
            for row in DEFAULTS:
                self._db.execute(
                    "UPDATE models SET capabilities = ? "
                    "WHERE provider = ? AND model_id = ? AND capabilities IN ('', '{}')",
                    (json.dumps(row["capabilities"]), row["provider"], row["model_id"]),
                )
        # The seeded model gained image input after the first release; keep
        # existing registries in sync without touching the user's other edits.
        seed = self._db.execute(
            "SELECT capabilities FROM models WHERE provider = ? AND model_id = ?",
            DEFAULT_ACTIVE,
        ).fetchone()
        if seed is not None:
            try:
                capabilities = json.loads(seed["capabilities"] or "{}")
            except json.JSONDecodeError:
                capabilities = {}
            if isinstance(capabilities, dict) and "input" not in capabilities:
                capabilities["input"] = ["text", "image"]
                self._db.execute(
                    "UPDATE models SET capabilities = ? WHERE provider = ? AND model_id = ?",
                    (json.dumps(capabilities), *DEFAULT_ACTIVE),
                )
        self._db.commit()

    def seed_defaults(self) -> None:
        count = int(self._db.execute("SELECT COUNT(*) FROM models").fetchone()[0])
        if count:
            return
        now = int(time.time())
        for row in DEFAULTS:
            self._db.execute(
                """
                INSERT INTO models
                    (provider, model_id, display_name, api, base_url, api_key_env,
                     context_window, max_output_tokens, reasoning_effort, capabilities,
                     enabled, is_active, updated_at)
                VALUES (?, ?, ?, 'openai-completions', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    row["provider"], row["model_id"], row["display_name"],
                    row["base_url"], row["api_key_env"], row["context_window"],
                    row.get("max_output_tokens", 2048), row.get("reasoning_effort", ""),
                    json.dumps(row.get("capabilities", {})),
                    1 if (row["provider"], row["model_id"]) == DEFAULT_ACTIVE else 0,
                    now,
                ),
            )
        self._db.commit()

    def ensure_active(self) -> None:
        if self.active() is not None:
            return
        fallback = self.get(*DEFAULT_ACTIVE)
        if fallback is None or not fallback.enabled:
            enabled = [row for row in self.list() if row.enabled]
            fallback = enabled[0] if enabled else None
        if fallback is not None:
            self.use(fallback.provider, fallback.model_id)

    # ------------------------------------------------------------------ reads

    def get(self, provider: str, model_id: str) -> ModelRecord | None:
        row = self._db.execute(
            "SELECT * FROM models WHERE provider = ? AND model_id = ?",
            (provider, model_id),
        ).fetchone()
        return _record(row) if row else None

    def active(self) -> ModelRecord | None:
        row = self._db.execute("SELECT * FROM models WHERE is_active = 1").fetchone()
        return _record(row) if row else None

    def active_signature(self) -> tuple | None:
        """Value identifying the active route and its request parameters.

        The adapter compares this on each turn to notice --use-model /
        --set-model changes without restarting the process.
        """
        record = self.active()
        if record is None:
            return None
        return (
            record.key, record.api, record.base_url, record.api_key_env,
            record.context_window, record.max_output_tokens,
            record.effective_effort, record.enabled,
        )

    def list(self, provider: str | None = None) -> list[ModelRecord]:
        if provider:
            rows = self._db.execute(
                "SELECT * FROM models WHERE provider = ? ORDER BY provider, model_id",
                (provider,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM models ORDER BY provider, model_id"
            ).fetchall()
        return [_record(row) for row in rows]

    # ----------------------------------------------------------------- writes

    def use(self, provider: str, model_id: str) -> ModelRecord:
        """Make (provider, model_id) the single active model."""
        record = self.get(provider, model_id)
        if record is None:
            raise ValueError(f"model {provider}/{model_id} is not registered")
        if not record.enabled:
            raise ValueError(f"model {provider}/{model_id} is disabled")
        with self._db:
            self._db.execute("UPDATE models SET is_active = 0 WHERE is_active = 1")
            self._db.execute(
                "UPDATE models SET is_active = 1, updated_at = ? WHERE provider = ? AND model_id = ?",
                (int(time.time()), provider, model_id),
            )
        return self.get(provider, model_id)  # type: ignore[return-value]

    def upsert(self, provider: str, model_id: str, fields: dict[str, str]) -> ModelRecord:
        """Create or update one row from string field values."""
        unknown = set(fields) - set(EDITABLE)
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        try:
            values = {name: EDITABLE[name](raw) for name, raw in fields.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid field value: {exc}") from exc
        if "capabilities" in values:
            if not isinstance(values["capabilities"], dict):
                raise ValueError("capabilities must be a JSON object")
            values["capabilities"] = json.dumps(values["capabilities"], ensure_ascii=False)
        for name in ("context_window", "max_output_tokens"):
            if name in values and values[name] <= 0:
                raise ValueError(f"{name} must be positive")

        current = self.get(provider, model_id)
        now = int(time.time())
        with self._db:
            if current is None:
                missing = [n for n in ("base_url", "context_window") if n not in values]
                if missing:
                    raise ValueError(f"new model needs: {', '.join(missing)}")
                values.setdefault("display_name", model_id)
                if "reasoning_effort" not in values:
                    # Default to "low" only when the model declares that effort.
                    declared = json.loads(values.get("capabilities", "{}")).get("reasoning_efforts") or []
                    values["reasoning_effort"] = "low" if "low" in declared else ""
                columns = ["provider", "model_id", *values, "updated_at"]
                self._db.execute(
                    f"INSERT INTO models ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    (provider, model_id, *values.values(), now),
                )
            elif values:
                assignments = ", ".join(f"{name} = ?" for name in values)
                self._db.execute(
                    f"UPDATE models SET {assignments}, updated_at = ? "
                    "WHERE provider = ? AND model_id = ?",
                    (*values.values(), now, provider, model_id),
                )

        record = self.get(provider, model_id)
        assert record is not None
        if record.reasoning_effort and record.reasoning_effort not in record.reasoning_efforts:
            raise ValueError(
                f"reasoning_effort {record.reasoning_effort!r} is not declared in "
                f"capabilities.reasoning_efforts {record.reasoning_efforts} for {record.key}"
            )
        if record.is_active and not record.enabled:
            raise ValueError(f"cannot disable the active model {record.key}; switch first")
        return record

    # --------------------------------------------------------------- render

    def write_dsh_patch(self, path: str | Path) -> Path:
        """Render the enabled rows as the complete DSH `llm-pi-ai` provider config."""
        providers: dict[str, dict] = {}
        for row in self.list():
            if not row.enabled:
                continue
            route = providers.setdefault(row.provider, {
                "displayName": PROVIDER_DISPLAY.get(row.provider, row.provider),
                "apiKeyEnv": row.api_key_env,
                "baseURL": row.base_url,
                "models": [],
            })
            if row.provider not in BUILTIN_PROVIDERS:
                route.setdefault("api", row.api)
                if route["api"] != row.api:
                    raise ValueError(
                        f"inconsistent provider metadata for {row.provider!r}: "
                        f"{row.model_id!r} disagrees on api"
                    )
            if (route["apiKeyEnv"], route["baseURL"]) != (row.api_key_env, row.base_url):
                raise ValueError(
                    f"inconsistent provider metadata for {row.provider!r}: "
                    f"{row.model_id!r} disagrees on baseURL/apiKeyEnv"
                )
            entry: dict[str, Any] = {
                "id": row.model_id,
                "name": row.display_name,
                "contextWindow": row.context_window,
            }
            if row.reasoning_efforts:
                entry["reasoningEfforts"] = {effort: effort for effort in row.reasoning_efforts}
            declared_input = row.capabilities.get("input")
            if isinstance(declared_input, list):
                modalities = [str(value).strip() for value in declared_input if str(value).strip()]
                if modalities:
                    entry["input"] = modalities
            route["models"].append(entry)

        patch = [{"id": "llm-pi-ai", "config": {"providers": providers}}]
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(target, 0o600)
        return target

    def close(self) -> None:
        self._db.close()
