import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Python webui_server serves the built files out of webui/dist/ and
// proxies /api/* to the band server. In dev we hit the host's webui port
// directly so the real backend drives the UI.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // keep the committed dist small and reviewable
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5733,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8783",
        changeOrigin: true,
      },
    },
  },
});
