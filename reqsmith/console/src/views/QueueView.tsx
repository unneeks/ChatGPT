import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, QueueItem } from "../api/client";
import RiskBadge from "../components/RiskBadge";
import Spinner from "../components/Spinner";

function slaClass(updatedAt: string): string {
  const ageHours = (Date.now() - new Date(updatedAt).getTime()) / 3_600_000;
  if (ageHours > 24) return "text-red-600 font-semibold";
  if (ageHours > 8) return "text-amber-600";
  return "text-gray-500";
}

function elapsed(updatedAt: string): string {
  const mins = Math.round((Date.now() - new Date(updatedAt).getTime()) / 60_000);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.round(hrs / 24)}d`;
}

export default function QueueView() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const load = () => {
    setLoading(true);
    api
      .queue()
      .then((r) => {
        setItems(r.queue);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Review Queue</h1>
        <button onClick={load} className="btn-ghost text-xs">
          ↻ Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700 mb-4">
          {error}
        </div>
      )}

      {loading && items.length === 0 ? (
        <Spinner />
      ) : items.length === 0 ? (
        <div className="card px-6 py-12 text-center text-gray-500">
          <p className="text-2xl">✓</p>
          <p className="mt-1 text-sm">Queue is empty — nothing awaiting review.</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">
              <tr>
                <th className="px-4 py-3">Issue</th>
                <th className="px-4 py-3">Risk</th>
                <th className="px-4 py-3">State</th>
                <th className="px-4 py-3">Waiting for</th>
                <th className="px-4 py-3">Draft</th>
                <th className="px-4 py-3">Waiting</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {items.map((item) => (
                <tr
                  key={item.run_id}
                  className="hover:bg-indigo-50 cursor-pointer transition-colors"
                  onClick={() => navigate(`/runs/${item.run_id}`)}
                >
                  <td className="px-4 py-3 font-mono font-medium text-brand-700">
                    {item.jira_issue_key}
                  </td>
                  <td className="px-4 py-3">
                    <RiskBadge tier={item.risk_tier} />
                  </td>
                  <td className="px-4 py-3">
                    <span className={`badge ${item.state === "checker_review" ? "badge-checker_review" : "badge-review"}`}>
                      {item.state}
                    </span>
                  </td>
                  <td className="px-4 py-3 capitalize text-gray-600">
                    {item.waiting_for}
                  </td>
                  <td className="px-4 py-3 text-gray-500">
                    {item.draft_version ? `v${item.draft_version}` : "—"}
                    {item.draft_model_id && (
                      <span className="ml-1 text-xs text-gray-400">
                        ({item.draft_model_id.replace("claude-", "").slice(0, 12)})
                      </span>
                    )}
                  </td>
                  <td className={`px-4 py-3 tabular-nums ${slaClass(item.updated_at)}`}>
                    {elapsed(item.updated_at)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      className="btn-primary text-xs"
                      onClick={(e) => {
                        e.stopPropagation();
                        navigate(`/runs/${item.run_id}`);
                      }}
                    >
                      Review →
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
