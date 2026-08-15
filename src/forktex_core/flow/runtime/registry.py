# Copyright (C) 2026 FORKTEX S.R.L.
#
# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-ForkTex-Commercial
#
# This file is part of forktex-core.
#
# For commercial licensing -- including use in proprietary products, SaaS
# deployments, or any context where AGPL obligations cannot be met -- you
# MUST obtain a commercial license from FORKTEX S.R.L. (info@forktex.com).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""In-process registry of WorkflowDefinitions and StepTemplates."""

from __future__ import annotations

from pydantic import Field

from forktex_core.flow.domain.definition import StepTemplateDef, WorkflowDefinition
from forktex_core.types import BaseValueObject


class _FlowRegistry(BaseValueObject):
    """Holds all registered workflow definitions and step templates.

    Keyed by (name, version, namespace) for workflow definitions.
    namespace=None for platform-track definitions.
    """

    # (name, version, namespace) → WorkflowDefinition
    definitions: dict[tuple[str, int, str | None], WorkflowDefinition] = Field(default_factory=dict)

    # template_name → StepTemplateDef
    step_templates: dict[str, StepTemplateDef] = Field(default_factory=dict)

    def register_definition(self, defn: WorkflowDefinition) -> None:
        key = (defn.name, defn.version, defn.namespace)
        if key in self.definitions:
            ns_str = f" in namespace {defn.namespace!r}" if defn.namespace else ""
            raise ValueError(f"workflow ({defn.name}, version={defn.version}){ns_str} already registered")
        self.definitions[key] = defn

    def register_step_template(self, template: StepTemplateDef) -> None:
        if template.name in self.step_templates:
            raise ValueError(f"step template {template.name!r} already registered")
        self.step_templates[template.name] = template

    def get_definition(
        self,
        name: str,
        *,
        version: int | None = None,
        namespace: str | None = None,
    ) -> WorkflowDefinition | None:
        """Look up a definition. If version=None, returns the highest registered version."""
        if version is not None:
            return self.definitions.get((name, version, namespace))
        matching = [defn for (n, v, ns), defn in self.definitions.items() if n == name and ns == namespace]
        if not matching:
            return None
        return max(matching, key=lambda d: d.version)

    def all_definitions_for_namespace(self, namespace: str | None) -> list[WorkflowDefinition]:
        return [d for (_, _, ns), d in self.definitions.items() if ns == namespace]

    def scheduled_definitions(self) -> list[WorkflowDefinition]:
        """All definitions with a cron schedule."""
        return [d for d in self.definitions.values() if d.schedule is not None]

    def latest_version(self, name: str, namespace: str | None = None) -> int | None:
        versions = [v for (n, v, ns) in self.definitions if n == name and ns == namespace]
        return max(versions) if versions else None
