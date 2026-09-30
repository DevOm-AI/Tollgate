// A typed client for Tollgate's admin API, used with the admin key.

export interface KeySummary {
  id: string;
  customer_id: string;
  customer_name: string;
  prefix: string;
  rpm_limit: number;
  tpm_limit: number;
  monthly_budget_micros: number;
  is_active: boolean;
  created_at: string;
  period: string;
  spent_micros: number;
  reserved_micros: number;
}

export interface DayStats {
  day: string;
  requests: number;
  errors: number;
  input_tokens: number;
  output_tokens: number;
  cost_micros: number;
}

export interface ProviderStats {
  provider: string;
  requests: number;
  errors: number;
  error_rate: number;
}

export interface KeyStats {
  key: KeySummary;
  days: number;
  daily: DayStats[];
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  providers: ProviderStats[];
}

export interface RequestEntry {
  id: number;
  model: string;
  provider: string | null;
  status: string;
  input_tokens: number;
  output_tokens: number;
  cost_micros: number;
  latency_ms: number | null;
  created_at: string;
}

export interface Customer {
  id: string;
  name: string;
  stripe_customer_id: string | null;
  created_at: string;
}

export interface Limits {
  rpm_limit: number;
  tpm_limit: number;
  monthly_budget_micros: number;
}

export interface CreatedKey extends Limits {
  id: string;
  key: string;
  prefix: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export class AdminApi {
  constructor(
    readonly baseUrl: string,
    private readonly adminKey: string,
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

  keys = () => this.request<KeySummary[]>("GET", "/admin/keys");
  customers = () => this.request<Customer[]>("GET", "/admin/customers");
  stats = (keyId: string, days = 30) =>
    this.request<KeyStats>("GET", `/admin/keys/${keyId}/stats?days=${days}`);
  requests = (keyId: string, beforeId?: number) =>
    this.request<RequestEntry[]>(
      "GET",
      `/admin/keys/${keyId}/requests?limit=50${beforeId ? `&before_id=${beforeId}` : ""}`,
    );
  createCustomer = (name: string) => this.request<Customer>("POST", "/admin/customers", { name });
  createKey = (customerId: string, limits: Limits) =>
    this.request<CreatedKey>("POST", `/admin/customers/${customerId}/keys`, limits);
  updateKey = (keyId: string, limits: Partial<Limits>) =>
    this.request<KeySummary>("PATCH", `/admin/keys/${keyId}`, limits);
  revokeKey = (keyId: string) => this.request<KeySummary>("POST", `/admin/keys/${keyId}/revoke`);

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
        method,
        headers: {
          Authorization: `Bearer ${this.adminKey}`,
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new ApiError(0, `Can't reach Tollgate at ${this.baseUrl}`);
    }
    if (!response.ok) {
      throw new ApiError(response.status, await errorMessage(response));
    }
    return (await response.json()) as T;
  }
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail ?? body?.error?.message;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  } catch {
    // Not JSON: fall through to the status.
  }
  return `Request failed (${response.status})`;
}
