"use client";

import dynamic from "next/dynamic";

const ApiDashboard = dynamic(() => import("../../../src/ApiDashboard.jsx"), {
  ssr: false,
  loading: () => <p className="msg" role="status">Loading screening preview…</p>,
});

export default ApiDashboard;
