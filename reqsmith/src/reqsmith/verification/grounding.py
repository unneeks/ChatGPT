"""Layer-3 grounding: every story must cite a resolvable source span. Orphan claims
hard-block and reopen as questions — the system never invents requirements."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reqsmith.persistence.models import Artifact, Citation, SourceDocument


@dataclass
class GroundingReport:
    total_claims: int
    grounded: int
    orphans: list[str]  # claim paths with no resolvable citation

    @property
    def passed(self) -> bool:
        return not self.orphans and self.total_claims > 0


async def check_grounding(session: AsyncSession, artifact: Artifact) -> GroundingReport:
    stories = artifact.content.get("stories", [])
    citations = list(
        await session.scalars(select(Citation).where(Citation.artifact_id == artifact.id))
    )
    cited_paths: set[str] = set()
    for citation in citations:
        source = await session.get(SourceDocument, citation.source_document_id)
        if source is None:
            continue
        span_ok = (
            0 <= citation.span_start <= citation.span_end
            and citation.span_start < max(len(source.text), 1)
        )
        if span_ok:
            citation.entailment_verdict = "resolved"
            cited_paths.add(citation.claim_path)

    orphans = [f"/stories/{i}" for i in range(len(stories)) if f"/stories/{i}" not in cited_paths]
    return GroundingReport(total_claims=len(stories), grounded=len(cited_paths), orphans=orphans)
