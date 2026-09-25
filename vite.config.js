import { defineConfig } from "vite";

// BASE_PATH is set by the GitHub Pages workflow (the site is served from /<repo>/).
export default defineConfig({
  base: process.env.BASE_PATH || "/",
});
