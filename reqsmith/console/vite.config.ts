import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/console/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/reviewer": "http://localhost:8000",
      "/runs": "http://localhost:8000",
      "/webhooks": "http://localhost:8000",
    },
  },
});
