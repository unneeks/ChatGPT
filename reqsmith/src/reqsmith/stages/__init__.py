"""Pipeline stage handlers. Importing this package registers all stages with the
engine. Stage order: triage → retrieval → drafting → verification → (human review
via webhook/console) → publish."""

from reqsmith.stages import drafting, publish, retrieval, triage, verification  # noqa: F401
