import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The launcher and read APIs intentionally support local access only.
  poweredByHeader: false,
};

export default nextConfig;
