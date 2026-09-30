import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api";
import { costMicros, outputTokens, type DemoApi, type DemoStatus, type Usage } from "../demo";
import { dollars } from "../format";

export function Playground({ api }: { api: DemoApi }) {
  const [status, setStatus] = useState<DemoStatus | null>(null);
  const [prompt, setPrompt] = useState("Explain what an API gateway does in two sentences.");
  const [answer, setAnswer] = useState("");
  const [usage, setUsage] = useState<Usage | null>(null);
  const [requestsLeft, setRequestsLeft] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const next = await api.status();
      setStatus(next);
      setRequestsLeft(next.requests_left);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function send(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setAnswer("");
    setUsage(null);
    try {
      await api.ask(prompt, {
        onText: (text) => setAnswer((current) => current + text),
        onUsage: setUsage,
        onRequestsLeft: setRequestsLeft,
      });
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
      void refresh();
    }
  }

  const cost =
    usage && status?.input_micros_per_1k != null && status.output_micros_per_1k != null
      ? costMicros(usage, status.input_micros_per_1k, status.output_micros_per_1k)
      : null;

  return (
    <section className="playground">
      <h2>Try Tollgate</h2>
      <p className="muted">
        Your prompt goes through the real gateway with a demo key: rate limited to{" "}
        {status?.rpm_limit ?? "…"} requests a minute and a {status ? dollars(status.budget_micros) : "…"}{" "}
        monthly budget, enforced by Tollgate itself. Model: <code>{status?.model ?? "…"}</code>.
      </p>

      <div className="tiles">
        <div className="tile">
          <span className="muted">Budget left</span>
          <strong>{status ? dollars(status.remaining_micros) : "…"}</strong>
        </div>
        <div className="tile">
          <span className="muted">Requests left this minute</span>
          <strong>{requestsLeft ?? "…"}</strong>
        </div>
        <div className="tile">
          <span className="muted">Cost of this request</span>
          <strong>{cost === null ? "—" : dollars(cost)}</strong>
          {usage && (
            <span className="muted">
              {usage.prompt_tokens} in / {outputTokens(usage)} out tokens
            </span>
          )}
        </div>
      </div>

      <form onSubmit={send} className="stack">
        <label>
          Prompt
          <textarea
            rows={3}
            value={prompt}
            maxLength={status?.prompt_max_chars}
            onChange={(e) => setPrompt(e.target.value)}
          />
        </label>
        <button type="submit" disabled={busy || !prompt.trim()}>
          {busy ? "Answering…" : "Send"}
        </button>
      </form>
      {error && <p role="alert" className="error">{error}</p>}
      <output className="answer" aria-live="polite">
        {answer || <span className="muted">The answer streams in here.</span>}
      </output>
    </section>
  );
}

function explain(err: unknown): string {
  if (err instanceof ApiError && err.status === 429) {
    return `${err.message} That's the demo's rate limit doing its job.`;
  }
  if (err instanceof ApiError && err.status === 402) {
    return `${err.message} The demo's budget is used up; it resets daily.`;
  }
  return err instanceof Error ? err.message : String(err);
}
