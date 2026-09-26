import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { environment: "jsdom" },
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
});
