import { useState } from "react";
import type { Limits } from "../api";
import { MICROS_PER_DOLLAR, parseDollars } from "../format";

/** Requests/min, tokens/min and the monthly budget (typed in dollars, sent as micros). */
export function LimitsForm({ initial, submitLabel, onSubmit }: {
  initial: Limits;
  submitLabel: string;
  onSubmit: (limits: Limits) => Promise<void>;
}) {
  const [rpm, setRpm] = useState(String(initial.rpm_limit));
  const [tpm, setTpm] = useState(String(initial.tpm_limit));
  const [budget, setBudget] = useState(
    (initial.monthly_budget_micros / MICROS_PER_DOLLAR).toString(),
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const budgetMicros = parseDollars(budget);
    const rpmLimit = Number(rpm);
    const tpmLimit = Number(tpm);
    if (budgetMicros === null) return setError("Budget must be a dollar amount, e.g. 1.50");
    if (!Number.isInteger(rpmLimit) || rpmLimit < 1 || !Number.isInteger(tpmLimit) || tpmLimit < 1) {
      return setError("Limits must be whole numbers above zero");
    }
    setError(null);
    setBusy(true);
    try {
      await onSubmit({ rpm_limit: rpmLimit, tpm_limit: tpmLimit, monthly_budget_micros: budgetMicros });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="limits">
      <label>
        Requests / min
        <input inputMode="numeric" value={rpm} onChange={(e) => setRpm(e.target.value)} />
      </label>
      <label>
        Tokens / min
        <input inputMode="numeric" value={tpm} onChange={(e) => setTpm(e.target.value)} />
      </label>
      <label>
        Monthly budget ($)
        <input inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} />
      </label>
      <button type="submit" disabled={busy}>
        {submitLabel}
      </button>
      {error && <p role="alert" className="error">{error}</p>}
    </form>
  );
}
