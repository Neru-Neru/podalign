import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Works both at / during local preview and /podalign/ on GitHub Pages.
  base: process.env.VITE_BASE_PATH || "/",
});
