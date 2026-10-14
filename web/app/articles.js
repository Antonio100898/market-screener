export const sections = { blog: "Blog", guides: "Guides", product: "Product" };

export const articles = [
  {
    section: "blog", slug: "why-filing-evidence-matters", title: "Why a financial figure needs filing evidence",
    description: "A local draft on reported facts, calculated results and the evidence needed to tell them apart.",
    updated: "2026-10-04", decisions: "P-02, P-04, P-09",
    sources: ["product/shared-data.md — Meaning of official and calculated; Historical change history"],
    paragraphs: [
      "A reported figure and a calculated ratio answer different questions. A reported figure comes from a company disclosure. A ratio combines inputs under a calculation definition. The source of an input must remain visible after the ratio is calculated.",
      "A useful source record identifies the filing, the reporting period, the unit and the exact tag or source row. A number without that context may belong to a different period or a different part of the business.",
      "Historical figures can change. A later report may revise an earlier comparative period. A history should retain the old and replacement values, both sources and the dates when the change was detected and activated. If the company gives no reason, the reason remains unknown.",
      "Missing evidence is not a measured zero. A zero assumption belongs in a separate disclosed view. It must not silently replace the reported record.",
    ],
  },
  {
    section: "guides", slug: "read-a-figure-source", title: "How to read a figure's source",
    description: "A local guide draft for checking a reported number's period, unit and filing location.",
    updated: "2026-10-04", decisions: "P-02, P-04, P-09",
    sources: ["product/shared-data.md — Meaning of official and calculated; Acceptance checks", "AGENTS.md — Every extracted figure carries provenance"],
    paragraphs: [
      "Start with the period end. Check whether the figure describes one reporting date or a span of time. Compare values only when their periods and reporting scope match.",
      "Check the unit and currency. Shares, amounts of money and per-share amounts are different measures. A market quote can use a different currency from the statements; conversion requires a disclosed exchange rate.",
      "Open the source filing. Use the accession or document identity and the exact tag or source row to find the reported figure. When a total combines several inputs, inspect each component's source.",
      "Read the missing-data and assumption notes. An unavailable input should remain unavailable in the reported view. A calculated estimate should state its inputs and assumptions.",
      "A publication date tells you when this system published a release. It does not, by itself, prove the source has been checked for later changes.",
    ],
  },
  {
    section: "product", slug: "fundamentals-workflow", title: "Research fundamentals with their sources",
    description: "A local product draft describing the screening and evidence workflow, with planned capabilities clearly identified.",
    updated: "2026-10-04", decisions: "P-01, P-02, P-06, P-07, P-10, P-12, P-17",
    sources: ["product.md — Accepted decisions", "product/personal-research.md — Required outcomes", "product/compatibility.md — Frontend choice"],
    paragraphs: [
      "Market Screener connects company screening results with filing evidence. Open the dashboard to search the screening universe, inspect a company and review the disclosed sources and assumptions.",
      "Graham screening is one existing calculation set. It does not define the product boundary. Reported company figures and shared calculated fields belong to the shared data service.",
      "Private research is separate from shared official data. The product plan requires persistent private workspaces and an agent that can help research companies and edit a user's calculations and columns. These planned capabilities are not a promise that the complete flow is already available.",
    ],
  },
];

export const findArticle = (section, slug) => articles.find((article) => article.section === section && article.slug === slug);
