import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import topLevelAwait from "vite-plugin-top-level-await";
import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    port: 10000,        // the port to run on
    host: '0.0.0.0',   // bind to all network interfaces (accessible from other devices)
    strictPort: true,  // optional: fail if port is already in use instead of trying the next one
  }
})

const __dirname = dirname(fileURLToPath(import.meta.url));

export default {
    build: {
        target: "chrome51",
        rollupOptions: {
            input: {
                main: resolve(__dirname, "index.html"),
                offlinePWA: resolve(__dirname, "offline-pwa.html"),
                privacy: resolve(__dirname, "privacy.html"),
            },
        }
    },
    plugins: [
        topLevelAwait()
    ]
}
