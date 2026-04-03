/**
 * "● Live" badge + last-refreshed timestamp shown in the footer of the data section.
 */
export function LiveIndicator({ lastUpdated, onRefresh }) {
  const timeStr = lastUpdated
    ? lastUpdated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : null;

  return (
    <div className="flex items-center justify-between text-sm text-slate-500">
      <div className="flex items-center gap-2">
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-green-500" />
        </span>
        <span>Live · refreshes every 30s</span>
        {timeStr && <span className="text-slate-600">· last at {timeStr}</span>}
      </div>
      <button
        onClick={onRefresh}
        className="rounded-lg px-3 py-1 text-xs text-slate-500 transition-colors hover:bg-slate-700 hover:text-slate-300"
      >
        Refresh now
      </button>
    </div>
  );
}
