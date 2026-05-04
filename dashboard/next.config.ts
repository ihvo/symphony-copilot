import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: {
    unoptimized: true,
  },
};

// Dev-only proxy — rewrites are ignored during static export but
// useful when running `next dev` against a local FastAPI instance.
if (process.env.NODE_ENV === "development") {
  (nextConfig as Record<string, unknown>).rewrites = async () => [
    {
      source: "/api/:path*",
      destination: "http://127.0.0.1:8080/api/:path*",
    },
  ];
}

export default nextConfig;
