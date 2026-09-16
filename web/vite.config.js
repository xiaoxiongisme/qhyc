import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/",
  server: {
    port: 5173,
    proxy: {
      // 开发模式代理到 FastAPI
      "/health": "http://localhost:8000",
      "/symbols": "http://localhost:8000",
      "/bars": "http://localhost:8000",
      "/ingest": "http://localhost:8000",
      "/anomalies": "http://localhost:8000",
      "/calendar": "http://localhost:8000",
      "/tasks": "http://localhost:8000",
      "/predict": "http://localhost:8000",
      "/backtest": "http://localhost:8000",
      "/dashboard": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
  },
});
