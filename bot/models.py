"""SQLite provider + model registry.

Providers own the connection metadata shared by every model behind them:
protocol, endpoint and the environment variable that holds the API key. Models
belong to a provider and carry per-model metadata: display name, context
window, output limit, reasoning effort and capabilities.

The registry is the single source of truth for model configuration:

- which model is active (exactly one ``models`` row has ``is_active = 1``);
- provider connection metadata (``providers``);
- per-model request parameters (context window, max output, effort).

At launch the adapter renders enabled providers and their enabled models into
a DSH ``llm-pi-ai`` patch and passes it to the SDK, and builds the request
parameters from the active row. Nothing model-specific lives in Python code or
env vars.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROVIDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    provider     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    api          TEXT NOT NULL DEFAULT 'openai-completions',
    base_url     TEXT NOT NULL DEFAULT '',
    api_key_env  TEXT NOT NULL DEFAULT 'LLM_API_KEY',
    api_key      TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
"""

_MODELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    provider          TEXT NOT NULL,
    model_id          TEXT NOT NULL,
    display_name      TEXT NOT NULL,
    context_window    INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL DEFAULT 2048,
    reasoning_effort  TEXT NOT NULL DEFAULT '',
    capabilities      TEXT NOT NULL DEFAULT '{}',
    enabled           INTEGER NOT NULL DEFAULT 1,
    is_active         INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY (provider, model_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_models_single_active
    ON models (is_active) WHERE is_active = 1;
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_SCHEMA = _PROVIDERS_SCHEMA + _MODELS_SCHEMA

# Columns added to the first release's table; created on existing databases.
_MIGRATIONS = {
    "reasoning_effort": "ALTER TABLE models ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'low'",
    "is_active": "ALTER TABLE models ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0",
}

# Columns added to the providers table after it was introduced.
_PROVIDER_MIGRATIONS = {
    "api_key": "ALTER TABLE providers ADD COLUMN api_key TEXT NOT NULL DEFAULT ''",
}

# Before the providers table existed these connection fields lived on every
# model row and had to be identical within a provider.
_LEGACY_CONNECTION_COLUMNS = {"api", "base_url", "api_key_env"}

PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Providers DSH already defines. Their route patch may only override connection
# metadata: setting `api` would replace the built-in adapter, and with it the
# wrappers (OpenCode Go adds x-opencode-session from the DSH session id).
BUILTIN_PROVIDERS = {"opencode", "opencode-go"}

PROVIDER_DISPLAY = {
    "opencode": "OpenCode Zen",
    "opencode-go": "OpenCode Go",
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "siliconflow": "SiliconFlow",
    "dashscope": "阿里云百炼",
    "moonshot": "Moonshot",
    "zhipu": "智谱 AI",
    "fake": "Fake (local test)",
}

# Values the admin UI uses to prefill "add provider". `custom` intentionally
# leaves base_url empty: the user supplies their own OpenAI-compatible endpoint.
PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "provider": "opencode-go", "display_name": "OpenCode Go",
        "api": "openai-completions", "base_url": "https://opencode.ai/zen/go/v1",
        "api_key_env": "OPENCODE_API_KEY", "builtin": True,
    },
    {
        "provider": "opencode", "display_name": "OpenCode Zen",
        "api": "openai-completions", "base_url": "https://opencode.ai/inference/openai/v1",
        "api_key_env": "OPENCODE_API_KEY", "builtin": True,
    },
    {
        "provider": "deepseek", "display_name": "DeepSeek",
        "api": "openai-completions", "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY", "builtin": False,
    },
    {
        "provider": "openai", "display_name": "OpenAI",
        "api": "openai-completions", "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY", "builtin": False,
    },
    {
        "provider": "openrouter", "display_name": "OpenRouter",
        "api": "openai-completions", "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY", "builtin": False,
    },
    {
        "provider": "siliconflow", "display_name": "SiliconFlow",
        "api": "openai-completions", "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY", "builtin": False,
    },
    {
        "provider": "dashscope", "display_name": "阿里云百炼",
        "api": "openai-completions",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY", "builtin": False,
    },
    {
        "provider": "moonshot", "display_name": "Moonshot",
        "api": "openai-completions", "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY", "builtin": False,
    },
    {
        "provider": "zhipu", "display_name": "智谱 AI",
        "api": "openai-completions", "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY", "builtin": False,
    },
    {
        "provider": "fake", "display_name": "Fake (local test)",
        "api": "openai-completions", "base_url": "http://127.0.0.1:8000/v1",
        "api_key_env": "FAKE_KEY", "builtin": False,
    },
    {
        "provider": "custom", "display_name": "自定义 OpenAI 兼容端点",
        "api": "openai-completions", "base_url": "",
        "api_key_env": "LLM_API_KEY", "builtin": False,
    },
]

