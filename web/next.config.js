import { apiPaths, backendOrigin } from "./backend.js";

export default {
  agentRules: false,
  experimental: { cpus: 1 },
  async rewrites() {
    return apiPaths.map((path) => ({
      source: path === "/dashboard.json" ? path : `${path}/:path*`,
      destination: `${backendOrigin}${path}${path === "/dashboard.json" ? "" : "/:path*"}`,
    }));
  },
};
