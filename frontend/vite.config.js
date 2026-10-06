import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// For local development (`make ui-dev`) the dev server proxies API calls to
// the FastAPI bridge started by `make run` on port 8501.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8501",
    },
  },
});
