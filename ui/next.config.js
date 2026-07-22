const nextConfig = {
  devIndicators: false,
  distDir: process.env.TEAMFLOW_UI_DIST_DIR || ".next",
  poweredByHeader: false
};

module.exports = nextConfig;
