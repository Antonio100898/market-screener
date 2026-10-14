import { fmtMultiple } from "./CompanyTable.jsx";

export default function MarketReference({ overview: sp500 }) {
  return (
      <section className="index-valuation" aria-labelledby="sp500-valuation-title">
        <div className="index-valuation-intro">
          <span className="index-valuation-kicker">Market reference</span>
          <h2 id="sp500-valuation-title">S&amp;P 500 valuation</h2>
          <p>
            {sp500.members.toLocaleString()} index members in this dashboard · full cohort,
            unaffected by the filters below
          </p>
        </div>
        <div className="index-valuation-metric">
          <span>Median P/E</span>
          <strong>{fmtMultiple(sp500.pe.median)}</strong>
          <small>{sp500.pe.count.toLocaleString()} companies with a valid positive TTM P/E</small>
        </div>
        <div className="index-valuation-metric">
          <span>Median P/E3</span>
          <strong>{fmtMultiple(sp500.pe3.median)}</strong>
          <small>{sp500.pe3.count.toLocaleString()} companies · price / 3-year average EPS</small>
        </div>
        <p className="index-valuation-note">
          Missing and non-positive multiples are excluded from each median, never counted as zero.
        </p>
      </section>

  );
}
