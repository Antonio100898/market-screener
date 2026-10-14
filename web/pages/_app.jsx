import "../src/styles.css";
import "../public/public.css";

export default function PublicApp({ Component, pageProps }) {
  return <Component {...pageProps} />;
}
