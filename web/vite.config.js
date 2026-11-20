import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const adminPath = env.MELDFABRIC_ADMIN_PATH || "/admin";
  return {
    plugins: [
      vue(),
      {
        name: "meldfabric-development-config",
        transformIndexHtml(html) {
          return command === "serve"
            ? html
                .replaceAll("__MELDFABRIC_ADMIN_BASE__", adminPath)
                .replaceAll("__MELDFABRIC_ROOT_PATH__", "")
            : html;
        },
      },
    ],
    // Python substitutes this deployment prefix in index.html; chunks use relative imports.
    base: command === "build" ? "/__MELDFABRIC_ADMIN_STATIC__/" : "/",
    build: {
      outDir: "../src/meldfabric/management/static",
      emptyOutDir: true,
    },
    server: {
      host: "127.0.0.1",
      proxy: {
        [adminPath]: {
          target: env.MELDFABRIC_API_TARGET || "http://127.0.0.1:8000",
          changeOrigin: true,
          bypass(req) {
            if (
              req.url === adminPath ||
              req.url === `${adminPath}/` ||
              req.url.startsWith(`${adminPath}/ui`)
            )
              return "/index.html";
          },
        },
        "/ready": env.MELDFABRIC_API_TARGET || "http://127.0.0.1:8000",
        "/docs": env.MELDFABRIC_API_TARGET || "http://127.0.0.1:8000",
        "/openapi.json": env.MELDFABRIC_API_TARGET || "http://127.0.0.1:8000",
      },
    },
  };
});
