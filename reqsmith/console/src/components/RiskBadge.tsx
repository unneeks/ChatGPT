export default function RiskBadge({ tier }: { tier: string }) {
  const cls =
    tier === "HIGH" ? "badge-high"
    : tier === "LOW" ? "badge-low"
    : "badge-medium";
  return <span className={cls}>{tier}</span>;
}
