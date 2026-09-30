import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "../api";
import type { DemoApi, DemoStatus, StreamHandlers } from "../demo";
import { Playground } from "./Playground";

const status: DemoStatus = {
  model: "mock",
  max_tokens: 256,
  prompt_max_chars: 2000,
  budget_micros: 100_000,
  spent_micros: 0,
  reserved_micros: 0,
  remaining_micros: 100_000,
  rpm_limit: 5,
  requests_left: 5,
  input_micros_per_1k: 100,
  output_micros_per_1k: 400,
};

function fakeApi(ask: DemoApi["ask"]): DemoApi {
  return {
    status: vi
      .fn()
      .mockResolvedValueOnce(status)
      .mockResolvedValue({ ...status, spent_micros: 13, remaining_micros: 99_987, requests_left: 4 }),
    ask,
  } as unknown as DemoApi;
}

describe("Playground", () => {
  it("shows the demo's limits before anything is sent", async () => {
    render(<Playground api={fakeApi(vi.fn())} />);

    expect(await screen.findByText("$0.10")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  it("streams the answer and shows its cost and what's left", async () => {
    const ask = vi.fn(async (_prompt: string, on: StreamHandlers) => {
      on.onRequestsLeft(4);
      on.onText("Hello");
      on.onText(" there");
      on.onUsage({ prompt_tokens: 1, completion_tokens: 32 });
    });
    render(<Playground api={fakeApi(ask)} />);
    await screen.findByText("$0.10");

    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("Hello there")).toBeInTheDocument();
    expect(screen.getByText("$0.000013")).toBeInTheDocument();
    expect(screen.getByText("1 in / 32 out tokens")).toBeInTheDocument();
    expect(await screen.findByText("$0.099987")).toBeInTheDocument();
  });

  it("explains a rate limit hit", async () => {
    const ask = vi.fn().mockRejectedValue(new ApiError(429, "Rate limit reached: 5 requests per minute."));
    render(<Playground api={fakeApi(ask)} />);
    await screen.findByText("$0.10");

    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("the demo's rate limit doing its job");
  });

  it("explains a used-up budget", async () => {
    const ask = vi.fn().mockRejectedValue(new ApiError(402, "Monthly budget exceeded."));
    render(<Playground api={fakeApi(ask)} />);
    await screen.findByText("$0.10");

    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("budget is used up");
  });
});
