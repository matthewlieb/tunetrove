import type { NextConfig } from "next";

/**
 * `/api/agent/*` is handled by `app/api/agent/[[...path]]/route.ts` (explicit proxy).
 * That returns JSON on upstream connection errors; rewrites alone could surface HTML 502s.
 *
 * Host-based redirects normalize `localhost:<port>` → `127.0.0.1:<port>` so Spotify API cookies
 * stay same-site. Do not add `middleware.ts` for this — Turbopack + Edge middleware has regressed
 * into broken routing (404 on `/`) in dev. These rules only match Host: localhost:* — never
 * production domains.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  async redirects() {
    const ports = [3003, 3000];
    return ports.map((port) => ({
      source: "/:path*",
      has: [{ type: "host" as const, value: `localhost:${port}` }],
      destination: `http://127.0.0.1:${port}/:path*`,
      permanent: false,
      basePath: false,
    }));
  },
};

export default nextConfig;
