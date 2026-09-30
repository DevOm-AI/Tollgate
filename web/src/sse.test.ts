import { sseEvents } from "./sse";

function streamOf(...parts: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(encoder.encode(part));
      controller.close();
    },
  });
}

async function collect(body: ReadableStream<Uint8Array>): Promise<unknown[]> {
  const out = [];
  for await (const event of sseEvents(body)) out.push(event);
  return out;
}

describe("sseEvents", () => {
  it("parses each event, even when split across reads", async () => {
    const events = await collect(streamOf('data: {"a"', ': 1}\n\ndata: {"b": 2}\n', "\n"));

    expect(events).toEqual([{ a: 1 }, { b: 2 }]);
  });

  it("stops at [DONE]", async () => {
    const events = await collect(streamOf('data: {"a": 1}\n\ndata: [DONE]\n\ndata: {"b": 2}\n\n'));

    expect(events).toEqual([{ a: 1 }]);
  });
});