_EFFORTS = {
    "reasoning_efforts": ["low", "medium", "high"],
    # The active OpenCode model accepts images; declaring it also enables the
    # DSH read_image tool for on-demand reads of stored originals.
    "input": ["text", "image"],
}

DEFAULT_ACTIVE = ("opencode-go", "deepseek-v4.1-flash")

DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "opencode-go": {
        "display_name": "OpenCode Go",
        "api": "openai-completions",
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key_env": "OPENCODE_API_KEY",
    },
}

# Seeded only when the database has never been seeded. Edit rows with the
# admin UI or --set-model afterwards.
DEFAULTS: list[dict[str, Any]] = [
    {
        "provider": "opencode-go", "model_id": "deepseek-v4.1-flash",
        "display_name": "DeepSeek V4.1 Flash",
        "context_window": 1_000_000,
        "max_output_tokens": 2048, "reasoning_effort": "low", "capabilities": _EFFORTS,
    },
    # Only the paid Flash is seeded. The Flash free tier only works inside the
    # OpenCode client (403 FreeTierError elsewhere), beam-free is geo-blocked
    # from cn-hangzhou (403 RegionError), and local test routes such as the fake
    # model are registered ad hoc with --set-model.
]


def _as_bool(value: Any) -> int:
    return 1 if str(value).strip().lower() in ("1", "true", "yes", "on") else 0


# Fields the admin UI / CLI may change on a provider.
PROVIDER_EDITABLE: dict[str, Any] = {
    "display_name": str,
    "api": str,
    "base_url": str,
    "api_key_env": str,
    "api_key": str,
    "enabled": _as_bool,
}

# Fields the admin UI / CLI may change on a model.
MODEL_EDITABLE: dict[str, Any] = {
    "display_name": str,
    "context_window": int,
    "max_output_tokens": int,
    "reasoning_effort": str,
    "capabilities": json.loads,
    "enabled": _as_bool,
}

# Connection fields remain accepted by --set-model for backward compatibility;
# they are written to the provider instead of the model row.
PROVIDER_FIELDS = ("api", "base_url", "api_key_env", "api_key")
EDITABLE: dict[str, Any] = {**MODEL_EDITABLE, **PROVIDER_EDITABLE}


def parse_key(key: str) -> tuple[str, str]:
    provider, sep, model_id = key.partition("/")
    if not sep or not provider or not model_id:
        raise ValueError(f"model key must look like provider/model_id, got {key!r}")
    return provider, model_id


def provider_key_env(provider: str) -> str:
    """Synthetic env ref used to hand a DB-stored key to the DSH runtime.

    DSH resolves provider credentials from its inherited environment (after the
    managed credentials file). The bot sets this variable in-process right
    before the runtime starts, so the key never has to be written to the patch.
    """
    safe = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_") or "PROVIDER"
    digest = hashlib.sha1(provider.encode("utf-8")).hexdigest()[:8].upper()
    return f"QQBOT_PK_{safe}_{digest}"


