/**
 * Revision history:
 *   2026-08-21  Added bearer-header and expired-session regression coverage for
 *               the authenticated diagnostic request hook.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAsk } from "../useAsk.js";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useAsk", () => {
  it("sends the OIDC bearer token with the diagnostic request", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ answer: "Looks healthy." }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useAsk("https://api.homeops.now", "access-token"));

    await act(async () => {
      await result.current.ask("Is the HVAC okay?");
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.homeops.now/api/diagnostic",
      expect.objectContaining({
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer access-token",
        },
      }),
    );
    expect(result.current.answer).toBe("Looks healthy.");
  });

  it("clears the session and reports a stable message after a 401", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 401 });
    const onUnauthorized = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useAsk("https://api.homeops.now", "expired-token", onUnauthorized));

    await act(async () => {
      await result.current.ask("Is the HVAC okay?");
    });

    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(result.current.error).toBe("Your sign-in expired. Sign in again to continue.");
  });
});
