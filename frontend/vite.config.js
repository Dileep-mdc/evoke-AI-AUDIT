import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Must match the ports start.ps1 launches, or `npm run dev` on its own proxies to a
    // backend that isn't there.
    port: 5174,
    proxy: {
      "/api": "http://127.0.0.1:8010",
    },
  },
});
