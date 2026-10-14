export default function CompanyDirectory({ result }) {
  const query = result.next_cursor ? new URLSearchParams({
    cursor: result.next_cursor, release: String(result.release.release_id),
  }) : null;
  return <><h1>Company directory</h1>
    <p className="notice">Published snapshots. These figures have not been verified against later source changes.</p>
    <p>Data release {result.release.release_id}. {result.matched} companies in this release.</p>
    <ul>{result.rows.map((row) => <li key={row.security_id}>
      <a href={`/stocks/${encodeURIComponent(row.ticker)}`}>{row.name} ({row.ticker})</a>
    </li>)}</ul>
    {query && <a href={`/stocks?${query}`}>Next companies in this release</a>}
  </>;
}
