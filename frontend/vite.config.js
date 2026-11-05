import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export default defineConfig(({ mode }) => {
  // 后端端口只认项目根目录 .env 的 APP_PORT，改 .env 即可，不用再动本文件。
  const env = loadEnv(mode, projectRoot, '')
  const port = env.APP_PORT || process.env.APP_PORT || '18730'
  const target = `http://127.0.0.1:${port}`

  return {
    envDir: projectRoot,
    plugins: [vue()],
    server: {
      port: 5173,
      proxy: {
        '/api': { target, changeOrigin: true },
        '/vendor': { target, changeOrigin: true },
      },
    },
  }
})
