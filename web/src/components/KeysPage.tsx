import { useCallback, useEffect, useState } from "react";
import type { AdminApi, CreatedKey, Customer, KeySummary, Limits } from "../api";
import { count } from "../format";
import { LimitsForm } from "./LimitsForm";
import { SpendBar } from "./SpendBar";

const NEW_KEY_LIMITS: Limits = { rpm_limit: 60, tpm_limit: 100_000, monthly_budget_micros: 10_000_000 };

export function KeysPage({ api }: { api: AdminApi }) {
  const [keys, setKeys] = useState<KeySummary[] | null>(null);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [loadedKeys, loadedCustomers] = await Promise.all([api.keys(), api.customers()]);
      setKeys(loadedKeys);
      setCustomers(loadedCustomers);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section>
      <h2>Keys</h2>
      {error && <p role="alert" className="error">{error}</p>}
      {keys === null ? (
        <p className="muted">Loading…</p>
      ) : keys.length === 0 ? (
        <p className="muted">No keys yet. Create one below.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Key</th>
              <th>Customer</th>
              <th>Limits</th>
              <th>Spend this month</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key.id}>
                <td>
                  <a href={`#/keys/${key.id}`}>
                    <code>tg_live_{key.prefix}…</code>
                  </a>
                </td>
                <td>{key.customer_name}</td>
                <td className="muted">
                  {count(key.rpm_limit)} req/min · {count(key.tpm_limit)} tok/min
                </td>
                <td>
                  <SpendBar spent={key.spent_micros} reserved={key.reserved_micros} budget={key.monthly_budget_micros} />
                </td>
                <td>{key.is_active ? <span className="ok">active</span> : <span className="muted">revoked</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <NewKey api={api} customers={customers} onCreated={load} />
    </section>
  );
}

function NewKey({ api, customers, onCreated }: {
  api: AdminApi;
  customers: Customer[];
  onCreated: () => Promise<void>;
}) {
  const [customerId, setCustomerId] = useState("");
  const [newCustomer, setNewCustomer] = useState("");
  const [created, setCreated] = useState<CreatedKey | null>(null);

  async function create(limits: Limits) {
    let id = customerId;
    if (!id) {
      if (!newCustomer.trim()) throw new Error("Pick a customer or name a new one");
      id = (await api.createCustomer(newCustomer.trim())).id;
    }
    setCreated(await api.createKey(id, limits));
    setNewCustomer("");
    await onCreated();
  }

  return (
    <div className="panel">
      <h3>New key</h3>
      <div className="row">
        <label>
          Customer
          <select value={customerId} onChange={(e) => setCustomerId(e.target.value)}>
            <option value="">New customer…</option>
            {customers.map((customer) => (
              <option key={customer.id} value={customer.id}>
                {customer.name}
              </option>
            ))}
          </select>
        </label>
        {!customerId && (
          <label>
            New customer name
            <input value={newCustomer} onChange={(e) => setNewCustomer(e.target.value)} />
          </label>
        )}
      </div>
      <LimitsForm initial={NEW_KEY_LIMITS} submitLabel="Create key" onSubmit={create} />
      {created && (
        <div className="created" role="status">
          <p>
            <strong>Copy this key now.</strong> Tollgate only stores its hash, so it can't be shown
            again.
          </p>
          <code className="secret">{created.key}</code>
          <button type="button" onClick={() => void navigator.clipboard?.writeText(created.key)}>
            Copy
          </button>
        </div>
      )}
    </div>
  );
}
