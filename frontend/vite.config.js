import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Ports are read from ../dev.config.json rather than written here, because they also have to
// be right in start.ps1. When both files carried their own copy they drifted, and the symptom
// was not an error -- the dashboard came up and proxied /api to a port with nothing on it.
// Environment variables still win, so a one-off port change needs no file edit.
const devConfig = JSON.parse(
  readFileSync(fileURLToPath(new URL("../dev.config.json", import.meta.url)), "utf8"),
);

const backendHost = process.env.BACKEND_HOST || devConfig.backendHost;
const backendPort = Number(process.env.BACKEND_PORT || devConfig.backendPort);
const frontendPort = Number(process.env.FRONTEND_PORT || devConfig.frontendPort);

export default defineConfig({
  plugins: [react()],
  server: {
    port: frontendPort,
    proxy: {
      "/api": `http://${backendHost}:${backendPort}`,
    },
  },
});
