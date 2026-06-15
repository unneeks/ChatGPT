"""Transcript ingestion pipeline (M9).

Flow:
  1. Parse WebVTT → list[TranscriptSegment]
  2. For each open question in the run, find matching segments (keyword overlap)
  3. Record answer on question; create citable SourceDocument (origin="transcript")
  4. Post [REQ-ANS:] Jira comment with timestamped evidence
  5. Emit audit events

Graph fetch path (T9.1): caller passes event_id; get_meeting_transcript() on the
GraphPort returns VTT or None (tenant policy may block — not a hard error).

Manual upload path (T9.2): caller passes raw VTT bytes from admin endpoint.

Both paths converge at `ingest_transcript()`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reqsmith import deps
from reqsmith.audit.ledger import emit_event
from reqsmith.persistence.idempotency import content_hash, insert_or_get
from reqsmith.persistence.models import Question, Run, SourceDocument


@dataclass
class TranscriptSegment:
    start: str
    end: str
    text: str


def parse_vtt(vtt_content: str) -> list[TranscriptSegment]:
    """Parse WebVTT text into timestamped segments.

    Handles optional cue identifiers and cue settings on the timestamp line.
    Returns segments in order; skips WEBVTT header, NOTE, and STYLE blocks.
    """
    segments: list[TranscriptSegment] = []
    blocks = re.split(r"\n[ \t]*\n", vtt_content.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        first = lines[0].strip()
        if first.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue

        # Locate timestamp line (may be preceded by an optional cue identifier)
        ts_idx = next(
            (i for i, ln in enumerate(lines) if " --> " in ln), None
        )
        if ts_idx is None:
            continue

        ts_parts = lines[ts_idx].split(" --> ", 1)
        start = ts_parts[0].strip()
        # cue settings may follow the end timestamp ("00:00:05.000 align:left")
        end = ts_parts[1].strip().split()[0]

        text = " ".join(ln.strip() for ln in lines[ts_idx + 1:] if ln.strip())
        if text:
            segments.append(TranscriptSegment(start=start, end=end, text=text))

    return segments


def _keywords(text: str) -> set[str]:
    """Significant words (4+ chars, alpha-only) lowercased."""
    return {w.lower() for w in re.findall(r"\b[a-zA-Z]{4,}\b", text)}


def find_answer_segments(
    question_text: str,
    segments: list[TranscriptSegment],
    *,
    threshold: int = 2,
) -> list[TranscriptSegment]:
    """Return segments sharing at least `threshold` keywords with the question."""
    q_kw = _keywords(question_text)
    if not q_kw:
        return []
    return [s for s in segments if len(q_kw & _keywords(s.text)) >= threshold]


async def ingest_transcript(
    run_id: str,
    vtt_content: str,
    session: AsyncSession,
    *,
    event_id: str | None = None,
) -> dict:
    """Process a meeting transcript for a run.

    Creates a SourceDocument for the full transcript, matches segments to open
    questions via keyword overlap, records answers with timestamp citations,
    and posts Jira comments.

    Returns a summary dict: {run_id, segments_parsed, questions_closed, results}.
    """
    run = await session.get(Run, run_id)
    if run is None:
        return {"error": "run not found", "run_id": run_id, "segments_parsed": 0, "questions_closed": 0, "results": []}

    segments = parse_vtt(vtt_content)
    if not segments:
        return {"error": "no transcript segments parsed", "run_id": run_id, "segments_parsed": 0, "questions_closed": 0, "results": []}

    # Idempotent: dedup transcript by content hash so re-ingesting the same VTT is a no-op
    ext_ref = event_id or f"manual:{run_id}"
    src_doc = SourceDocument(
        run_id=run_id,
        origin="transcript",
        external_ref=ext_ref,
        text=vtt_content,
        text_hash=content_hash(vtt_content),
    )
    src_doc, _ = await insert_or_get(
        session, src_doc, SourceDocument, SourceDocument.text_hash, src_doc.text_hash
    )

    open_questions = list(await session.scalars(
        select(Question).where(
            Question.run_id == run_id,
            Question.status.in_(["open", "asked"]),
        )
    ))

    jira = deps.get_jira()
    closed = 0
    results: list[dict] = []

    for question in open_questions:
        matched = find_answer_segments(question.text, segments)
        if not matched:
            results.append({"question_id": question.question_id, "action": "no_match"})
            continue

        answer_parts = [f"[{seg.start}] {seg.text}" for seg in matched]
        answer_text = "\n".join(answer_parts)

        question.answer_text = answer_text
        question.answer_source_document_id = src_doc.id
        question.status = "answered"
        await session.flush()

        body = (
            f"[REQ-ANS:{question.question_id}] Answer extracted from meeting transcript"
            f" (event: {event_id or 'manual'}):\n\n"
            + answer_text
        )
        await jira.add_comment(run.jira_issue_key, body)

        await emit_event(
            session, actor="transcript", action="question.answered_from_transcript",
            detail={
                "question_id": question.question_id,
                "run_id": run_id,
                "segments_matched": len(matched),
                "first_timestamp": matched[0].start,
                "event_id": event_id,
            },
        )
        closed += 1
        results.append({
            "question_id": question.question_id,
            "action": "answered",
            "segments_matched": len(matched),
            "first_timestamp": matched[0].start,
        })

    await emit_event(
        session, actor="transcript", action="transcript.ingested",
        detail={
            "run_id": run_id,
            "segments_parsed": len(segments),
            "questions_closed": closed,
            "event_id": event_id,
        },
    )

    return {
        "run_id": run_id,
        "segments_parsed": len(segments),
        "questions_closed": closed,
        "results": results,
    }


async def fetch_and_ingest_transcript(
    run_id: str,
    event_id: str,
    organizer: str,
    session: AsyncSession,
) -> dict:
    """Try to fetch the transcript from Graph and ingest it.

    Returns {"action": "ingested", ...} or {"action": "unavailable", "reason": ...}
    if Graph returns None (tenant policy, no transcript yet, etc.).
    """
    graph = deps.get_graph()
    vtt = await graph.get_meeting_transcript(event_id=event_id, organizer=organizer)
    if vtt is None:
        return {
            "action": "unavailable",
            "reason": "transcript not available from Graph (tenant policy or not yet processed)",
            "run_id": run_id,
            "event_id": event_id,
        }
    result = await ingest_transcript(run_id, vtt, session, event_id=event_id)
    return {"action": "ingested", **result}
