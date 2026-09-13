"""Per-request runtime context.

Tenant and role scope travel in `contextvars`, not in tool arguments. That is a security decision,
not a style one: anything a model can write into a tool call is something a prompt injection can
control. A tool that accepts `tenant_id` as a parameter can be told to read another tenant's data;
a tool that reads it from an authenticated context cannot.

The fixture mechanism serves evaluation: with a fixtures file bound, tools return recorded output
instead of calling real dependencies, so the eval suite tests reasoning rather than infrastructure
uptime and costs nothing to run on every pull request.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_tenant: ContextVar[str] = ContextVar("tenant_id", default="default")
_roles: ContextVar[tuple[str, ...]] = ContextVar("roles", default=("admin",))
_components: ContextVar[tuple[str, ...]] = ContextVar("components", default=())
_fixtures: ContextVar[dict | None] = ContextVar("fixtures", default=None)


def current_tenant() -> str:
    return _tenant.get()


def current_roles() -> list[str]:
    return list(_roles.get())


def current_components() -> list[str]:
    return list(_components.get())


@contextmanager
def request_context(tenant_id: str, roles: list[str], components: list[str] | None = None) -> Generator[None]:
    """Binds the authenticated scope for the duration of a request."""
    tokens = [
        _tenant.set(tenant_id),
        _roles.set(tuple(roles)),
        _components.set(tuple(components or [])),
    ]
    try:
        yield
    finally:
        for var, token in zip((_tenant, _roles, _components), tokens):
            var.reset(token)


@contextmanager
def fixtures(path: str | Path) -> Generator[None]:
    """Binds recorded tool output for an eval run."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    token = _fixtures.set(data)
    try:
        yield
    finally:
        _fixtures.reset(token)


def fixture_lookup(tool_name: str) -> Any | None:
    """Returns recorded output for a tool, or None when running live."""
    data = _fixtures.get()
    return data.get(tool_name) if data else None
