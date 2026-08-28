/**
 * Small pill badge showing HVAC action state.
 * heating → orange   cooling → blue   idle → green   unavailable → gray
 *
 * Revision history:
 *   2026-08-28  Added a distinct cooling badge and an explicit unavailable
 *               state so null API telemetry cannot look like idle or heating.
 */
export function StatusBadge({ action }) {
  const normalized = typeof action === "string" ? action.toLowerCase() : "";

  const styles = {
    heating: "bg-orange-500/20 text-orange-300 border-orange-500/40",
    cooling: "bg-sky-500/20 text-sky-300 border-sky-500/40",
    idle: "bg-green-500/20 text-green-300 border-green-500/40",
    unavailable: "bg-slate-500/20 text-slate-400 border-slate-500/40",
  };
  const dots = {
    heating: "bg-orange-400 animate-pulse",
    cooling: "bg-sky-400 animate-pulse",
    idle: "bg-green-400",
    unavailable: "bg-slate-400",
  };

  const style = styles[normalized] ?? "bg-slate-500/20 text-slate-400 border-slate-500/40";
  const dot = dots[normalized] ?? "bg-slate-500";
  const label = normalized ? normalized.charAt(0).toUpperCase() + normalized.slice(1) : "Unknown";

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${style}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}