@dataclass(frozen=True)
class ProviderRecord:
    provider: str
    display_name: str
    api: str
    base_url: str
    api_key_env: str
    api_key: str
    enabled: bool
    created_at: int
    updated_at: int
    model_count: int = 0
    enabled_models: int = 0
    active_model_id: str | None = None

    @property
    def builtin(self) -> bool:
        return self.provider in BUILTIN_PROVIDERS

    @property
    def has_stored_key(self) -> bool:
        return bool(self.api_key.strip())


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
    provider_display_name: str = ""
    provider_enabled: bool = True
    api_key: str = ""

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


def _provider_record(row: sqlite3.Row) -> ProviderRecord:
    keys = row.keys()
    return ProviderRecord(
        provider=str(row["provider"]),
        display_name=str(row["display_name"] or row["provider"]),
        api=str(row["api"] or ""),
        base_url=str(row["base_url"] or ""),
        api_key_env=str(row["api_key_env"] or ""),
        api_key=str(row["api_key"] or ""),
        enabled=bool(row["enabled"]),
        created_at=int(row["created_at"] or 0),
        updated_at=int(row["updated_at"] or 0),
        model_count=int(row["model_count"]) if "model_count" in keys else 0,
        enabled_models=int(row["enabled_models"]) if "enabled_models" in keys else 0,
        active_model_id=(
            str(row["active_model_id"])
            if "active_model_id" in keys and row["active_model_id"]
            else None
        ),
    )


def _record(row: sqlite3.Row) -> ModelRecord:
    try:
        capabilities = json.loads(row["capabilities"] or "{}")
    except json.JSONDecodeError:
        capabilities = {}
    return ModelRecord(
        provider=str(row["provider"]),
        model_id=str(row["model_id"]),
        display_name=str(row["display_name"] or row["model_id"]),
        api=str(row["api"] or ""),
        base_url=str(row["base_url"] or ""),
        api_key_env=str(row["api_key_env"] or ""),
        context_window=int(row["context_window"]),
        max_output_tokens=int(row["max_output_tokens"]),
        reasoning_effort=str(row["reasoning_effort"] or ""),
        capabilities=capabilities if isinstance(capabilities, dict) else {},
        enabled=bool(row["enabled"]),
        is_active=bool(row["is_active"]),
        updated_at=int(row["updated_at"]),
        provider_display_name=str(row["provider_display_name"] or row["provider"]),
        provider_enabled=bool(row["provider_enabled"]),
        api_key=str(row["provider_api_key"] or ""),
    )


