/** @type {import('next').NextConfig} */
const nextConfig = {
  // Same-origin API proxy: the browser only ever talks to the Next
  // server, so CORS, host aliases (localhost vs 127.0.0.1) and
  // per-host proxy quirks can't break API calls. Set
  // NEXT_PUBLIC_NEXUS_API to reach a backend directly instead.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8001/:path*",
      },
    ];
  },
};

export default nextConfig;
