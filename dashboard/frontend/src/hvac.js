/**
 * Shared helpers for presenting the mode-aware current HVAC API response.
 *
 * Revision history:
 *   2026-08-28  Added authoritative heat/cool/idle normalization and safe
 *               zone-card mapping so the frontend never infers heating from
 *               the legacy heating-call boolean.
 */

export const ZONE_ORDER = ["floor_3", "floor_2", "floor_1"];

export const ZONE_LABELS = {
  floor_1: "Floor 1",
  floor_2: "Floor 2",
  floor_3: "Floor 3",
};

const HVAC_ACTIONS = new Set(["heating", "cooling", "idle"]);

/**
 * Accept only the action values guaranteed by the API contract.
 * Missing, null, and unexpected values become unavailable instead of being
 * interpreted as an active mode.
 */
export function normalizeHvacAction(action) {
  const normalized = typeof action === "string" ? action.toLowerCase() : "";
  return HVAC_ACTIONS.has(normalized) ? normalized : null;
}

/**
 * Map one API snapshot to the shape consumed by TempCard and LiveSummary.
 * The legacy `${zone}_call` field is intentionally not consulted here.
 */
export function toZoneTelemetry(snapshot, zone, { stale = false } = {}) {
  if (snapshot == null) return null;

  const unavailable = stale || Boolean(snapshot.error);

  return {
    current_temp_f: snapshot[zone] ?? null,
    setpoint_f: snapshot[`${zone}_setpoint`] ?? null,
    hvac_action: unavailable ? null : normalizeHvacAction(snapshot[`${zone}_hvac_action`]),
    stale: unavailable,
  };
}
