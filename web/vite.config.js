import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { apiPaths, backendOrigin } from "./backend.js";

// Built straight into the API package so one uvicorn process serves everything.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../api/screener/static/ui", emptyOutDir: true },
  server: {
    proxy: Object.fromEntries(apiPaths.map((path) => [path, backendOrigin])),
  },
});
