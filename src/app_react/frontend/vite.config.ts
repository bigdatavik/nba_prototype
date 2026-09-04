import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// FastAPI serves the build from frontend/dist (see backend/main.py). In dev,
// proxy /api to the local uvicorn backend on :8000.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
