import { useEffect, useState } from "react";
import {
  api,
  OutreachMonitorResponse,
  StakeholderBudget,
} from "../api/client";

const RUNG_LABELS: Record<number, string> = {
  1: "Jira Comment",
  2: "Teams Card",
  3: "Meeting Invite",
};

const CHANNEL_ICONS: Record<string, string> = {
  jira_comment: "📝",
  teams_card: "💬",
  meeting_invite: "📅",
};

function BudgetMeter({
  label,
  value,
  limit,
}: {
  label: string;
  value: number;
  limit: number;
}) {
  const pct = Math.min((value / limit) * 100, 100);
  const color = pct >= 100 ? "bg-red-500" : pct >= 75 ? "bg-amber-400" : "bg-emerald-500";
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-gray-600">
        <span>{label}</span>
        <span className={value >= limit ? "text-red-600 font-semibold" : ""}>
          {value} / {limit}
        </span>
      </div>
      <div className="h-2 w-full rounded bg-gray-100">
        <div className={`h-2 rounded ${color} transition-all`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function RungDots({ rung, max = 3 }: { rung: number; max?: number }) {
  return (
    <span className="flex gap-1 items-center">
      {Array.from({ length: max }, (_, i) => (
        <span
          key={i}
          className={`inline-block w-2.5 h-2.5 rounded-full ${
            i < rung ? "bg-brand-500" : "bg-gray-200"
          }`}
          title={RUNG_LABELS[i + 1]}
        />
      ))}
    </span>
  );
}

export default function OutreachView() {
  const [data, setData] = useState<OutreachMonitorResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);

  const load = () =>
    api.outreach().then(setData).catch((e) => setError(String(e)));

  useEffect(() => {
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, []);

  const toggleKillSwitch = async () => {
    if (!data) return;
    setToggling(true);
    try {
      const next = !data.kill_switch.paused;
      const reason = next ? "paused from console" : "resumed from console";
      const res = await fetch(`/outreach/pause?paused=${next}&reason=${encodeURIComponent(reason)}`, {
        method: "POST",
      });
      if (res.ok) await load();
    } finally {
      setToggling(false);
    }
  };

  if (error) return <p className="text-red-600 p-4">{error}</p>;
  if (!data) return <p className="text-gray-500 p-4">Loading outreach monitor…</p>;

  const { kill_switch, questions, recent_events, budget } = data;
  const openCount = questions.filter((q) => ["open", "asked"].includes(q.status)).length;
  const overdueCount = questions.filter((q) => q.sla_overdue && ["open", "asked"].includes(q.status)).length;

  return (
    <div className="space-y-6">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Outreach Monitor</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {openCount} open question{openCount !== 1 ? "s" : ""}
            {overdueCount > 0 && (
              <span className="ml-2 text-red-600 font-medium">
                · {overdueCount} SLA overdue
              </span>
            )}
          </p>
        </div>

        {/* Kill switch */}
        <div className="flex items-center gap-3 rounded-lg border px-4 py-2.5 bg-white shadow-sm">
          <div>
            <p className="text-sm font-medium text-gray-700">Global outreach</p>
            {kill_switch.reason && kill_switch.paused && (
              <p className="text-xs text-gray-500">{kill_switch.reason}</p>
            )}
          </div>
          <button
            onClick={toggleKillSwitch}
            disabled={toggling}
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none ${
              kill_switch.paused ? "bg-red-500" : "bg-emerald-500"
            } ${toggling ? "opacity-50" : ""}`}
            title={kill_switch.paused ? "Click to resume outreach" : "Click to pause outreach"}
          >
            <span
              className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                kill_switch.paused ? "translate-x-1" : "translate-x-6"
              }`}
            />
          </button>
          <span
            className={`text-sm font-medium ${kill_switch.paused ? "text-red-600" : "text-emerald-600"}`}
          >
            {kill_switch.paused ? "PAUSED" : "ACTIVE"}
          </span>
        </div>
      </div>

      {/* Budget panel */}
      <div className="card">
        <h2 className="text-sm font-semibold text-gray-700 mb-3">Rate Budget</h2>
        <div className="mb-3">
          <BudgetMeter
            label="Global sends today"
            value={budget.global_sent_today}
            limit={budget.global_limit}
          />
        </div>
        {budget.stakeholders.length === 0 ? (
          <p className="text-xs text-gray-400">No stakeholder activity recorded.</p>
        ) : (
          <div className="space-y-4">
            {budget.stakeholders.map((s: StakeholderBudget) => (
              <div key={s.aad_id} className="bg-gray-50 rounded p-3 space-y-2">
                <p className="text-xs font-medium text-gray-700 truncate">{s.aad_id}</p>
                <BudgetMeter
                  label="Teams cards today"
                  value={s.chats_today}
                  limit={s.chats_limit}
                />
                <BudgetMeter
                  label="Meeting invites this week"
                  value={s.meetings_this_week}
                  limit={s.meetings_limit}
                />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Question ladder */}
      <div className="card">
        <h2 className="text-sm font-semibold text-gray-700 mb-3">Question Ladder</h2>
        {questions.length === 0 ? (
          <p className="text-xs text-gray-400">No questions found.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="border-b text-xs text-gray-500 uppercase tracking-wide">
                  <th className="text-left py-2 pr-4">Question ID</th>
                  <th className="text-left py-2 pr-4">Issue</th>
                  <th className="text-left py-2 pr-4">Status</th>
                  <th className="text-left py-2 pr-4">Rung</th>
                  <th className="text-left py-2 pr-4">SLA</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {questions.map((q) => (
                  <tr
                    key={q.question_id}
                    className={q.sla_overdue && ["open", "asked"].includes(q.status) ? "bg-red-50" : ""}
                  >
                    <td className="py-2 pr-4 font-mono text-xs text-gray-700 max-w-[140px] truncate">
                      {q.question_id}
                    </td>
                    <td className="py-2 pr-4 text-xs text-gray-600">
                      {q.issue_key ?? "—"}
                    </td>
                    <td className="py-2 pr-4">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                          q.status === "answered"
                            ? "bg-emerald-100 text-emerald-700"
                            : q.status === "escalated"
                            ? "bg-red-100 text-red-700"
                            : q.status === "handed_off"
                            ? "bg-purple-100 text-purple-700"
                            : "bg-gray-100 text-gray-600"
                        }`}
                      >
                        {q.status}
                      </span>
                    </td>
                    <td className="py-2 pr-4">
                      <RungDots rung={q.current_rung} />
                    </td>
                    <td className="py-2 pr-4 text-xs">
                      {q.sla_deadline ? (
                        <span className={q.sla_overdue ? "text-red-600 font-medium" : "text-gray-500"}>
                          {new Date(q.sla_deadline).toLocaleString()}
                          {q.sla_overdue ? " ⚠" : ""}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Recent events feed */}
      <div className="card">
        <h2 className="text-sm font-semibold text-gray-700 mb-3">Recent Events</h2>
        {recent_events.length === 0 ? (
          <p className="text-xs text-gray-400">No outreach events yet.</p>
        ) : (
          <ul className="space-y-1.5 text-xs text-gray-600">
            {recent_events.slice(0, 30).map((evt, i) => (
              <li key={i} className="flex items-center gap-2">
                <span className="text-base leading-none">
                  {CHANNEL_ICONS[evt.channel] ?? "📨"}
                </span>
                <span className="font-mono text-gray-500 shrink-0">
                  {new Date(evt.created_at).toLocaleTimeString()}
                </span>
                <span
                  className={`px-1 py-0.5 rounded text-xs font-medium shrink-0 ${
                    evt.direction === "out"
                      ? "bg-blue-100 text-blue-700"
                      : "bg-green-100 text-green-700"
                  }`}
                >
                  {evt.direction}
                </span>
                <span className="font-medium shrink-0">{evt.channel}</span>
                <span className="text-gray-400 font-mono truncate max-w-[180px]">
                  {evt.question_id}
                </span>
                {evt.external_message_id && (
                  <span className="text-gray-300 font-mono truncate">
                    → {evt.external_message_id}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
