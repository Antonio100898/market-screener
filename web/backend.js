export const backendOrigin = (process.env.SCREENER_API_ORIGIN || "http://127.0.0.1:8000").replace(/\/$/, "");

export const apiPaths = [
  "/dashboard.json", "/company", "/companies", "/fundamentals", "/screen", "/health",
  "/tracked", "/portfolio", "/portfolios", "/crypto", "/sync", "/config", "/api", "/research",
];
