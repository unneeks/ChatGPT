/**
 * Review Workspace — 3-pane layout.
 *
 * Left pane:   Draft artifact (epic summary + stories with ACs)
 * Middle pane: Cited source evidence (click a [Cite] tag in the draft to highlight span)
 * Right pane:  Judge scores + gate results
 * Bottom bar:  Decision controls (Approve / Edit / Reject / Escalate)
 * Top strip:   Live run timeline (SSE-driven audit events)
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, AuditEvent, BundleResponse, ResolvedCitation } from "../api/client";
import RiskBadge from "../components/RiskBadge";
import StateBadge from "../components/StateBadge";
import Spinner from "../components/Spinner";

// ---------------------------------------------------------------------------
// Run Timeline (SSE)
// ---------------------------------------------------------------------------

function Timeline({ runId }: { runId: string }) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const es = new EventSource(api.eventsUrl(runId));
    setConnected(true);

    es.onmessage = (e) => {
      const ev: AuditEvent = JSON.parse(e.data);
      setEvents((prev) => {
        if (prev.some((p) => p.id === ev.id)) return prev;
        return [...prev, ev];
      });
      setTimeout(() => ref.current?.scrollTo({ top: ref.current.scrollHeight, behavior: "smooth" }), 50);
    };

    es.addEventListener("done", () => {
      setConnected(false);
      es.close();
    });

    es.onerror = () => {
      setConnected(false);
    };

    return () => es.close();
  }, [runId]);

  const actionColor = (action: string) =>
    action.startsWith("decision") ? "text-indigo-700 font-semibold"
    : action.startsWith("run.") ? "text-gray-900 font-medium"
    : action === "judge.scored" ? "text-teal-700"
    : action.startsWith("draft") ? "text-sky-700"
    : "text-gray-600";

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between border-b border-gray-100 px-4 py-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          Run Timeline
        </span>
        <span className={`flex items-center gap-1 text-xs ${connected ? "text-green-600" : "text-gray-400"}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-green-500 animate-pulse" : "bg-gray-400"}`} />
          {connected ? "live" : "ended"}
        </span>
      </div>
      <div ref={ref} className="max-h-36 overflow-y-auto px-4 py-2 space-y-0.5 font-mono text-xs">
        {events.length === 0 ? (
          <span className="text-gray-400">No events yet…</span>
        ) : (
          events.map((ev) => (
            <div key={ev.id} className="flex gap-2">
              <span className="text-gray-400 shrink-0">
                {new Date(ev.at).toLocaleTimeString()}
              </span>
              <span className={actionColor(ev.action)}>{ev.action}</span>
              {ev.actor !== "system" && (
                <span className="text-gray-500 truncate">← {ev.actor}</span>
              )}
              {ev.model_id && (
                <span className="text-gray-400 truncate">
                  [{ev.model_id.slice(0, 16)}]
                </span>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Draft pane
// ---------------------------------------------------------------------------

interface Story {
  title?: string;
  story?: string;
  acceptance_criteria?: string[];
  citations?: Array<{ source_id: string; span_start: number; span_end: number }>;
  nfrs?: string[];
}

function CitationTag({
  idx,
  active,
  onClick,
}: {
  idx: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`mx-1 inline-flex items-center rounded px-1 py-0.5 text-xs font-mono transition-colors
        ${active ? "bg-amber-300 text-amber-900" : "bg-gray-100 text-gray-600 hover:bg-amber-100"}`}
    >
      [{idx + 1}]
    </button>
  );
}

function DraftPane({
  bundle,
  activeCitation,
  onCiteClick,
}: {
  bundle: BundleResponse;
  activeCitation: ResolvedCitation | null;
  onCiteClick: (c: ResolvedCitation) => void;
}) {
  const content = bundle.artifact.content as {
    epic_summary?: string;
    stories?: Story[];
    assumptions?: string[];
    open_questions?: string[];
  };

  // Build a map: source_id → citation object
  const citationBySourceId = Object.fromEntries(
    bundle.citations.map((c) => [c.source_document_id, c])
  );

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="text-xs font-semibold uppercase text-gray-500 mb-1">Epic Summary</h3>
        <p className="text-sm text-gray-800">{content.epic_summary || "—"}</p>
      </div>

      {(content.stories || []).map((story, si) => (
        <div key={si} className="card p-4 space-y-2">
          <h4 className="font-semibold text-sm text-gray-900">{story.title || `Story ${si + 1}`}</h4>
          <p className="text-sm text-gray-700 italic">{story.story}</p>

          {(story.acceptance_criteria || []).length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-500 mb-1">Acceptance Criteria</p>
              <ul className="space-y-1">
                {story.acceptance_criteria!.map((ac, ai) => (
                  <li key={ai} className="text-xs text-gray-700 flex gap-1">
                    <span className="text-green-500 shrink-0">✓</span>
                    {ac}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {(story.nfrs || []).length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-500 mb-1">NFRs</p>
              <ul className="list-disc list-inside space-y-0.5">
                {story.nfrs!.map((n, ni) => (
                  <li key={ni} className="text-xs text-gray-600">{n}</li>
                ))}
              </ul>
            </div>
          )}

          {(story.citations || []).length > 0 && (
            <div className="flex items-center gap-1 flex-wrap">
              <span className="text-xs text-gray-400">Sources:</span>
              {story.citations!.map((cref, ci) => {
                const resolved = citationBySourceId[cref.source_id];
                return (
                  <CitationTag
                    key={ci}
                    idx={ci}
                    active={activeCitation?.source_document_id === cref.source_id}
                    onClick={() => resolved && onCiteClick(resolved)}
                  />
                );
              })}
            </div>
          )}
        </div>
      ))}

      {(content.assumptions || []).length > 0 && (
        <div>
          <h3 className="text-xs font-semibold uppercase text-gray-500 mb-1">Assumptions</h3>
          <ul className="list-disc list-inside text-xs text-gray-600 space-y-0.5">
            {content.assumptions!.map((a, i) => <li key={i}>{a}</li>)}
          </ul>
        </div>
      )}

      {(content.open_questions || []).length > 0 && (
        <div>
          <h3 className="text-xs font-semibold uppercase text-gray-500 mb-1">Open Questions</h3>
          <ul className="list-disc list-inside text-xs text-amber-700 space-y-0.5">
            {content.open_questions!.map((q, i) => <li key={i}>{q}</li>)}
          </ul>
        </div>
      )}

      <div className="text-xs text-gray-400 border-t pt-2 flex gap-3">
        <span>v{bundle.artifact.version}</span>
        <span>prompt: {bundle.artifact.prompt_version}</span>
        <span>model: {bundle.artifact.model_id}</span>
        <span>hash: {bundle.artifact.content_hash.slice(0, 8)}</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Evidence pane
// ---------------------------------------------------------------------------

function EvidencePane({ citation }: { citation: ResolvedCitation | null }) {
  if (!citation) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-gray-400">
        Click a [Cite] tag in the draft to see the supporting evidence.
      </div>
    );
  }

  const text = citation.source_full_text ?? "";
  const start = citation.span_start;
  const end = citation.span_end || text.length;
  const before = text.slice(0, start);
  const span = text.slice(start, end);
  const after = text.slice(end);

  return (
    <div className="flex flex-col gap-3">
      <div>
        <span className="text-xs font-semibold uppercase text-gray-500">Source</span>
        <p className="text-xs text-gray-600 mt-0.5">
          {citation.source_origin} — {citation.source_external_ref}
        </p>
      </div>
      {citation.entailment_verdict && (
        <div className={`badge ${
          citation.entailment_verdict === "supported" ? "bg-green-100 text-green-800"
          : citation.entailment_verdict === "contradicted" ? "bg-red-100 text-red-800"
          : "bg-gray-100 text-gray-700"
        } w-fit`}>
          entailment: {citation.entailment_verdict}
        </div>
      )}
      <div className="rounded-md bg-gray-50 border border-gray-200 p-3 text-xs text-gray-700 whitespace-pre-wrap overflow-auto max-h-96 leading-relaxed">
        {before}
        <mark className="highlight-span">{span}</mark>
        {after}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Judge / Gates pane
// ---------------------------------------------------------------------------

function JudgePane({ bundle }: { bundle: BundleResponse }) {
  let judgeData: Record<string, unknown> | null = null;
  if (bundle.judge_scores) {
    try {
      judgeData = JSON.parse(bundle.judge_scores) as Record<string, unknown>;
    } catch {
      judgeData = null;
    }
  }

  const gatesByLayer = bundle.gates.reduce<Record<number, typeof bundle.gates>>((acc, g) => {
    (acc[g.layer] ??= []).push(g);
    return acc;
  }, {});

  const overall = judgeData?.overall as number | undefined;

  return (
    <div className="flex flex-col gap-4">
      {judgeData && (
        <div className="card p-4">
          <h3 className="text-xs font-semibold uppercase text-gray-500 mb-2">Judge Scores</h3>
          <div className="flex items-center gap-2 mb-3">
            <span className={`text-2xl font-bold ${
              (overall ?? 0) >= 7 ? "text-green-600" : (overall ?? 0) >= 5 ? "text-amber-600" : "text-red-600"
            }`}>
              {typeof overall === "number" ? overall.toFixed(1) : "—"}
            </span>
            <span className="text-xs text-gray-400">/ 10 overall</span>
          </div>
          {judgeData.scores && (
            <div className="space-y-1">
              {Object.entries(judgeData.scores as Record<string, number>).map(([k, v]) => (
                <div key={k} className="flex items-center gap-2">
                  <span className="text-xs text-gray-600 w-28 capitalize">{k.replace(/_/g, " ")}</span>
                  <div className="flex-1 bg-gray-100 rounded-full h-1.5">
                    <div
                      className={`h-1.5 rounded-full ${v >= 7 ? "bg-green-500" : v >= 5 ? "bg-amber-400" : "bg-red-500"}`}
                      style={{ width: `${(v / 10) * 100}%` }}
                    />
                  </div>
                  <span className="text-xs font-mono text-gray-700">{v}</span>
                </div>
              ))}
            </div>
          )}
          {judgeData.reasoning && (
            <p className="mt-2 text-xs text-gray-500 italic">{String(judgeData.reasoning)}</p>
          )}
          {Array.isArray(judgeData.blocking_issues) && judgeData.blocking_issues.length > 0 && (
            <div className="mt-2 rounded-md bg-red-50 border border-red-200 px-3 py-2">
              <p className="text-xs font-semibold text-red-700 mb-1">Blocking Issues</p>
              <ul className="list-disc list-inside text-xs text-red-600">
                {(judgeData.blocking_issues as string[]).map((issue, i) => (
                  <li key={i}>{issue}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {[1, 2, 3].map((layer) =>
        gatesByLayer[layer] ? (
          <div key={layer} className="card p-4">
            <h3 className="text-xs font-semibold uppercase text-gray-500 mb-2">
              Layer {layer} —{" "}
              {layer === 1 ? "Deterministic Gates" : layer === 2 ? "Heuristic Scoring" : "Grounding"}
            </h3>
            <div className="space-y-1">
              {gatesByLayer[layer].map((g, i) => (
                <div key={i} className="flex items-start gap-2">
                  <span className={`text-xs font-bold shrink-0 ${
                    g.verdict === "pass" ? "text-green-600" : "text-red-600"
                  }`}>
                    {g.verdict === "pass" ? "✓" : "✗"}
                  </span>
                  <span className="text-xs font-mono text-gray-700">{g.rule_id}</span>
                  {g.score != null && (
                    <span className="text-xs text-gray-500">{g.score.toFixed(1)}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        ) : null
      )}

      {bundle.approvals.length > 0 && (
        <div className="card p-4">
          <h3 className="text-xs font-semibold uppercase text-gray-500 mb-2">Approvals</h3>
          <div className="space-y-1.5">
            {bundle.approvals.map((a, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className={`badge ${
                  a.role === "maker" ? "bg-indigo-100 text-indigo-700"
                  : a.role === "checker" ? "bg-purple-100 text-purple-700"
                  : "bg-gray-100 text-gray-700"
                }`}>
                  {a.role}
                </span>
                <span className="text-xs text-gray-700">{a.reviewer_identity}</span>
                <span className={`text-xs font-medium ${
                  a.decision === "approve" ? "text-green-600"
                  : a.decision === "reject" ? "text-red-600"
                  : "text-amber-600"
                }`}>
                  {a.decision}
                </span>
                {a.has_diff && <span className="badge bg-amber-100 text-amber-700">edited</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="text-xs text-gray-400">
        {bundle.audit_event_count !== null && `${bundle.audit_event_count} audit events recorded`}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Decision Bar
// ---------------------------------------------------------------------------

const REVIEW_STATES = new Set(["review", "checker_review"]);

function DecisionBar({
  runId,
  state,
  identity,
  onDecision,
}: {
  runId: string;
  state: string;
  identity: string;
  onDecision: () => void;
}) {
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editDiff, setEditDiff] = useState<string>("");
  const [showEdit, setShowEdit] = useState(false);

  if (!REVIEW_STATES.has(state)) {
    return (
      <div className="border-t border-gray-200 bg-gray-50 px-6 py-3 text-sm text-gray-500">
        Run is in state <span className="font-mono">{state}</span> — no decision needed.
      </div>
    );
  }

  const decide = async (decision: string) => {
    setSubmitting(decision);
    setError(null);
    try {
      let diff: Record<string, unknown> | undefined;
      if (decision === "edit") {
        try {
          diff = JSON.parse(editDiff) as Record<string, unknown>;
        } catch {
          setError("Edit diff must be valid JSON");
          setSubmitting(null);
          return;
        }
      }
      await api.decide(runId, { decision, note, diff });
      onDecision();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(null);
    }
  };

  return (
    <div className="border-t border-gray-200 bg-white px-6 py-4 space-y-3">
      {error && (
        <div className="rounded-md bg-red-50 border border-red-200 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      {showEdit && (
        <div>
          <label className="text-xs font-medium text-gray-700 block mb-1">
            Edited content (JSON diff / replacement object)
          </label>
          <textarea
            rows={4}
            value={editDiff}
            onChange={(e) => setEditDiff(e.target.value)}
            className="w-full rounded-md border border-gray-300 px-3 py-2 text-xs font-mono focus:ring-2 focus:ring-brand-500 focus:border-transparent"
            placeholder='{"stories": [...]}'
          />
        </div>
      )}

      <div className="flex items-center gap-3 flex-wrap">
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Optional note…"
          className="flex-1 min-w-48 rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:ring-2 focus:ring-brand-500 focus:border-transparent"
        />

        <button
          className="btn-primary"
          disabled={submitting !== null}
          onClick={() => decide("approve")}
        >
          {submitting === "approve" ? "Approving…" : "✓ Approve"}
        </button>

        <button
          className="btn-ghost"
          disabled={submitting !== null}
          onClick={() => {
            setShowEdit((v) => !v);
          }}
        >
          ✎ Edit
        </button>

        {showEdit && (
          <button
            className="btn-ghost"
            disabled={submitting !== null || !editDiff}
            onClick={() => decide("edit")}
          >
            {submitting === "edit" ? "Saving…" : "Save Edit"}
          </button>
        )}

        <button
          className="btn-danger"
          disabled={submitting !== null}
          onClick={() => decide("reject")}
        >
          {submitting === "reject" ? "Rejecting…" : "✗ Reject"}
        </button>

        <button
          className="btn-ghost"
          disabled={submitting !== null}
          onClick={() => decide("escalate")}
        >
          {submitting === "escalate" ? "Escalating…" : "↑ Escalate"}
        </button>

        <span className="text-xs text-gray-400">as {identity}</span>
      </div>

      {state === "checker_review" && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
          ⚠ Two-person rule: you must be a different person from the maker to approve.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Workspace
// ---------------------------------------------------------------------------

export default function WorkspaceView() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const [bundle, setBundle] = useState<BundleResponse | null>(null);
  const [activeCitation, setActiveCitation] = useState<ResolvedCitation | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!runId) return;
    setLoading(true);
    api
      .bundle(runId)
      .then((b) => {
        setBundle(b);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [runId]);

  useEffect(() => {
    load();
  }, [load]);

  if (!runId) return null;
  if (error) {
    return (
      <div className="card px-6 py-8 text-center text-red-600">
        <p>{error}</p>
        <button onClick={() => navigate("/")} className="btn-ghost mt-4">← Back to Queue</button>
      </div>
    );
  }
  if (loading && !bundle) return <Spinner label="Loading workspace…" />;
  if (!bundle) return null;

  const identity = api.getToken() ? "(authenticated reviewer)" : "dev@localhost";

  return (
    <div className="flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3">
          <button onClick={() => navigate("/")} className="text-sm text-gray-500 hover:text-gray-700">
            ← Queue
          </button>
          <span className="font-mono font-bold text-lg text-brand-700">
            {bundle.run.jira_issue_key}
          </span>
          <StateBadge state={bundle.run.state} />
          <RiskBadge tier={bundle.run.risk_tier} />
        </div>
        <div className="flex gap-4 text-xs text-gray-400">
          <span>policy: {bundle.run.policy_version}</span>
          <span>prompt: {bundle.run.prompt_pack_version}</span>
          <a
            href={`/console/audit/${runId}`}
            className="text-brand-500 hover:underline"
          >
            Audit Replay →
          </a>
        </div>
      </div>

      {/* Live timeline */}
      <Timeline runId={runId} />

      {/* 3-pane workspace */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 min-h-[60vh]">
        {/* Draft */}
        <div className="card p-5 overflow-auto">
          <h2 className="text-sm font-semibold text-gray-700 mb-3 border-b pb-2">
            Draft Requirements
          </h2>
          <DraftPane
            bundle={bundle}
            activeCitation={activeCitation}
            onCiteClick={setActiveCitation}
          />
        </div>

        {/* Evidence */}
        <div className="card p-5 overflow-auto">
          <h2 className="text-sm font-semibold text-gray-700 mb-3 border-b pb-2">
            Source Evidence
            {activeCitation && (
              <button
                onClick={() => setActiveCitation(null)}
                className="ml-2 text-xs text-gray-400 hover:text-gray-600"
              >
                clear ×
              </button>
            )}
          </h2>
          <EvidencePane citation={activeCitation} />
        </div>

        {/* Judge / Gates */}
        <div className="card p-5 overflow-auto">
          <h2 className="text-sm font-semibold text-gray-700 mb-3 border-b pb-2">
            Verification
          </h2>
          <JudgePane bundle={bundle} />
        </div>
      </div>

      {/* Decision bar */}
      <DecisionBar
        runId={runId}
        state={bundle.run.state}
        identity={identity}
        onDecision={load}
      />
    </div>
  );
}
