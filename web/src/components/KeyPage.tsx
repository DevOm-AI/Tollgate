import { useCallback, useEffect, useState } from "react";
import type { AdminApi, KeyStats, RequestEntry } from "../api";
import { count, dollars, milliseconds, percent } from "../format";
import { BarChart } from "./BarChart";
import { LimitsForm } from "./LimitsForm";
import { SpendBar } from "./SpendBar";

const WINDOWS = [7, 30, 90];

export function KeyPage({ api, keyId }: { api: AdminApi; keyId: string }) {
  const [days, setDays] = useState(30);
  const [stats, setStats] = useState<KeyStats | null>(null);
  const [log, setLog] = useState<RequestEntry[]>([]);
  const [moreLog, setMoreLog] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [loadedStats, firstPage] = await Promise.all([api.stats(keyId, days), api.requests(keyId)]);
      setStats(loadedStats);
      setLog(firstPage);
      setMoreLog(firstPage.length === 50);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [api, keyId, days]);

  useEffect(() => {
    void load();
  }, [load]);

  async function loadOlder() {
    const older = await api.requests(keyId, log[log.length - 1]?.id);
    setLog([...log, ...older]);
    setMoreLog(older.length === 50);
  }

  async function revoke() {
    if (!window.confirm("Revoke this key? Requests with it will be refused from now on.")) return;
    await api.revokeKey(keyId);
    await load();
  }

  if (error) return <p role="alert" className="error">{error}</p>;
  if (!stats) return <p className="muted">Loading…</p>;
  const { key } = stats;
  const label = (day: string) => day.slice(5);

  return (
    <section>
      <p>
        <a href="#/">← All keys</a>
      </p>
      <h2>
        <code>tg_live_{key.prefix}…</code> <span className="muted">{key.customer_name}</span>
        {!key.is_active && <span className="badge">revoked</span>}
      </h2>

      <div className="tiles">
        <div className="tile">
          <span className="muted">Spend in {key.period}</span>
          <SpendBar spent={key.spent_micros} reserved={key.reserved_micros} budget={key.monthly_budget_micros} />
        </div>
        <div className="tile">
          <span className="muted">Latency p50 / p95</span>
          <strong>
            {milliseconds(stats.latency_p50_ms)} / {milliseconds(stats.latency_p95_ms)}
          </strong>
        </div>
      </div>

      <div className="row">
        <span className="muted">Last</span>
        {WINDOWS.map((window) => (
          <button
            key={window}
            type="button"
            className={window === days ? "selected" : "link"}
            onClick={() => setDays(window)}
          >
            {window} days
          </button>
        ))}
      </div>

      <BarChart
        title="Requests per day"
        format={count}
        bars={stats.daily.map((day) => ({
          label: label(day.day),
          segments: [
            { name: "answered", value: day.requests - day.errors, className: "fill-ok" },
            { name: "failed", value: day.errors, className: "fill-error" },
          ],
        }))}
      />
      <BarChart
        title="Tokens per day"
        format={count}
        bars={stats.daily.map((day) => ({
          label: label(day.day),
          segments: [
            { name: "input", value: day.input_tokens, className: "fill-input" },
            { name: "output", value: day.output_tokens, className: "fill-output" },
          ],
        }))}
      />

      <h3>Error rate by provider</h3>
      {stats.providers.length === 0 ? (
        <p className="muted">No requests in this window.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Provider</th>
              <th>Requests</th>
              <th>Errors</th>
              <th>Error rate</th>
            </tr>
          </thead>
          <tbody>
            {stats.providers.map((provider) => (
              <tr key={provider.provider}>
                <td>{provider.provider}</td>
                <td>{count(provider.requests)}</td>
                <td>{count(provider.errors)}</td>
                <td>{percent(provider.error_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {key.is_active && (
        <div className="panel">
          <h3>Limits</h3>
          <LimitsForm
            initial={key}
            submitLabel="Save limits"
            onSubmit={async (limits) => {
              await api.updateKey(keyId, limits);
              await load();
            }}
          />
          <button type="button" className="danger" onClick={() => void revoke()}>
            Revoke key
          </button>
        </div>
      )}

      <h3>Request log</h3>
      <p className="muted">Metadata only: Tollgate never stores prompts or answers.</p>
      <table>
        <thead>
          <tr>
            <th>Time (UTC)</th>
            <th>Model</th>
            <th>Provider</th>
            <th>Status</th>
            <th>Tokens in / out</th>
            <th>Cost</th>
            <th>Latency</th>
          </tr>
        </thead>
        <tbody>
          {log.map((entry) => (
            <tr key={entry.id}>
              <td>{entry.created_at.replace("T", " ").slice(0, 19)}</td>
              <td>{entry.model}</td>
              <td>{entry.provider ?? "—"}</td>
              <td className={entry.status === "ok" ? "ok" : "error"}>{entry.status}</td>
              <td>
                {count(entry.input_tokens)} / {count(entry.output_tokens)}
              </td>
              <td>{dollars(entry.cost_micros)}</td>
              <td>{milliseconds(entry.latency_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {moreLog && (
        <button type="button" className="link" onClick={() => void loadOlder()}>
          Load older
        </button>
      )}
    </section>
  );
}
