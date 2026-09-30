// Money is whole micro-dollars (1 USD = 1,000,000) end to end; only the display divides.
export const MICROS_PER_DOLLAR = 1_000_000;

/** $1.50, $0.000123 — enough decimals to show the smallest non-zero amount. */
export function dollars(micros: number): string {
  const sign = micros < 0 ? "-" : "";
  const abs = Math.abs(micros);
  const whole = Math.floor(abs / MICROS_PER_DOLLAR);
  const fraction = String(abs % MICROS_PER_DOLLAR).padStart(6, "0");
  // Always cents; beyond that only as many digits as the amount needs.
  const shown = fraction.replace(/0+$/, "").padEnd(2, "0");
  return `${sign}$${whole.toLocaleString("en-US")}.${shown}`;
}

/** "1.50" or "$1.5" -> 1500000, without floating point. null if it isn't an amount. */
export function parseDollars(text: string): number | null {
  const match = /^\$?\s*(\d+)(?:\.(\d{0,6}))?$/.exec(text.trim());
  if (!match) return null;
  const [, whole, fraction = ""] = match;
  return Number(whole) * MICROS_PER_DOLLAR + Number(fraction.padEnd(6, "0"));
}

export function count(n: number): string {
  return n.toLocaleString("en-US");
}

export function percent(ratio: number): string {
  return `${(ratio * 100).toFixed(ratio > 0 && ratio < 0.01 ? 2 : 1)}%`;
}

export function milliseconds(ms: number | null): string {
  if (ms === null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}