_MODEL_SELECT = """
SELECT m.*, p.display_name AS provider_display_name,
       p.enabled AS provider_enabled, p.api AS api, p.base_url AS base_url,
       p.api_key_env AS api_key_env, p.api_key AS provider_api_key
FROM models m
JOIN providers p ON p.provider = m.provider
"""


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
        self._ensure_providers_for_models()
        self.seed_defaults()
        self.ensure_active()
        self._secure_files()

    # ------------------------------------------------------------------ setup

    def _secure_files(self) -> None:
        """Keep the registry (which can hold API keys) owner-only."""
        for suffix in ("", "-wal", "-shm"):
            target = Path(f"{self.path}{suffix}")
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass

    def _table_exists(self, name: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        if self._table_exists("providers"):
            provider_columns = self._columns("providers")
            for name, ddl in _PROVIDER_MIGRATIONS.items():
                if name not in provider_columns:
                    self._db.execute(ddl)
        if not self._table_exists("models"):
            return
        columns = self._columns("models")
        for name, ddl in _MIGRATIONS.items():
            if name not in columns:
                self._db.execute(ddl)
                columns.add(name)
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
        if "reasoning_effort" in columns:
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
        if _LEGACY_CONNECTION_COLUMNS <= columns:
            self._split_legacy_connection_fields()
        self._db.commit()

    def _split_legacy_connection_fields(self) -> None:
        """Move per-model connection fields into one providers row per provider."""
        self._db.executescript(_PROVIDERS_SCHEMA)
        now = int(time.time())
        rows = self._db.execute(
            "SELECT provider, api, base_url, api_key_env, updated_at FROM models "
            "ORDER BY provider, is_active DESC, updated_at DESC"
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            provider = str(row["provider"])
            if provider in seen:
                continue
            seen.add(provider)
            self._db.execute(
                """
                INSERT OR IGNORE INTO providers
                    (provider, display_name, api, base_url, api_key_env,
                     enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    provider,
                    PROVIDER_DISPLAY.get(provider, provider),
                    str(row["api"] or "openai-completions"),
                    str(row["base_url"] or ""),
                    str(row["api_key_env"] or "LLM_API_KEY"),
                    now,
                    int(row["updated_at"] or now),
                ),
            )
        self._db.executescript(
            """
            CREATE TABLE models_v2 (
                provider          TEXT NOT NULL,
                model_id          TEXT NOT NULL,
                display_name      TEXT NOT NULL,
                context_window    INTEGER NOT NULL,
                max_output_tokens INTEGER NOT NULL DEFAULT 2048,
                reasoning_effort  TEXT NOT NULL DEFAULT '',
                capabilities      TEXT NOT NULL DEFAULT '{}',
                enabled           INTEGER NOT NULL DEFAULT 1,
                is_active         INTEGER NOT NULL DEFAULT 0,
                updated_at        INTEGER NOT NULL,
                PRIMARY KEY (provider, model_id)
            );
            INSERT INTO models_v2
                (provider, model_id, display_name, context_window, max_output_tokens,
                 reasoning_effort, capabilities, enabled, is_active, updated_at)
            SELECT provider, model_id, display_name, context_window, max_output_tokens,
                   reasoning_effort, capabilities, enabled, is_active, updated_at
            FROM models;
            DROP TABLE models;
            ALTER TABLE models_v2 RENAME TO models;
            """
        )

    def _ensure_providers_for_models(self) -> None:
        """Repair model rows left without a provider (manual edits / old DBs)."""
        rows = self._db.execute(
            "SELECT DISTINCT m.provider FROM models m "
            "LEFT JOIN providers p ON p.provider = m.provider "
            "WHERE p.provider IS NULL"
        ).fetchall()
        if not rows:
            return
        now = int(time.time())
        for row in rows:
            provider = str(row["provider"])
            self._db.execute(
                "INSERT OR IGNORE INTO providers "
                "(provider, display_name, api, base_url, api_key_env, enabled, "
                " created_at, updated_at) VALUES (?, ?, ?, '', ?, 1, ?, ?)",
                (
                    provider, PROVIDER_DISPLAY.get(provider, provider),
                    "openai-completions", "LLM_API_KEY", now, now,
                ),
            )
        self._db.commit()

    def seed_defaults(self) -> None:
        seeded = self._db.execute(
            "SELECT value FROM meta WHERE key = 'seeded'"
        ).fetchone()
        if seeded is not None:
            return
        count = int(self._db.execute("SELECT COUNT(*) FROM models").fetchone()[0])
        if count == 0:
            now = int(time.time())
            for provider, values in DEFAULT_PROVIDERS.items():
                self._db.execute(
                    "INSERT OR IGNORE INTO providers "
                    "(provider, display_name, api, base_url, api_key_env, enabled, "
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        provider, values["display_name"], values["api"],
                        values["base_url"], values["api_key_env"], now, now,
                    ),
                )
            for row in DEFAULTS:
                self._db.execute(
                    """
                    INSERT INTO models
                        (provider, model_id, display_name, context_window,
                         max_output_tokens, reasoning_effort, capabilities,
                         enabled, is_active, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        row["provider"], row["model_id"], row["display_name"],
                        row["context_window"], row.get("max_output_tokens", 2048),
                        row.get("reasoning_effort", ""),
                        json.dumps(row.get("capabilities", {})),
                        1 if (row["provider"], row["model_id"]) == DEFAULT_ACTIVE else 0,
                        now,
                    ),
                )
        self._db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')"
        )
        self._db.commit()

    def ensure_active(self) -> None:
        active = self.active()
        if active is not None and active.enabled and active.provider_enabled:
            return
        fallback = self.get(*DEFAULT_ACTIVE)
        if fallback is None or not fallback.enabled or not fallback.provider_enabled:
            enabled = [
                row for row in self.list() if row.enabled and row.provider_enabled
            ]
            fallback = enabled[0] if enabled else None
        if fallback is not None:
            self.use(fallback.provider, fallback.model_id)

    # ------------------------------------------------------------------ reads

    def get_provider(self, provider: str) -> ProviderRecord | None:
        row = self._db.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM models m WHERE m.provider = p.provider) AS model_count, "
            "(SELECT COUNT(*) FROM models m WHERE m.provider = p.provider AND m.enabled = 1) "
            "AS enabled_models, "
            "(SELECT m.model_id FROM models m WHERE m.provider = p.provider "
            " AND m.is_active = 1 LIMIT 1) AS active_model_id "
            "FROM providers p WHERE p.provider = ?",
            (provider,),
        ).fetchone()
        return _provider_record(row) if row else None

    def list_providers(self) -> list[ProviderRecord]:
        rows = self._db.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM models m WHERE m.provider = p.provider) AS model_count, "
            "(SELECT COUNT(*) FROM models m WHERE m.provider = p.provider AND m.enabled = 1) "
            "AS enabled_models, "
            "(SELECT m.model_id FROM models m WHERE m.provider = p.provider "
            " AND m.is_active = 1 LIMIT 1) AS active_model_id "
            "FROM providers p ORDER BY p.provider"
        ).fetchall()
        return [_provider_record(row) for row in rows]

    def get(self, provider: str, model_id: str) -> ModelRecord | None:
        row = self._db.execute(
            _MODEL_SELECT + " WHERE m.provider = ? AND m.model_id = ?",
            (provider, model_id),
        ).fetchone()
        return _record(row) if row else None

    def active(self) -> ModelRecord | None:
        row = self._db.execute(_MODEL_SELECT + " WHERE m.is_active = 1").fetchone()
        return _record(row) if row else None

    def active_signature(self) -> tuple | None:
        """Value identifying the active route and its request parameters.

        The adapter compares this on each turn to notice --use-model /
        --set-model / admin-UI changes without restarting the process.
        """
        record = self.active()
        if record is None:
            return None
        key_ref = (
            f"stored:{hashlib.sha256(record.api_key.encode('utf-8')).hexdigest()[:16]}"
            if record.api_key
            else f"env:{record.api_key_env}"
        )
        return (
            record.key, record.api, record.base_url, key_ref,
            record.context_window, record.max_output_tokens,
            record.effective_effort, record.enabled, record.provider_enabled,
        )

    def list(self, provider: str | None = None) -> list[ModelRecord]:
        if provider:
            rows = self._db.execute(
                _MODEL_SELECT + " WHERE m.provider = ? ORDER BY m.provider, m.model_id",
                (provider,),
            ).fetchall()
        else:
            rows = self._db.execute(
                _MODEL_SELECT + " ORDER BY m.provider, m.model_id"
            ).fetchall()
        return [_record(row) for row in rows]

    # --------------------------------------------------------- provider writes

    def upsert_provider(self, provider: str, fields: dict[str, Any]) -> ProviderRecord:
        """Create or update one provider from string/JSON field values."""
        provider = str(provider or "").strip()
        if not PROVIDER_ID_RE.fullmatch(provider):
            raise ValueError(
                "provider id must be lowercase letters/digits/._- and start "
                f"with a letter or digit, got {provider!r}"
            )
        unknown = set(fields) - set(PROVIDER_EDITABLE)
        if unknown:
            raise ValueError(f"unknown provider field(s): {', '.join(sorted(unknown))}")
        try:
            values = {name: PROVIDER_EDITABLE[name](raw) for name, raw in fields.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid provider field value: {exc}") from exc

        current = self.get_provider(provider)
        now = int(time.time())
        with self._db:
            if current is None:
                if not str(values.get("base_url", "")).strip():
                    raise ValueError("new provider needs base_url")
                values.setdefault("display_name", PROVIDER_DISPLAY.get(provider, provider))
                values.setdefault("api", "openai-completions")
                # New providers are expected to carry a stored key (the admin
                # UI requires one). If a key is missing, reference a synthetic
                # name rather than accidentally borrowing LLM_API_KEY.
                values.setdefault("api_key_env", provider_key_env(provider))
                columns = ["provider", *values, "created_at", "updated_at"]
                self._db.execute(
                    f"INSERT INTO providers ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    (provider, *values.values(), now, now),
                )
            elif values:
                if "base_url" in values and not str(values["base_url"]).strip():
                    raise ValueError("provider base_url must not be empty")
                if "api" in values and not str(values["api"]).strip():
                    raise ValueError("provider api must not be empty")
                if "api_key_env" in values and not str(values["api_key_env"]).strip():
                    raise ValueError("provider api_key_env must not be empty")
                assignments = ", ".join(f"{name} = ?" for name in values)
                self._db.execute(
                    f"UPDATE providers SET {assignments}, updated_at = ? WHERE provider = ?",
                    (*values.values(), now, provider),
                )
            record = self.get_provider(provider)
            assert record is not None
            if not record.display_name.strip():
                raise ValueError("provider display_name must not be empty")
            if record.enabled and not record.base_url.strip():
                raise ValueError("an enabled provider needs a base_url")
            if record.enabled and not record.api_key_env.strip():
                raise ValueError("an enabled provider needs an api_key_env")
            if (
                record.enabled
                and record.provider not in BUILTIN_PROVIDERS
                and not record.api.strip()
            ):
                raise ValueError("an enabled provider needs an api protocol")
            if (
                current is not None
                and current.enabled
                and not record.enabled
                and record.active_model_id is not None
            ):
                raise ValueError(
                    f"cannot disable provider {provider!r}: it owns the active model "
                    f"{provider}/{record.active_model_id}; switch models first"
                )
        assert record is not None
        return record

    def delete_provider(self, provider: str) -> None:
        record = self.get_provider(provider)
        if record is None:
            raise ValueError(f"provider {provider!r} is not registered")
        if record.model_count:
            raise ValueError(
                f"provider {provider!r} still has {record.model_count} model(s); "
                "delete them first"
            )
        with self._db:
            self._db.execute("DELETE FROM providers WHERE provider = ?", (provider,))

    # ------------------------------------------------------------ model writes

    def use(self, provider: str, model_id: str) -> ModelRecord:
        """Make (provider, model_id) the single active model."""
        record = self.get(provider, model_id)
        if record is None:
            raise ValueError(f"model {provider}/{model_id} is not registered")
        if not record.enabled:
            raise ValueError(f"model {provider}/{model_id} is disabled")
        if not record.provider_enabled:
            raise ValueError(f"provider {provider} is disabled")
        with self._db:
            self._db.execute("UPDATE models SET is_active = 0 WHERE is_active = 1")
            self._db.execute(
                "UPDATE models SET is_active = 1, updated_at = ? WHERE provider = ? AND model_id = ?",
                (int(time.time()), provider, model_id),
            )
        updated = self.get(provider, model_id)
        assert updated is not None
        return updated

    def delete_model(self, provider: str, model_id: str) -> None:
        record = self.get(provider, model_id)
        if record is None:
            raise ValueError(f"model {provider}/{model_id} is not registered")
        if record.is_active:
            raise ValueError(
                f"cannot delete the active model {record.key}; switch models first"
            )
        with self._db:
            self._db.execute(
                "DELETE FROM models WHERE provider = ? AND model_id = ?",
                (provider, model_id),
            )

    def upsert(self, provider: str, model_id: str, fields: dict[str, Any]) -> ModelRecord:
        """Create or update one model row from string field values.

        Connection fields (``api`` / ``base_url`` / ``api_key_env``) are still
        accepted for compatibility with --set-model and are written to the
        provider, so every model behind that provider shares them.
        """
        provider = str(provider or "").strip()
        model_id = str(model_id or "").strip()
        if not provider or not model_id:
            raise ValueError("provider and model_id must not be empty")
        unknown = set(fields) - set(EDITABLE)
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        provider_values = {
            name: raw for name, raw in fields.items() if name in PROVIDER_FIELDS
        }
        model_values_raw = {
            name: raw for name, raw in fields.items() if name in MODEL_EDITABLE
        }
        try:
            values = {
                name: MODEL_EDITABLE[name](raw)
                for name, raw in model_values_raw.items()
            }
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid field value: {exc}") from exc
        if "capabilities" in values:
            if not isinstance(values["capabilities"], dict):
                raise ValueError("capabilities must be a JSON object")
            values["capabilities"] = json.dumps(values["capabilities"], ensure_ascii=False)
        for name in ("context_window", "max_output_tokens"):
            if name in values and values[name] <= 0:
                raise ValueError(f"{name} must be positive")
        if "display_name" in values and not values["display_name"].strip():
            values["display_name"] = model_id

        if provider_values:
            self.upsert_provider(provider, provider_values)
        elif self.get_provider(provider) is None:
            raise ValueError(
                f"provider {provider!r} is not registered; add it first "
                "(or pass base_url)"
            )

        current = self.get(provider, model_id)
        now = int(time.time())
        with self._db:
            if current is None:
                if "context_window" not in values:
                    raise ValueError("new model needs: context_window")
                values.setdefault("display_name", model_id)
                if "reasoning_effort" not in values:
                    declared = json.loads(values.get("capabilities", "{}")).get(
                        "reasoning_efforts"
                    ) or []
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
            if (
                record.reasoning_effort
                and record.reasoning_effort not in record.reasoning_efforts
            ):
                raise ValueError(
                    f"reasoning_effort {record.reasoning_effort!r} is not declared in "
                    f"capabilities.reasoning_efforts {record.reasoning_efforts} "
                    f"for {record.key}"
                )
            if record.is_active and not record.enabled:
                raise ValueError(
                    f"cannot disable the active model {record.key}; switch first"
                )
        return record

    # --------------------------------------------------------------- render

    def write_dsh_patch(self, path: str | Path) -> Path:
        """Render enabled providers/models as the DSH `llm-pi-ai` provider config."""
        providers: dict[str, dict] = {}
        for provider in self.list_providers():
            if not provider.enabled:
                continue
            rows = [row for row in self.list(provider.provider) if row.enabled]
            if not rows:
                continue
            if not provider.base_url.strip():
                raise ValueError(f"provider {provider.provider!r} has no base_url")
            if not provider.api_key_env.strip():
                raise ValueError(f"provider {provider.provider!r} has no api_key_env")
            key_ref = provider.api_key_env
            synthetic = provider_key_env(provider.provider)
            if provider.api_key.strip():
                # The DSH runtime inherits this process's environment when it
                # starts; the patch only carries the reference name.
                os.environ[synthetic] = provider.api_key.strip()
                key_ref = synthetic
            else:
                os.environ.pop(synthetic, None)
            route: dict[str, Any] = {
                "displayName": provider.display_name or provider.provider,
                "apiKeyEnv": key_ref,
                "baseURL": provider.base_url,
                "models": [],
            }
            if not provider.builtin:
                if not provider.api.strip():
                    raise ValueError(f"provider {provider.provider!r} has no api")
                route["api"] = provider.api
            for row in rows:
                entry: dict[str, Any] = {
                    "id": row.model_id,
                    "name": row.display_name,
                    "contextWindow": row.context_window,
                }
                if row.reasoning_efforts:
                    entry["reasoningEfforts"] = {
                        effort: effort for effort in row.reasoning_efforts
                    }
                declared_input = row.capabilities.get("input")
                if isinstance(declared_input, list):
                    modalities = [
                        str(value).strip()
                        for value in declared_input
                        if str(value).strip()
                    ]
                    if modalities:
                        entry["input"] = modalities
                route["models"].append(entry)
            providers[provider.provider] = route

        patch = [{"id": "llm-pi-ai", "config": {"providers": providers}}]
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(target, 0o600)
        return target

    def close(self) -> None:
        self._secure_files()
        self._db.close()
