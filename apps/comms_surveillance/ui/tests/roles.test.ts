import { describe, expect, it } from "vitest";
import {
  allowedViews,
  capabilities,
  defaultView,
  parseRoute,
  routeHref,
} from "../src/lib/roles";
import type { Role } from "../src/lib/types";

describe("capabilities", () => {
  it("gives a reviewer the queue and nothing analytical", () => {
    expect(capabilities(["compliance_reviewer"])).toEqual({
      queue: true,
      qaSample: false,
      metrics: false,
    });
  });

  it("makes the lead a superset of the reviewer, plus the QA sample and metrics", () => {
    expect(capabilities(["compliance_lead"])).toEqual({
      queue: true,
      qaSample: true,
      metrics: true,
    });
  });

  it("gives governance metrics only — never the queue or the QA sample", () => {
    const caps = capabilities(["governance"]);
    expect(caps).toEqual({ queue: false, qaSample: false, metrics: true });
    // The plain-English form of the rule this UI is built around: a governance
    // session has no code path that asks for a transcript or an audio grant.
    expect(allowedViews(caps)).toEqual(["metrics"]);
  });

  it("gives an account with no role nothing to ask for", () => {
    expect(allowedViews(capabilities([]))).toEqual([]);
    expect(defaultView(capabilities([]))).toBeNull();
  });

  it("unions the roles when a person holds more than one", () => {
    const roles: Role[] = ["governance", "compliance_reviewer"];
    expect(capabilities(roles)).toEqual({ queue: true, qaSample: false, metrics: true });
  });

  it("ignores anything that is not one of the three contract roles", () => {
    expect(capabilities(["operator" as Role])).toEqual({
      queue: false,
      qaSample: false,
      metrics: false,
    });
  });
});

describe("landing view", () => {
  it("sends a reviewer to the queue", () =>
    expect(defaultView(capabilities(["compliance_reviewer"]))).toBe("queue"));
  it("sends a lead to the queue", () =>
    expect(defaultView(capabilities(["compliance_lead"]))).toBe("queue"));
  it("sends governance to metrics", () =>
    expect(defaultView(capabilities(["governance"]))).toBe("metrics"));
});

describe("routing", () => {
  it("defaults to the queue", () => {
    expect(parseRoute("")).toEqual({ view: "queue", flagId: null });
    expect(parseRoute("#/")).toEqual({ view: "queue", flagId: null });
  });

  it("reads a deep link to one flag", () => {
    expect(parseRoute("#/queue/f-123")).toEqual({ view: "queue", flagId: "f-123" });
  });

  it("decodes an id that needed escaping", () => {
    expect(parseRoute("#/queue/a%2Fb").flagId).toBe("a/b");
  });

  it("round-trips through routeHref", () => {
    expect(parseRoute(routeHref("queue", "a/b"))).toEqual({ view: "queue", flagId: "a/b" });
    expect(parseRoute(routeHref("qa"))).toEqual({ view: "qa", flagId: null });
    expect(parseRoute(routeHref("metrics"))).toEqual({ view: "metrics", flagId: null });
  });

  it("sends an unknown hash to the queue rather than a blank screen", () => {
    expect(parseRoute("#/nonsense").view).toBe("queue");
  });
});
