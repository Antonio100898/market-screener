import { notFound } from "next/navigation";
import { articles, sections } from "../articles.js";
import { draftMetadata } from "../site.js";
import PublicNav from "../PublicNav.jsx";

export function generateStaticParams() {
  return Object.keys(sections).map((section) => ({ section }));
}

export async function generateMetadata({ params }) {
  const { section } = await params;
  if (!Object.hasOwn(sections, section)) notFound();
  return draftMetadata(sections[section], "Local editorial drafts for Market Screener.", `/${section}`);
}

export default async function SectionPage({ params }) {
  const { section } = await params;
  if (!Object.hasOwn(sections, section)) notFound();
  return <main className="public-page"><PublicNav />
    <h1>{sections[section]}</h1>
    <p className="notice">Local drafts. Publication has not been approved.</p>
    <ul>{articles.filter((article) => article.section === section).map((article) =>
      <li key={article.slug}><a href={`/${section}/${article.slug}`}>{article.title}</a></li>)}</ul>
  </main>;
}
