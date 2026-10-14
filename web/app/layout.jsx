import "../src/styles.css";
import "../public/public.css";

export const metadata = { title: "Market Screener" };

export default function RootLayout({ children }) {
  return <html lang="en"><body>{children}</body></html>;
}
