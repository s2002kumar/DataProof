// The dashboard and the control plane API share one origin, so the session cookie and the API's
// Origin and CSRF checks work unchanged. In development Next.js forwards /api and /auth to the
// API; a production reverse proxy may route them directly instead.

const api = process.env.DATAPROOF_API_ORIGIN ?? "http://127.0.0.1:8000";

const securityHeaders = [
  // Pages render pull-request-supplied text (binding SQL, check IDs), always as text. These
  // headers limit what an injection could do if one were ever introduced.
  {
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
      "script-src 'self' 'unsafe-inline'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data:",
      "connect-src 'self'",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
    ].join("; "),
  },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Frame-Options", value: "DENY" },
];

/** @type {import("next").NextConfig} */
const nextConfig = {
  poweredByHeader: false,
  images: { unoptimized: true },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${api}/api/:path*` },
      { source: "/auth/:path*", destination: `${api}/auth/:path*` },
    ];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
