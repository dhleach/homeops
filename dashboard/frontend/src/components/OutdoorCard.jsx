/**
 * Card showing the outdoor temperature from the weather sensor.
 */
export function OutdoorCard({ temp, lastUpdated }) {
  return (
    <div className="flex flex-col gap-4 rounded-2xl border border-blue-500/30 bg-blue-500/5 p-6 transition-colors hover:bg-blue-500/10">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium uppercase tracking-widest text-blue-400">
          Outdoor
        </span>
        <span className="text-xl">🌡️</span>
      </div>

      <div className="flex items-end gap-2">
        {temp != null ? (
          <>
            <span className="text-6xl font-bold leading-none tracking-tight text-white">
              {temp}
            </span>
            <span className="mb-2 text-2xl font-light text-blue-300">°F</span>
          </>
        ) : (
          <span className="text-4xl font-bold text-slate-600">—</span>
        )}
      </div>

      {lastUpdated && (
        <p className="text-xs text-slate-500">
          Sensor updated {formatRelative(lastUpdated)}
        </p>
      )}
    </div>
  );
}

function formatRelative(isoString) {
  if (!isoString) return "";
  const diffMs = Date.now() - new Date(isoString).getTime();
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 2) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  return `${Math.floor(diffMin / 60)}h ago`;
}
