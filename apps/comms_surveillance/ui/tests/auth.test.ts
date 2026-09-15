import { afterEach, describe, expect, it, vi } from "vitest";
import { devBypassExpected, unauthenticatedHint } from "../src/lib/auth";

afterEach(() => vi.unstubAllEnvs());

describe("dev bypass", () => {
  it("is off unless the variable is exactly true", () => {
    expect(devBypassExpected()).toBe(false);
    vi.stubEnv("VITE_AUTH_DEV_BYPASS", "1");
    expect(devBypassExpected()).toBe(false);
    vi.stubEnv("VITE_AUTH_DEV_BYPASS", "true");
    expect(devBypassExpected()).toBe(true);
  });

  it("explains a 401 differently in a local build than in a deployed one", () => {
    expect(unauthenticatedHint()).toMatch(/identity provider/);
    vi.stubEnv("VITE_AUTH_DEV_BYPASS", "true");
    expect(unauthenticatedHint()).toMatch(/AUTH__DEV_BYPASS=true/);
  });
});
