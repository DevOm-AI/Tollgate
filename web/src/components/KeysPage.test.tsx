import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AdminApi, KeySummary } from "../api";
import { KeysPage } from "./KeysPage";

const key: KeySummary = {
  id: "k1",
  customer_id: "c1",
  customer_name: "Acme",
  prefix: "ab12cd34",
  rpm_limit: 60,
  tpm_limit: 100_000,
  monthly_budget_micros: 10_000_000,
  is_active: true,
  created_at: "2026-09-30T10:00:00Z",
  period: "2026-09",
  spent_micros: 2_500_000,
  reserved_micros: 0,
};

function fakeApi(overrides: Partial<AdminApi> = {}): AdminApi {
  return {
    keys: vi.fn().mockResolvedValue([key]),
    customers: vi.fn().mockResolvedValue([{ id: "c1", name: "Acme" }]),
    createCustomer: vi.fn().mockResolvedValue({ id: "c2", name: "Globex" }),
    createKey: vi.fn().mockResolvedValue({ ...key, id: "k2", key: "tg_live_SECRETSECRET" }),
    ...overrides,
  } as unknown as AdminApi;
}

describe("KeysPage", () => {
  it("lists keys by prefix with their spend against budget", async () => {
    render(<KeysPage api={fakeApi()} />);

    expect(await screen.findByText("tg_live_ab12cd34…")).toBeInTheDocument();
    expect(screen.getByText("$2.50 of $10.00")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it("creates a key for a new customer and shows it once", async () => {
    const api = fakeApi();
    render(<KeysPage api={api} />);
    await screen.findByText("tg_live_ab12cd34…");

    await userEvent.type(screen.getByLabelText("New customer name"), "Globex");
    await userEvent.clear(screen.getByLabelText("Monthly budget ($)"));
    await userEvent.type(screen.getByLabelText("Monthly budget ($)"), "0.10");
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));

    expect(await screen.findByText("tg_live_SECRETSECRET")).toBeInTheDocument();
    expect(api.createCustomer).toHaveBeenCalledWith("Globex");
    expect(api.createKey).toHaveBeenCalledWith("c2", {
      rpm_limit: 60,
      tpm_limit: 100_000,
      monthly_budget_micros: 100_000,
    });
  });

  it("refuses a budget that isn't a dollar amount", async () => {
    const api = fakeApi();
    render(<KeysPage api={api} />);
    await screen.findByText("tg_live_ab12cd34…");

    await userEvent.clear(screen.getByLabelText("Monthly budget ($)"));
    await userEvent.type(screen.getByLabelText("Monthly budget ($)"), "ten");
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("dollar amount");
    expect(api.createKey).not.toHaveBeenCalled();
  });

  it("shows API errors", async () => {
    render(<KeysPage api={fakeApi({ keys: vi.fn().mockRejectedValue(new Error("Invalid admin key")) })} />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Invalid admin key"));
  });
});
