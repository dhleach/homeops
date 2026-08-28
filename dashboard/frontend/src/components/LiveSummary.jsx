import { ZONE_LABELS, ZONE_ORDER, normalizeHvacAction } from "../hvac.js";
import { StatusBadge } from "./StatusBadge.jsx";

const SUMMARY_ORDER = ["cooling", "heating", "idle", "unavailable"];

/**
 * Compact mode-aware summary for the current dashboard snapshot.
 *
 * Revision history:
 *   2026-08-28  Added live heat/cool/idle/unavailable grouping so the landing
 *               page communicates mixed-zone state without inferring mode.
 */
export function LiveSummary({ zones, stale = false }) {
  const groups = Object.fromEntries(SUMMARY_ORDER.map((action) => [action, []]));

  for (const zone of ZONE_ORDER) {
    const action = stale ? null : normalizeHvacAction(zones?.[zone]?.hvac_action);
    groups[action ?? "unavailable"].push(ZONE_LABELS[zone] ?? zone);
  }

  const hasLiveAction = SUMMARY_ORDER.some(
    (action) => action !== "unavailable" && groups[action].length > 0,
  );
  const unavailableOnly = !hasLiveAction || stale;

  return (
    <section
      aria-label="Live HVAC summary"
      aria-live="polite"
      className="mb-6 rounded-2xl border border-border bg-card/60 px-5 py-4"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-widest text-slate-500">
            Live HVAC summary
          </p>
          <p className="mt-1 text-sm text-slate-300">
            {unavailableOnly
              ? "Live HVAC state is unavailable."
              : "Current thermostat-derived zone activity"}
          </p>
        </div>

        <div className="flex flex-wrap gap-2" data-testid="live-summary-groups">
          {SUMMARY_ORDER.map((action) =>
            groups[action].length > 0 ? (
              <span
                key={action}
                className="inline-flex items-center gap-2 rounded-full border border-border px-2.5 py-1 text-xs text-slate-300"
              >
                <StatusBadge action={action} />
                <span>{groups[action].join(", ")}</span>
              </span>
            ) : null,
          )}
        </div>
      </div>

      {!unavailableOnly && groups.cooling.length > 0 && (
        <p className="mt-3 text-xs text-slate-500">
          Cooling reflects inferred thermostat demand, not compressor feedback.
        </p>
      )}
    </section>
  );
}
