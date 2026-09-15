import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../src/lib/api";

type Call = { url: string; init?: RequestInit };

function stubFetch(handler: (call: Call) => Response | Promise<Response>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(handler({ url, init }));
  });
  return calls;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

describe("paths match the pinned contract", () => {
  it("asks for the endpoints the contract names, unprefixed", async () => {
    const calls = stubFetch(({ url }) => {
      if (url.endsWith("/me")) return json({ identity: "a", roles: [], dev_bypass: false });
      if (url.includes("/flags")) return json({ flags: [] });
      if (url.includes("/qa-sample")) return json({ items: [] });
      return json({});
    });
    await api.me();
    await api.flags();
    await api.qaSample();
    await api.precision();
    await api.falseNegatives();
    await api.chainStatus();
    expect(calls.map((call) => call.url)).toEqual([
      "/me",
      "/flags",
      "/qa-sample",
      "/metrics/precision",
      "/metrics/false_negative_estimate",
      "/audit/chain_status",
    ]);
  });

  it("sends the queue filters as query parameters, and omits the empty ones", async () => {
    const calls = stubFetch(() => json({ flags: [] }));
    await api.flags({ severity: "high", undispositioned: true });
    await api.flags({ severity: "", undispositioned: false });
    expect(calls[0]?.url).toBe("/flags?severity=high&undispositioned=true");
    expect(calls[1]?.url).toBe("/flags");
  });

  it("escapes a flag id into the path", async () => {
    const calls = stubFetch(() => json({}));
    await api.flag("a/b");
    await api.audio("a/b");
    expect(calls.map((call) => call.url)).toEqual(["/flags/a%2Fb", "/flags/a%2Fb/audio"]);
  });

  it("posts a disposition as the contract body", async () => {
    const calls = stubFetch(() => json({ disposition_id: "d", seq: 7, row_hash: "abc" }, 201));
    const receipt = await api.disposition("f1", { disposition: "confirmed", note: "checked" });
    expect(calls[0]?.init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
      disposition: "confirmed",
      note: "checked",
    });
    expect(receipt.seq).toBe(7);
  });

  it("unwraps the envelopes the contract wraps lists in", async () => {
    stubFetch(({ url }) =>
      url.includes("qa-sample") ? json({ items: [{ flag_id: "q" }] }) : json({ flags: [{ flag_id: "f" }] }),
    );
    expect((await api.flags()).map((flag) => flag.flag_id)).toEqual(["f"]);
    expect((await api.qaSample()).map((flag) => flag.flag_id)).toEqual(["q"]);
  });
});

describe("a refusal is an answer, not a crash", () => {
  it("marks 403 as forbidden and keeps the path that was refused", async () => {
    stubFetch(() => json({ detail: "role not permitted" }, 403));
    const error = await api.flags().catch((cause) => cause);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.forbidden).toBe(true);
    expect(error.unauthenticated).toBe(false);
    expect(error.path).toBe("/flags");
  });

  it("keeps 401 distinguishable from 403", async () => {
    stubFetch(() => json({}, 401));
    const error = await api.flag("f1").catch((cause) => cause);
    expect(error.unauthenticated).toBe(true);
    expect(error.forbidden).toBe(false);
  });

  it("does not mistake a transport failure for a permission answer", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new Error("network down")));
    const error = await api.chainStatus().catch((cause) => cause);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.forbidden).toBe(false);
  });

  it("truncates a long error body rather than pasting a page into the UI", async () => {
    stubFetch(() => new Response("x".repeat(5000), { status: 500 }));
    const error = await api.precision().catch((cause) => cause);
    expect(error.message.length).toBeLessThanOrEqual(400);
  });
});
