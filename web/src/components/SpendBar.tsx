import { dollars } from "../format";

/** Spent and reserved against the monthly budget. */
export function SpendBar({ spent, reserved, budget }: { spent: number; reserved: number; budget: number }) {
  const share = (micros: number) => (budget > 0 ? Math.min(100, (micros / budget) * 100) : 0);
  return (
    <div className="spend">
      <div
        className="meter"
        role="meter"
        aria-label="Spend against budget"
        aria-valuemin={0}
        aria-valuemax={budget}
        aria-valuenow={spent}
      >
        <span className="spent" style={{ width: `${share(spent)}%` }} />
        <span className="reserved" style={{ width: `${share(reserved)}%` }} />
      </div>
      <span className="muted">
        {dollars(spent)} of {dollars(budget)}
        {reserved > 0 && ` (${dollars(reserved)} held)`}
      </span>
    </div>
  );
}
