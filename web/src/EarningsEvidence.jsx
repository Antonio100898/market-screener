import { epsEvidence, earningsQuality } from "./earningsEvidence.js";

export { epsEvidence, earningsQuality };

const W = 76;
const H = 21;

/** Visualizes Graham's defensive ten-year EPS evidence without mixing in TTM. */
export default function EarningsEvidence({ annual, fcf, latestFy }) {
  const evidence = epsEvidence(annual, latestFy);
  if (!evidence) return <span className="dim">—</span>;
  const { years, values, present, positive, stable, growth } = evidence;
  const fcfValues = years.map((year) => (fcf && Object.hasOwn(fcf, year) ? fcf[year] : null));
  const points = segments(values);
  const fcfPoints = segments(fcfValues);
  const observed = [...values, ...fcfValues].filter((value) => value != null);
  const min = Math.min(...observed, 0);
  const max = Math.max(...observed, 0);
  const span = max - min || 1;
  const x = (i) => (i / 9) * W;
  const y = (value) => H - ((value - min) / span) * H;
  const colour = stable && growth != null && growth >= 100 / 3 ? "var(--pass)"
    : values.some((value) => value != null && value <= 0) ? "var(--fail)" : "var(--insuf)";
  const title = [
    `Completed fiscal-year EPS: ${years.map((year, i) => `${year} ${values[i] == null ? "—" : values[i].toFixed(2)}`).join(" · ")}`,
    `${positive}/${present} positive EPS years in the ten-year window`,
    `${fcfValues.filter((value) => value != null).length}/${fcfValues.length} FCF/share years shown`,
  ].join("\n");
  return <span className="eps-evidence" title={title}>
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
      {min < 0 && <line x1="0" x2={W} y1={y(0)} y2={y(0)} stroke="var(--line)" strokeWidth="1" />}
      {points.map((segment, index) => <polyline key={`eps-${index}`}
        points={segment.map(([i, value]) => `${x(i).toFixed(1)},${y(value).toFixed(1)}`).join(" ")}
        fill="none" stroke={colour} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />)}
      {fcfPoints.map((segment, index) => <polyline key={`fcf-${index}`}
        points={segment.map(([i, value]) => `${x(i).toFixed(1)},${y(value).toFixed(1)}`).join(" ")}
        fill="none" stroke="var(--info, #6aa9ff)" strokeWidth="1.2" strokeDasharray="2 1"
        strokeLinecap="round" strokeLinejoin="round" />)}
    </svg>
    <span className="eps-proof"><b className={stable ? "ok" : ""}>{positive}/{present}</b><em>positive</em></span>
  </span>;
}

function segments(values) {
  const out = [];
  let segment = [];
  values.forEach((value, index) => {
    if (value == null) {
      if (segment.length) out.push(segment);
      segment = [];
    } else {
      segment.push([index, value]);
    }
  });
  if (segment.length) out.push(segment);
  return out;
}
