/**
 * Audit Replay — regulator view.
 * Shows every event that produced a run's artifacts, in order,
 * with the full (actor, action, prompt_version, model_id, policy_version, hashes) triple.
 */

import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, AuditResponse } from "../api/client";
import Spinner from "../components/Spinner";

const ACTION_COLORS: Record<string, string> = {
  "run.created": "bg-blue-50 text-blue-700",
  "run.transition": "bg-indigo-50 text-indigo-700",
  "draft.created": "bg-sky-50 text-sky-700",
  "judge.scored": "bg-teal-50 text-teal-700",
  "stories.published": "bg-green-50 text-green-700",
  "decision.approved": "bg-green-50 text-green-800 font-semibold",
  "decision.rejected": "bg-red-50 text-red-700",
  "decision.escalated": "bg-orange-50 text-orange-700",
  "approval.rejected_same_identity": "bg-red-50 text-red-800 font-semibold",
};

function actionClass(action: string): string {
  return ACTION_COLORS[action] ?? "bg-gray-50 text-gray-700";
}

export default function AuditView() {
  const { runId: paramRunId } = useParams<{ runId?: string }>();
  const [inputRunId, setInputRunId] = useState(paramRunId ?? "");
  const [data, setData] = useState<AuditResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const navigate = useNavigate();

  const load = (id: string) => {
    if (!id.trim()) return;
    setLoading(true);
    setError(null);
    api
      .auditReplay(id.trim())
      .then(setData)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (paramRunId) load(paramRunId);
  }, [paramRunId]);

  const toggle = (seq: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(seq) ? next.delete(seq) : next.add(seq);
      return next;
    });

  return (
    <div className="max-w-4xl mx-auto">
      <div className="mb-6">
        <h1 className="text-xl font-semibold mb-3">Audit Replay</h1>
        <div className="flex gap-2">
          <input
            value={inputRunId}
            onChange={(e) => setInputRunId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && load(inputRunId)}
            placeholder="Enter run ID…"
            className="flex-1 rounded-md border border-gray-300 px-3 py-2 text-sm focus:ring-2 focus:ring-brand-500"
          />
          <button
            className="btn-primary"
            onClick={() => {
              navigate(`/audit/${inputRunId.trim()}`);
              load(inputRunId);
            }}
          >
            Load
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700 mb-4">
          {error}
        </div>
      )}

      {loading && <Spinner label="Loading audit trail…" />}

      {data && !loading && (
        <>
          <div className="flex items-center gap-4 mb-4">
            <span className="font-mono font-bold text-lg text-brand-700">
              {data.jira_issue_key}
            </span>
            <span className="badge bg-gray-100 text-gray-700">{data.state}</span>
            <span className="text-xs text-gray-400">{data.events.length} events</span>
          </div>

          <div className="card overflow-hidden">
            <table className="w-full text-xs">
              <thead className="bg-gray-50 text-left font-medium text-gray-500 uppercase tracking-wide">
                <tr>
                  <th className="px-3 py-2">#</th>
                  <th className="px-3 py-2">Time</th>
                  <th className="px-3 py-2">Actor</th>
                  <th className="px-3 py-2">Action</th>
                  <th className="px-3 py-2">Model / Prompt / Policy</th>
                  <th className="px-3 py-2">Hashes</th>
                  <th className="px-3 py-2"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {data.events.map((ev) => (
                  <>
                    <tr
                      key={ev.seq}
                      className="hover:bg-gray-50 cursor-pointer"
                      onClick={() => ev.detail && toggle(ev.seq)}
                    >
                      <td className="px-3 py-2 font-mono text-gray-400">{ev.seq}</td>
                      <td className="px-3 py-2 text-gray-500 whitespace-nowrap">
                        {new Date(ev.at).toLocaleTimeString()}
                      </td>
                      <td className="px-3 py-2 font-medium text-gray-700 max-w-24 truncate">
                        {ev.actor}
                      </td>
                      <td className="px-3 py-2">
                        <span className={`badge ${actionClass(ev.action)}`}>
                          {ev.action}
                        </span>
                      </td>
                      <td className="px-3 py-2 font-mono text-gray-500 space-x-2">
                        {ev.model_id && <span>{ev.model_id.slice(0, 18)}</span>}
                        {ev.prompt_version && <span className="text-indigo-500">{ev.prompt_version}</span>}
                        {ev.policy_version && <span className="text-teal-500">{ev.policy_version}</span>}
                      </td>
                      <td className="px-3 py-2 font-mono text-gray-400">
                        {ev.input_hash?.slice(0, 8) ?? ""}
                        {ev.output_hash ? ` → ${ev.output_hash.slice(0, 8)}` : ""}
                      </td>
                      <td className="px-3 py-2 text-gray-400">
                        {ev.detail ? (expanded.has(ev.seq) ? "▲" : "▼") : ""}
                      </td>
                    </tr>
                    {expanded.has(ev.seq) && ev.detail && (
                      <tr key={`${ev.seq}-detail`}>
                        <td colSpan={7} className="px-3 py-2 bg-gray-50">
                          <pre className="text-xs font-mono text-gray-600 overflow-auto max-h-40">
                            {JSON.stringify(ev.detail, null, 2)}
                          </pre>
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
