import BrowserApiDashboard from "./api/BrowserApiDashboard.jsx";

export const metadata = {
  title: "Dashboard | Market Screener",
  robots: { index: false, follow: false },
};

export default function DashboardPage() {
  return <BrowserApiDashboard />;
}
