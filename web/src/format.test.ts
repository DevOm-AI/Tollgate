import { dollars, milliseconds, parseDollars, percent } from "./format";

describe("dollars", () => {
  it.each([
    [0, "$0.00"],
    [1_500_000, "$1.50"],
    [1_234_567_890, "$1,234.56789"],
    [123, "$0.000123"],
    [10_000, "$0.01"],
    [-2_500_000, "-$2.50"],
  ])("%i micros is %s", (micros, text) => {
    expect(dollars(micros)).toBe(text);
  });
});

describe("parseDollars", () => {
  it.each([
    ["1.50", 1_500_000],
    ["$0.10", 100_000],
    ["12", 12_000_000],
    ["0.000001", 1],
    [" 3.5 ", 3_500_000],
  ])("%s is %i micros", (text, micros) => {
    expect(parseDollars(text)).toBe(micros);
  });

  it.each(["", "abc", "1.2345678", "-1", "1,000"])("refuses %j", (text) => {
    expect(parseDollars(text)).toBeNull();
  });

  it("round-trips without floating point error", () => {
    expect(parseDollars("0.29")).toBe(290_000);
  });
});

describe("other formats", () => {
  it("shows percentages", () => {
    expect(percent(0.25)).toBe("25.0%");
    expect(percent(0.001)).toBe("0.10%");
  });

  it("shows latency", () => {
    expect(milliseconds(null)).toBe("—");
    expect(milliseconds(123.4)).toBe("123 ms");
    expect(milliseconds(2500)).toBe("2.50 s");
  });
});
