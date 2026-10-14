"use client";

import dynamic from "next/dynamic";

const Dashboard = dynamic(() => import("../src/Dashboard.jsx"), {
  ssr: false,
  loading: () => <div className="msg" role="status">Loading universe…</div>,
});

export default Dashboard;
