import { latestEpsLabel } from "./earningsEvidence.js";

/**
 * The earnings record, drawn at the width of the panel.
 *
 * Every other earnings figure here is a summary — a five-year minimum, a
 * ten-year growth percentage, a smoothed average five years apart. Each of them
 * can be produced by more than one path, and Graham's preference between those
 * paths is most of chapter 15: a decade of steady earning power is not the same
 * business as a flat decade that jumped in its last three years, even when both
 * end at the same number. The path is the one thing a summary cannot carry, so
 * it is drawn.
 *
 * Thirteen years, because that is the span the smoothed comparison uses (three
 * years, five years back, and five years before that). The trailing twelve
 * months is appended dashed: it is where the business is now, and it is not an
 * audited fiscal year.
 */
const YEARS = 13;
const W = 620, H = 190;
const PAD = { left: 46, right: 16, top: 14, bottom: 24 };

export default function EpsCurve({ annualEps, ttmEps, currency = "USD", latestFy }) {
  const years = Object.keys(annualEps ?? {})
    .map(Number).filter(Number.isFinite).sort((a, b) => a - b).slice(-YEARS);
  const latest = latestEpsLabel(annualEps, latestFy);
  const missingCaption = latest != null && (!years.length || latest > years.at(-1))
    ? <p className="dim">FY{latest} EPS unavailable.</p> : null;
  if (years.length < 3) return missingCaption;   // two points are a line, not a record

  const points = years.map((year) => ({ label: `FY${year}`, short: `'${String(year).slice(2)}`,
                                        value: annualEps[String(year)], audited: true }));
  // only when it says something the closed year does not
  if (ttmEps != null && Math.abs(ttmEps - points[points.length - 1].value) > 1e-9) {
    points.push({ label: "TTM", short: "TTM", value: ttmEps, audited: false });
  }

  const values = points.map((p) => p.value);
  // zero is always on the chart: a curve whose baseline floats is a curve that
  // can make a 3% rise look like a doubling
  const top = Math.max(...values, 0);
  const bottom = Math.min(...values, 0);
  const span = top - bottom || Math.abs(top) || 1;
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  // Spaced by year, not by array position. Valaris is missing FY2021, and drawing
  // FY2020 (−24.42) adjacent to FY2022 (2.33) claims a recovery that skipped a
  // year nobody reported. The trailing point sits one year past the last close.
  const firstYear = years[0];
  const spanYears = Math.max(1, (points.length && points[points.length - 1].audited
    ? years[years.length - 1] : years[years.length - 1] + 1) - firstYear);
  const at1 = (p, i) => (p.audited ? years[i] : years[years.length - 1] + 1) - firstYear;
  const x = (i) => PAD.left + (at1(points[i], i) / spanYears) * plotW;
  const y = (v) => PAD.top + ((top - v) / span) * plotH;
  const at = (i) => `${x(i).toFixed(1)},${y(values[i]).toFixed(1)}`;

  const lastAudited = points[points.length - 1].audited ? points.length - 1 : points.length - 2;
  const line = points.slice(0, lastAudited + 1).map((_, i) => at(i)).join(" ");
  const tail = lastAudited < points.length - 1 ? `${at(lastAudited)} ${at(points.length - 1)}` : null;
  // the area between the line and zero is what a run of losses looks like from
  // across a room
  const area = `${line} ${x(lastAudited).toFixed(1)},${y(0).toFixed(1)} `
             + `${x(0).toFixed(1)},${y(0).toFixed(1)}`;

  // the dashed trailing point is not a fiscal year, so it is not one of the
  // "figures" the caption counts: six companies read "1 of 14 figures is a loss"
  // with the whole curve red while criterion 4 passed on the same series
  const audited = points.filter((p) => p.audited).map((p) => p.value);
  const losses = audited.filter((v) => v < 0).length;
  // one loss in thirteen years is not a failing record; the line turns red only
  // when the losses are the story
  const colour = losses > audited.length / 4 ? "var(--fail)" : "var(--accent)";
  // a label under every year is unreadable at thirteen of them; the ends always show
  const step = points.length > 9 ? 2 : 1;
  const shown = (i) => i === 0 || i === points.length - 1 || (points.length - 1 - i) % step === 0;

  return (
    <section className="eps-curve" aria-label="Earnings per share, by fiscal year">
      <div className="criteria-title">
        <div>
          <h3>Earnings per share</h3>
        </div>
      </div>
      {missingCaption}
      <svg className="eps-curve-svg" viewBox={`0 0 ${W} ${H}`} role="img"
           aria-label={points.map((p) => `${p.label} ${fmt(p.value, currency)}`).join(", ")}>
        <line x1={PAD.left} x2={W - PAD.right} y1={y(top)} y2={y(top)} className="eps-grid" />
        <text x={PAD.left - 8} y={y(top) + 4} className="eps-axis" textAnchor="end">{fmt(top, currency)}</text>
        {bottom < 0 && (
          <>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} className="eps-zero" />
            <text x={PAD.left - 8} y={y(0) + 4} className="eps-axis" textAnchor="end">0</text>
          </>
        )}
        <line x1={PAD.left} x2={W - PAD.right} y1={y(bottom)} y2={y(bottom)} className="eps-grid" />
        <text x={PAD.left - 8} y={y(bottom) + 4} className="eps-axis" textAnchor="end">{fmt(bottom, currency)}</text>

        <polygon points={area} fill={colour} opacity="0.07" />
        <polyline points={line} fill="none" stroke={colour} strokeWidth="2"
                  strokeLinejoin="round" strokeLinecap="round" />
        {tail && (
          <polyline points={tail} fill="none" stroke={colour} strokeWidth="2"
                    strokeDasharray="5 4" strokeLinecap="round" />
        )}
        {points.map((p, i) => (
          <g key={p.label}>
            <circle cx={x(i)} cy={y(p.value)} r={p.audited ? 3 : 4}
                    fill={p.audited ? "var(--bg)" : colour} stroke={colour} strokeWidth="2">
              <title>{`${p.label}  ${fmt(p.value, currency)}`}</title>
            </circle>
            {shown(i) && (
              <text x={x(i)} y={H - 6} className="eps-axis" textAnchor="middle">{p.short}</text>
            )}
          </g>
        ))}
      </svg>
    </section>
  );
}

function fmt(value, currency) {
  const symbol = ({ EUR: "€", GBP: "£", JPY: "¥" }[currency] ?? "$");
  return `${symbol}${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2,
                                                               maximumFractionDigits: 2 })}`;
}
