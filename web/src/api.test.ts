import { AdminApi, ApiError } from "./api";

function fakeFetch(status: number, body: unknown) {
  const calls: { url: string; init: RequestInit }[] = [];
  const impl = (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), { status });
  }) as unknown as typeof fetch;
  return { impl, calls };
}

describe("AdminApi", () => {
  it("sends the admin key and JSON bodies", async () => {
    const { impl, calls } = fakeFetch(201, { id: "c1", name: "Acme" });

    await new AdminApi("http://api.test/", "admin-secret", impl).createCustomer("Acme");

    expect(calls[0].url).toBe("http://api.test/admin/customers");
    expect(calls[0].init.method).toBe("POST");
    expect((calls[0].init.headers as Record<string, string>).Authorization).toBe("Bearer admin-secret");
    expect(calls[0].init.body).toBe(JSON.stringify({ name: "Acme" }));
  });

  it("pages the request log back from an id", async () => {
    const { impl, calls } = fakeFetch(200, []);

    await new AdminApi("http://api.test", "k", impl).requests("key-1", 42);

    expect(calls[0].url).toBe("http://api.test/admin/keys/key-1/requests?limit=50&before_id=42");
  });

  it("turns FastAPI and OpenAI errors into messages", async () => {
    const api = (status: number, body: unknown) =>
      new AdminApi("http://api.test", "k", fakeFetch(status, body).impl);

    await expect(api(404, { detail: "Key not found" }).keys()).rejects.toEqual(
      new ApiError(404, "Key not found"),
    );
    await expect(api(422, { detail: [{ msg: "too small" }] }).keys()).rejects.toThrow("too small");
    await expect(api(429, { error: { message: "slow down" } }).keys()).rejects.toThrow("slow down");
  });

  it("says when Tollgate can't be reached", async () => {
    const failing = (async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch;

    await expect(new AdminApi("http://down.test", "k", failing).keys()).rejects.toThrow(
      "Can't reach Tollgate at http://down.test",
    );
  });
});
