// Rewrites are frozen by `next build`, including in standalone output.
const internalApiUrl = (process.env.INTERNAL_API_URL || "http://localhost:8000").replace(/\/+$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${internalApiUrl}/api/:path*`,
      },
      {
        source: "/health/:path*",
        destination: `${internalApiUrl}/health/:path*`,
      },
    ];
  },
};

export default nextConfig;
