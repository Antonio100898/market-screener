import assert from "node:assert/strict";
import test from "node:test";
import { createServer as createHttpServer } from "node:http";
import { createServer as createViteServer } from "vite";

import config from "../vite.config.js";
import nextConfig from "../next.config.js";


test("development server proxies detail, persistent and sync API routes to FastAPI", () => {
  const proxy = config.server.proxy;
  for (const route of ["/company", "/tracked", "/portfolio", "/portfolios", "/crypto", "/sync", "/config", "/research"])
    assert.equal(proxy[route], "http://127.0.0.1:8000", `${route} must not hit Vite's SPA fallback`);
});

test("Next routes data reads and persistent writes to the same FastAPI owner", async () => {
  const rewrites = await nextConfig.rewrites();
  for (const route of ["/company", "/companies", "/tracked", "/portfolio", "/portfolios", "/crypto", "/sync", "/config", "/api", "/research"])
    assert.ok(rewrites.some((rewrite) => rewrite.source === `${route}/:path*`
      && rewrite.destination === `http://127.0.0.1:8000${route}/:path*`),
    `${route} must not return frontend HTML or bypass FastAPI`);
  assert.ok(rewrites.some((rewrite) => rewrite.source === "/dashboard.json"
    && rewrite.destination === "http://127.0.0.1:8000/dashboard.json"));
});

test("research proxy preserves private HTTP denials for reads and writes", { timeout: 10_000 }, async () => {
  const observed = [];
  const backend = createHttpServer((request, response) => {
    observed.push({ method: request.method, path: request.url, authorization: request.headers.authorization });
    response.writeHead(401, { "Content-Type": "application/json", "Cache-Control": "private, no-store" });
    response.end(JSON.stringify({ detail: "verified sign-in is required" }));
  });
  let frontend;
  try {
    await new Promise((resolve) => backend.listen(0, "127.0.0.1", resolve));
    const target = `http://127.0.0.1:${backend.address().port}`;
    frontend = await createViteServer({ configFile: false, plugins: [], server: {
      host: "127.0.0.1", port: 0, hmr: false, watch: null,
      proxy: Object.fromEntries(Object.keys(config.server.proxy).map((path) => [path, target])),
    } });
    await frontend.listen();
    const origin = `http://127.0.0.1:${frontend.httpServer.address().port}`;
    for (const method of ["GET", "PUT"]) {
      const response = await fetch(`${origin}/research/workspaces/default`, {
        method, headers: { Authorization: "Bearer proxy-fixture" },
      });
      assert.equal(response.status, 401);
      assert.equal(response.headers.get("cache-control"), "private, no-store");
      assert.deepEqual(await response.json(), { detail: "verified sign-in is required" });
    }
    assert.deepEqual(observed, [
      { method: "GET", path: "/research/workspaces/default", authorization: "Bearer proxy-fixture" },
      { method: "PUT", path: "/research/workspaces/default", authorization: "Bearer proxy-fixture" },
    ]);
  } finally {
    if (frontend) await frontend.close();
    await new Promise((resolve) => backend.close(resolve));
  }
});
