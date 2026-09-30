// The public playground's API: no admin key, just the demo routes.
import { ApiError } from "./api";
import { sseEvents } from "./sse";

export interface DemoStatus {
  model: string;
  max_tokens: number;
  prompt_max_chars: number;
  budget_micros: number;
  spent_micros: number;
  reserved_micros: number;
  remaining_micros: number;
  rpm_limit: number;
  requests_left: number | null;
  input_micros_per_1k: number | null;
  output_micros_per_1k: number | null;
}

export interface Usage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens?: number;
}

/** Output tokens Tollgate bills: Gemini's "thinking" only shows in total_tokens. */
export function outputTokens(usage: Usage): number {
  if (usage.total_tokens === undefined) return usage.completion_tokens;
  return Math.max(usage.completion_tokens, usage.total_tokens - usage.prompt_tokens);
}

/** Tollgate's formula: whole micro-dollars, rounded up. */
export function costMicros(usage: Usage, inputPer1k: number, outputPer1k: number): number {
  return Math.ceil((usage.prompt_tokens * inputPer1k + outputTokens(usage) * outputPer1k) / 1000);
}

export interface StreamHandlers {
  onText: (text: string) => void;
  onUsage: (usage: Usage) => void;
  onRequestsLeft: (left: number) => void;
}

export class DemoApi {
  constructor(
    readonly baseUrl: string,
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

  async status(): Promise<DemoStatus> {
    const response = await this.call("/demo/status", { method: "GET" });
    return (await response.json()) as DemoStatus;
  }

  /** Streams the answer to `prompt`, word by word, through the handlers. */
  async ask(prompt: string, handlers: StreamHandlers): Promise<void> {
    const response = await this.call("/demo/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    const left = response.headers.get("X-RateLimit-Remaining-Requests");
    if (left !== null) handlers.onRequestsLeft(Number(left));
    if (!response.body) return;
    for await (const event of sseEvents(response.body)) {
      const chunk = event as {
        choices?: { delta?: { content?: string } }[];
        usage?: Usage;
        error?: { message: string };
      };
      if (chunk.error) throw new ApiError(502, chunk.error.message);
      const text = chunk.choices?.[0]?.delta?.content;
      if (text) handlers.onText(text);
      if (chunk.usage) handlers.onUsage(chunk.usage);
    }
  }

  private async call(path: string, init: RequestInit): Promise<Response> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl.replace(/\/$/, "")}${path}`, init);
    } catch {
      throw new ApiError(0, "Can't reach Tollgate right now. It may be waking up; try again.");
    }
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        const body = await response.json();
        message = body?.error?.message ?? body?.detail ?? message;
      } catch {
        // Keep the status message.
      }
      throw new ApiError(response.status, message);
    }
    return response;
  }
}
