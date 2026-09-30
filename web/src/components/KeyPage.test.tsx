import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AdminApi, KeyStats, RequestEntry } from "../api";
import { KeyPage } from "./KeyPage";

const stats: KeyStats = {
  key: {
    id: "k1",
    customer_id: "c1",
    customer_name: "Acme",
    prefix: "ab12cd34",
    rpm_limit: 60,
    tpm_limit: 100_000,
    monthly_budget_micros: 1_000_000,
    is_active: true,
    created_at: "2026-09-01T00:00:00Z",
    period: "2026-09",
    spent_micros: 250_000,
    reserved_micros: 1_000,
  },
  days: 30,
  daily: [
    { day: "2026-09-29", requests: 3, errors: 1, input_tokens: 30, output_tokens: 60, cost_micros: 12 },
    { day: "2026-09-30", requests: 5, errors: 0, input_tokens: 50, output_tokens: 90, cost_micros: 20 },
  ],
  latency_p50_ms: 180,
  latency_p95_ms: 1450,
  providers: [{ provider: "groq", requests: 8, errors: 1, error_rate: 0.125 }],
};

const entry: RequestEntry = {
  id: 7,
  model: "fast-chat",
  provider: "groq",
  status: "timeout",
  input_tokens: 12,
  output_tokens: 0,
  cost_micros: 0,
  latency_ms: 30_000,
  created_at: "2026-09-30T12:34:56Z",
};

function fakeApi(): AdminApi {
  return {
    stats: vi.fn().mockResolvedValue(stats),
    requests: vi.fn().mockResolvedValue([entry]),
    updateKey: vi.fn().mockResolvedValue(stats.key),
    revokeKey: vi.fn().mockResolvedValue({ ...stats.key, is_active: false }),
  } as unknown as AdminApi;
}

describe("KeyPage", () => {
  it("shows spend, latency, charts, provider errors and the request log", async () => {
    render(<KeyPage api={fakeApi()} keyId="k1" />);

    expect(await screen.findByText("$0.25 of $1.00 ($0.001 held)")).toBeInTheDocument();
    expect(screen.getByText("180 ms / 1.45 s")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Requests per day" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Tokens per day" })).toBeInTheDocument();
    expect(screen.getByText("12.5%")).toBeInTheDocument();
    expect(screen.getByText("2026-09-30 12:34:56")).toBeInTheDocument();
    expect(screen.getByText("timeout")).toBeInTheDocument();
  });

  it("changes the window", async () => {
    const api = fakeApi();
    render(<KeyPage api={api} keyId="k1" />);
    await screen.findByText("180 ms / 1.45 s");

    await userEvent.click(screen.getByRole("button", { name: "7 days" }));

    expect(api.stats).toHaveBeenLastCalledWith("k1", 7);
  });

  it("saves new limits in micro-dollars", async () => {
    const api = fakeApi();
    render(<KeyPage api={api} keyId="k1" />);
    await screen.findByText("180 ms / 1.45 s");

    await userEvent.clear(screen.getByLabelText("Requests / min"));
    await userEvent.type(screen.getByLabelText("Requests / min"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Save limits" }));

    expect(api.updateKey).toHaveBeenCalledWith("k1", {
      rpm_limit: 5,
      tpm_limit: 100_000,
      monthly_budget_micros: 1_000_000,
    });
  });

  it("revokes after confirming", async () => {
    const api = fakeApi();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<KeyPage api={api} keyId="k1" />);
    await screen.findByText("180 ms / 1.45 s");

    await userEvent.click(screen.getByRole("button", { name: "Revoke key" }));

    expect(api.revokeKey).toHaveBeenCalledWith("k1");
  });
});
