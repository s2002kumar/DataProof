// The candidate's frontend is built offline inside an isolated container (ADR-0004): nothing may
// be fetched at build time. No remote fonts, no remote image optimization, and every page renders
// on request, so static generation never calls the API.

/** @type {import("next").NextConfig} */
const nextConfig = {
  images: { unoptimized: true },
  poweredByHeader: false,
};

export default nextConfig;
