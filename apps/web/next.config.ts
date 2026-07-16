import type { NextConfig } from "next";

const apiProxyTarget = (process.env.MONITUBE_WEB_API_PROXY_TARGET?.trim() || "http://localhost:8000")
  .replace(/\/+$/, "");

const nextConfig: NextConfig = {
  transpilePackages: ["@monitube/contracts"],
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget}/:path*`,
      },
    ];
  },
};

export default nextConfig;
