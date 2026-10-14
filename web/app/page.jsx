import Link from "next/link";
import PublicNav from "./PublicNav.jsx";
import { siteUrl } from "./site.js";

export const metadata = {
  description: "Research company fundamentals and screening results with filing evidence in Market Screener.",
  alternates: { canonical: siteUrl("/") },
};

export default function HomePage() {
  return <main className="public-page"><PublicNav />
    <header><div>
      <h1>Market Screener</h1>
      <p className="sub">Company fundamentals with filing evidence.</p>
    </div></header>
    <p>Explore company screening results and inspect the sources behind reported figures.</p>
    <Link href="/dashboard">Open dashboard</Link>
  </main>;
}
