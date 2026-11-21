import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { existsSync } from "node:fs";
import { spawn, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";

let backendProcess: ChildProcess | undefined;

const assistantBackend = {
  name: "assistant-backend",
  async configureServer() {
    try {
      const health = await fetch("http://127.0.0.1:8000/api/health");
      if (health.ok) return;
    } catch {
      // The backend is not running yet.
    }

    const repoRoot = resolve(import.meta.dirname, "..");
    const virtualEnvPython = resolve(repoRoot, "venv", "Scripts", "python.exe");
    const python = existsSync(virtualEnvPython) ? virtualEnvPython : "python";

    backendProcess = spawn(
      python,
      [resolve(repoRoot, "assistant_fast.py"), "--ui", "--host", "127.0.0.1", "--port", "8000"],
      { cwd: repoRoot, stdio: "inherit", windowsHide: false },
    );

    const stopBackend = () => {
      if (backendProcess && !backendProcess.killed) backendProcess.kill();
    };
    process.once("exit", stopBackend);
  },
};

export default defineConfig({
  plugins: [react(), assistantBackend],
  server: {
    host: "127.0.0.1",
    allowedHosts: true,
    strictPort: false,
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  preview: { host: "127.0.0.1", port: 4173 },
});
