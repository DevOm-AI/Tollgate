export interface Segment {
  name: string;
  value: number;
  className: string;
}

export interface Bar {
  label: string;
  segments: Segment[];
}

/** Stacked bars in plain SVG: one bar per day, segments stacked bottom-up. */
export function BarChart({ title, bars, format }: {
  title: string;
  bars: Bar[];
  format: (value: number) => string;
}) {
  const width = 600;
  const height = 140;
  const max = Math.max(1, ...bars.map((bar) => total(bar)));
  const slot = width / Math.max(bars.length, 1);
  const barWidth = Math.max(2, slot * 0.7);
  const legend = bars[0]?.segments ?? [];

  return (
    <figure className="chart">
      <figcaption>
        {title}
        <span className="legend">
          {legend.map((segment) => (
            <span key={segment.name}>
              <i className={segment.className} /> {segment.name}
            </span>
          ))}
        </span>
      </figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title} preserveAspectRatio="none">
        {bars.map((bar, i) => {
          let y = height;
          return (
            <g key={bar.label}>
              <title>{`${bar.label}: ${bar.segments.map((s) => `${s.name} ${format(s.value)}`).join(", ")}`}</title>
              {bar.segments.map((segment) => {
                const h = (segment.value / max) * (height - 4);
                y -= h;
                return (
                  <rect
                    key={segment.name}
                    className={segment.className}
                    x={i * slot + (slot - barWidth) / 2}
                    y={y}
                    width={barWidth}
                    height={h}
                  />
                );
              })}
            </g>
          );
        })}
      </svg>
      <div className="axis">
        <span>{bars[0]?.label}</span>
        <span>peak {format(max)}</span>
        <span>{bars[bars.length - 1]?.label}</span>
      </div>
    </figure>
  );
}

function total(bar: Bar): number {
  return bar.segments.reduce((sum, segment) => sum + segment.value, 0);
}
