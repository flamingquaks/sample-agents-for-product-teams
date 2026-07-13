import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is a static bundle uploaded to S3 and served via CloudFront (Phase 5).
// Runtime config (API URL, Cognito ids) is loaded from /config.json at startup
// rather than baked in, so one build artifact deploys to any stack. See
// src/config.ts. `base: "./"` keeps asset URLs relative so the bundle works
// under any CloudFront path.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: { port: 5173 },
  build: { outDir: "dist", sourcemap: true },
});
