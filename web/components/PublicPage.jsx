import Head from "next/head";
import PublicNav from "../app/PublicNav.jsx";

export default function PublicPage({ title, description, canonical, children }) {
  return <>
    <Head>
      <title>{`${title} | Market Screener`}</title>
      <meta name="description" content={description} />
      <meta name="robots" content="noindex, nofollow" />
      <link rel="canonical" href={canonical} />
    </Head>
    <main className="public-page"><PublicNav />{children}</main>
  </>;
}

export function UnavailableContent({ status, message }) {
  return <><h1>{status === 404 ? "Company not found" : "Company data unavailable"}</h1>
    <p role="status">{message}</p><p><a href="/stocks">Return to company directory</a></p></>;
}
