import { normalizeHvacAction, toZoneTelemetry } from "../hvac.js";

describe("mode-aware HVAC mapping", () => {
  it("uses the authoritative action instead of the legacy heating-call field", () => {
    const zone = toZoneTelemetry(
      {
        floor_1: 74,
        floor_1_call: true,
        floor_1_hvac_action: "cooling",
        floor_1_setpoint: 72,
      },
      "floor_1",
    );

    expect(zone).toMatchObject({
      current_temp_f: 74,
      setpoint_f: 72,
      hvac_action: "cooling",
      stale: false,
    });
  });

  it("turns missing and invalid actions into unavailable telemetry", () => {
    expect(normalizeHvacAction(undefined)).toBeNull();
    expect(normalizeHvacAction("compressor-running")).toBeNull();
    expect(toZoneTelemetry({ floor_1_call: true }, "floor_1").hvac_action).toBeNull();
  });

  it("suppresses an active action for stale snapshots", () => {
    const zone = toZoneTelemetry(
      { floor_1_hvac_action: "cooling", floor_1_setpoint: 72 },
      "floor_1",
      { stale: true },
    );

    expect(zone.hvac_action).toBeNull();
    expect(zone.stale).toBe(true);
  });

  it("suppresses an active action when the API marks the snapshot unavailable", () => {
    const zone = toZoneTelemetry(
      { error: "Prometheus unreachable", floor_1_hvac_action: "heating" },
      "floor_1",
    );

    expect(zone.hvac_action).toBeNull();
    expect(zone.stale).toBe(true);
  });

  it("normalizes valid actions case-insensitively", () => {
    expect(normalizeHvacAction("HEATING")).toBe("heating");
    expect(normalizeHvacAction("Cooling")).toBe("cooling");
    expect(normalizeHvacAction("idle")).toBe("idle");
  });
});
