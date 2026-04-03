/**
 * Shown when the API call fails — non-intrusive banner above the cards.
 */
export function ErrorBanner({ message }) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
      <span className="text-base">⚠️</span>
      <span>
        Could not reach the HomeOps API — showing last known data.{" "}
        <span className="text-red-400/70">({message})</span>
      </span>
    </div>
  );
}
