/**
 * Small pill badge showing HVAC action state.
 * heating → orange   idle → green   off / unknown → gray
 */
export function StatusBadge({ action }) {
  const normalized = (action ?? "").toLowerCase();

  const styles = {
    heating: "bg-orange-500/20 text-orange-300 border-orange-500/40",
    idle: "bg-green-500/20 text-green-300 border-green-500/40",
  };
  const dots = {
    heating: "bg-orange-400 animate-pulse",
    idle: "bg-green-400",
  };

  const style = styles[normalized] ?? "bg-slate-500/20 text-slate-400 border-slate-500/40";
  const dot = dots[normalized] ?? "bg-slate-500";
  const label = normalized.charAt(0).toUpperCase() + normalized.slice(1) || "Unknown";

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${style}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}
