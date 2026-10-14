import { notFound } from "next/navigation";
import { articles, findArticle } from "../../articles.js";
import { draftMetadata } from "../../site.js";
import PublicNav from "../../PublicNav.jsx";

export function generateStaticParams({ params }) {
  return articles.filter((article) => article.section === params.section).map(({ slug }) => ({ slug }));
}

export async function generateMetadata({ params }) {
  const { section, slug } = await params;
  const article = findArticle(section, slug);
  if (!article) notFound();
  return draftMetadata(article.title, article.description, `/${section}/${slug}`);
}

export default async function ArticlePage({ params }) {
  const { section, slug } = await params;
  const article = findArticle(section, slug);
  if (!article) notFound();
  return <main className="public-page"><PublicNav /><article>
    <p className="notice">Local draft. Publication has not been approved.</p>
    <h1>{article.title}</h1>
    <p>Authorship: local project draft based on approved product decisions.<br />
      Updated <time dateTime={article.updated}>{article.updated}</time>.</p>
    {article.paragraphs.map((paragraph, index) => <p key={index}>{paragraph}</p>)}
    <footer><h2>Sources</h2>
      <p>Market Screener product decisions: {article.decisions}.</p>
      <ul>{article.sources.map((source) => <li key={source}>{source}</li>)}</ul>
      <a href="/guides/read-a-figure-source">Read the source-checking guide</a>
    </footer>
  </article></main>;
}
