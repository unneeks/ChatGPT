export default function StateBadge({ state }: { state: string }) {
  const cls =
    state === "review" ? "badge-review"
    : state === "checker_review" ? "badge-checker_review"
    : state === "complete" ? "badge bg-green-100 text-green-800"
    : state === "failed" || state === "quarantined" ? "badge bg-red-100 text-red-800"
    : "badge bg-gray-100 text-gray-700";
  return <span className={cls}>{state}</span>;
}
