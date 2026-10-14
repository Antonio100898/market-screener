const COLUMNS = [
  ["eps", "Reported EPS"], ["weighted_shares", "Reported weighted shares"],
  ["net_income", "Reported net income"], ["operating_cash_flow", "Operating cash flow"],
  ["depreciation_and_amortisation", "D&A"], ["capital_expenditure", "Cash CapEx"],
  ["intangible_investment", "Intangible purchases"], ["free_cash_flow", "FCF (derived)"],
];

export default function PriorAccountingHistory({ history, formatMoney, formatEps, renderSource }) {
  if (!Array.isArray(history) || !history.length) return null;
  return <section className="annual-history" aria-label="Prior accounting history">
    <div className="criteria-title"><div><h3>Earlier accounting history</h3>
      <p>Separate captured reported bases. Missing years remain gaps; overlapping years are not equivalent or restated onto the current basis.</p>
    </div></div>
    {history.map((group, index) => {
      const series = group.series ?? {};
      const years = [...new Set(Object.values(series).flatMap((values) => Object.keys(values)))]
        .map(Number).filter(Number.isFinite).sort((a, b) => b - a);
      const columns = COLUMNS.filter(([key]) => Object.keys(series[key] ?? {}).length);
      return <div key={`${group.basis}:${group.currency}:${index}`}>
        <h4>{group.basis} · {group.currency} · historical</h4>
        <p>Current tables and calculations use {group.current_basis}. {group.share_basis}</p>
        <p>FCF here is operating cash flow less cash CapEx on this historical basis. No current-price ratios use these figures.</p>
        {group.unavailable_reason && <p role="status">{group.unavailable_reason}</p>}
        {!!years.length && <div className="annual-history-scroll"><table className="annual-history-table">
          <thead><tr><th>Fiscal year</th>{columns.map(([key, label]) => <th key={key} className="num">{label}</th>)}</tr></thead>
          <tbody>{years.map((year) => <tr key={year}><td><b>FY{year}</b></td>
            {columns.map(([key]) => {
              const point = series[key]?.[year];
              const sources = point?.source?.components?.length ? point.source.components
                : point?.source ? [point.source] : [];
              const knownSymbol = ["USD", "EUR", "GBP", "JPY"].includes(group.currency);
              return <td key={key} className="num">
                {point?.value == null ? "—" : key === "weighted_shares" ? Number(point.value).toLocaleString()
                  : !knownSymbol ? `${group.currency} ${Number(point.value).toLocaleString()}`
                    : key === "eps" ? formatEps(point.value, group.currency)
                    : formatMoney(point.value, group.currency)}
                {sources.map((source, sourceIndex) => <small className="accn" key={sourceIndex}>
                  <span title={source.tag}>{source.tag}</span><br />{renderSource(source)}
                </small>)}
              </td>;
            })}
          </tr>)}</tbody>
        </table></div>}
      </div>;
    })}
  </section>;
}
