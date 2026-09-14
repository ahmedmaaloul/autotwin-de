import path from "node:path";
import type { NextConfig } from "next";

/**
 * AutoTwin DE — web application configuration.
 *
 * `turbopack.root` is pinned to this package so Turbopack does not walk up past the
 * repository and pick up an unrelated lockfile from the developer's home directory.
 * `output: "standalone"` is what `infra/docker/web.Dockerfile` copies into the runtime image.
 */
const nextConfig: NextConfig = {
  reactCompiler: true,
  output: "standalone",
  outputFileTracingRoot: path.join(import.meta.dirname, "../../"),
  turbopack: {
    root: path.join(import.meta.dirname, "../../"),
  },
  async rewrites() {
    // Browser-side calls go to /api/backend/* and are proxied server-side to FastAPI.
    // Keeps the API origin out of the client bundle and avoids CORS in development.
    const target = process.env.AUTOTWIN_API_URL ?? "http://localhost:8000";
    return [
      { source: "/api/backend/:path*", destination: `${target}/api/v1/:path*` },
      // /health, /ready and /metrics deliberately live outside the versioned API — they are
      // operational endpoints, not product surface, so they get their own passthrough.
      { source: "/api/backend-root/:path*", destination: `${target}/:path*` },
    ];
  },
};

export default nextConfig;
