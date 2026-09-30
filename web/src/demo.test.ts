import { costMicros, DemoApi } from "./demo";

function sseResponse(events: unknown[], headers: Record<string, string> = {}): Response {
  const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n";
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream", ...headers } });
}

describe("costMicros", () => {
  it("matches Tollgate's formula: whole micro-dollars, rounded up", () => {
    expect(costMicros({ prompt_tokens: 1, completion_tokens: 32 }, 100, 400)).toBe(13);
    expect(costMicros({ prompt_tokens: 10, completion_tokens: 0 }, 100, 400)).toBe(1);
    expect(costMicros({ prompt_tokens: 0, completion_tokens: 0 }, 100, 400)).toBe(0);
  });
});

describe("DemoApi.ask", () => {
  it("streams text, usage and the requests left", async () => {
    const fetchImpl = (async () =>
      sseResponse(
        [
          { choices: [{ delta: { content: "Hello" } }] },
          { choices: [{ delta: { content: " world" } }] },
          { choices: [], usage: { prompt_tokens: 3, completion_tokens: 2 } },
        ],
        { "X-RateLimit-Remaining-Requests": "4" },
      )) as unknown as typeof fetch;
    const text: string[] = [];
    const seen: { usage?: unknown; left?: number } = {};

    await new DemoApi("http://api.test", fetchImpl).ask("Hi", {
      onText: (t) => text.push(t),
      onUsage: (u) => (seen.usage = u),
      onRequestsLeft: (n) => (seen.left = n),
    });

    expect(text.join("")).toBe("Hello world");
    expect(seen).toEqual({ usage: { prompt_tokens: 3, completion_tokens: 2 }, left: 4 });
  });

  it("raises the gateway's message for a refused request", async () => {
    const fetchImpl = (async () =>
      new Response(JSON.stringify({ error: { message: "Rate limit reached" } }), {
        status: 429,
      })) as unknown as typeof fetch;

    await expect(
      new DemoApi("http://api.test", fetchImpl).ask("Hi", {
        onText: () => {},
        onUsage: () => {},
        onRequestsLeft: () => {},
      }),
    ).rejects.toMatchObject({ status: 429, message: "Rate limit reached" });
  });

  it("raises a mid-stream error event", async () => {
    const fetchImpl = (async () =>
      sseResponse([{ error: { message: "The provider 'mock' failed mid-answer" } }])) as unknown as typeof fetch;

    await expect(
      new DemoApi("http://api.test", fetchImpl).ask("Hi", {
        onText: () => {},
        onUsage: () => {},
        onRequestsLeft: () => {},
      }),
    ).rejects.toThrow("failed mid-answer");
  });
});
