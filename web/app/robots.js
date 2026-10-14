import { siteUrl } from "./site.js";

export default function robots() {
  return {
    rules: { userAgent: "*", allow: "/", disallow: ["/dashboard", "/research", "/api", "/tracked", "/portfolio", "/portfolios", "/config", "/sync", "/stocks", "/blog", "/guides", "/product"] },
    sitemap: siteUrl("/sitemap.xml"),
  };
}
