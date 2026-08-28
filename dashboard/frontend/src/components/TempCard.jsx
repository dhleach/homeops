import { StatusBadge } from "./StatusBadge.jsx";
import { ZONE_LABELS, normalizeHvacAction } from "../hvac.js";

/**
 * Card showing live temperature data for a single thermostat zone.
 *
 * Revision history:
 *   2026-08-28  Render cooling directionally, surface unavailable/stale
 *               telemetry explicitly, and retain the existing heating copy.
 */
export function TempCard({ zone, data }) {
  const label = ZONE_LABELS[zone] ?? zone.replace(/_/g, " ");
  const temp = data?.current_temp_f;
  const setpoint = data?.setpoint_f;
  const action = data?.stale
    ? "unavailable"
    : normalizeHvacAction(data?.hvac_action) ?? "unavailable";
  const setpointLabel = action === "cooling" ? "Cooling to" : "Set to";

  return (
    <div className="flex flex-col gap-4 rounded-2xl border border-border bg-card p-6 transition-colors hover:bg-card-hover">
      {/* Zone label */}
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium uppercase tracking-widest text-slate-400">
          {label}
        </span>
        <StatusBadge action={action} />
      </div>

      {/* Current temp — big number */}
      <div className="flex items-end gap-2">
        {temp != null ? (
          <>
            <span className="text-6xl font-bold leading-none tracking-tight text-white">
              {temp}
            </span>
            <span className="mb-2 text-2xl font-light text-slate-400">°F</span>
          </>
        ) : (
          <span className="text-4xl font-bold text-slate-600">—</span>
        )}
      </div>

      {data?.stale && (
        <p className="text-xs text-amber-300" role="status">
          Last known reading — live state unavailable
        </p>
      )}

      {/* Setpoint */}
      {setpoint != null && (
        <div className="flex items-center gap-1.5 text-sm text-slate-500">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 20 20"
            fill="currentColor"
            className="h-4 w-4 text-blue-500"
          >
            <path
              fillRule="evenodd"
              d="M10 2a.75.75 0 0 1 .75.75v.258a33.186 33.186 0 0 1 6.668.83.75.75 0 0 1-.336 1.461 31.28 31.28 0 0 0-1.103-.232l1.702 7.545a.75.75 0 0 1-.387.832A4.981 4.981 0 0 1 15 14c-.825 0-1.606-.2-2.294-.556a.75.75 0 0 1-.387-.832l1.77-7.849a31.743 31.743 0 0 0-3.339-.254v11.505a20.01 20.01 0 0 1 3.78.501.75.75 0 1 1-.339 1.462A18.51 18.51 0 0 0 10 17.5c-1.442 0-2.845.165-4.191.477a.75.75 0 0 1-.339-1.462 20.01 20.01 0 0 1 3.78-.501V4.509a31.743 31.743 0 0 0-3.339.254l1.77 7.849a.75.75 0 0 1-.387.832A4.98 4.98 0 0 1 5 14a4.981 4.981 0 0 1-2.293-.556.75.75 0 0 1-.387-.832L4.022 5.067c-.37.07-.734.146-1.103.232a.75.75 0 0 1-.336-1.461 33.186 33.186 0 0 1 6.668-.83V2.75A.75.75 0 0 1 10 2Z"
              clipRule="evenodd"
            />
          </svg>
          {setpointLabel} {setpoint}°F
        </div>
      )}
    </div>
  );
}
