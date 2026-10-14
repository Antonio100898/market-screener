export function filingUrl(cik, accn) {
  if (/^E\d{5}$/.test(String(cik)) && /^S[0-9A-Z]{7}$/.test(String(accn)))
    return `https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/${accn}.pdf`;
  if (!cik || !accn || !/^\d{10}$/.test(String(cik))) return null;
  return `https://www.sec.gov/Archives/edgar/data/${Number(cik)}/${accn.replaceAll("-", "")}/${accn}-index.htm`;
}
