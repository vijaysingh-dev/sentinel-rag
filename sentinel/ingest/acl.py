"""Access control assignment at ingest time.

The ACL is declared in a YAML file that lives in git and is reviewed like code, not inferred by a
model and not guessed from folder names at query time. Two properties follow:

* **Deny by default.** A document matching no rule is admin-only. A new folder appearing in the
  source system cannot silently become readable by the support role.
* **Auditable.** "Why can support see this document?" is answered by pointing at a line in a
  reviewed file, which is the answer a compliance review actually wants.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sentinel.core.observability import log

DEFAULT_RULES: dict[str, Any] = {
    "defaults": {"allowed_roles": ["admin"], "components": []},
    "rules": [],
}


class AclResolver:
    def __init__(self, rules_path: str | Path | None = None):
        self.rules = DEFAULT_RULES
        if rules_path and Path(rules_path).exists():
            self.rules = yaml.safe_load(Path(rules_path).read_text()) or DEFAULT_RULES
            log.info("acl_rules_loaded", path=str(rules_path), count=len(self.rules.get("rules", [])))

    def resolve(
        self, source_uri: str, doc_type: str, meta: dict[str, Any] | None = None
    ) -> tuple[list[str], list[str]]:
        """Returns (allowed_roles, components) for a document.

        Rules are evaluated in order and later matches override earlier ones, so the file reads
        top-to-bottom from general to specific — the same convention as a firewall ruleset.
        """
        defaults = self.rules.get("defaults", DEFAULT_RULES["defaults"])
        roles = list(defaults.get("allowed_roles", ["admin"]))
        components = list(defaults.get("components", []))
        meta = meta or {}

        for rule in self.rules.get("rules", []):
            if not self._matches(rule.get("match", {}), source_uri, doc_type, meta):
                continue
            roles = list(rule.get("allowed_roles", roles))
            components = list(rule.get("components", components))

        return roles, components

    @staticmethod
    def _matches(match: dict[str, Any], source_uri: str, doc_type: str, meta: dict[str, Any]) -> bool:
        if "source_prefix" in match and not source_uri.startswith(match["source_prefix"]):
            return False
        if "source_contains" in match and match["source_contains"] not in source_uri:
            return False
        # `source_any` matches if any listed substring appears, which lets a rule cover a component
        # named several ways across the corpus (a directory, a filename prefix, a product alias)
        # without duplicating the rule three times.
        if "source_any" in match and not any(token in source_uri for token in match["source_any"]):
            return False
        if "doc_type" in match and match["doc_type"] != doc_type:
            return False
        return all(meta.get(key) == expected for key, expected in (match.get("meta") or {}).items())
