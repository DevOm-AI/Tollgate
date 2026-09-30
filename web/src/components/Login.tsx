import { useState } from "react";
import { AdminApi, ApiError } from "../api";
import { DEFAULT_API_URL, type Session } from "../session";

export function Login({ onSignIn }: { onSignIn: (session: Session) => void }) {
  const [baseUrl, setBaseUrl] = useState(DEFAULT_API_URL);
  const [adminKey, setAdminKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      // Checks the address and the key in one go.
      await new AdminApi(baseUrl, adminKey).keys();
      onSignIn({ baseUrl, adminKey });
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Wrong admin key" : String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <h1>Tollgate</h1>
      <p className="muted">Sign in with the admin key (ADMIN_API_KEY).</p>
      <form onSubmit={submit} className="stack">
        <label>
          API address
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} required />
        </label>
        <label>
          Admin key
          <input
            type="password"
            value={adminKey}
            onChange={(e) => setAdminKey(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && <p role="alert" className="error">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? "Checking…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
