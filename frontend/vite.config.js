import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址可用环境变量覆盖，便于「不改配置就跑第二套联调环境」：
//   VITE_API_TARGET=http://127.0.0.1:8096 npm run dev
const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:8080'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    // 显式绑定 IPv4：默认只监听 ::1（IPv6），会让 127.0.0.1 无法访问，
    // 且代理转发到 IPv4 的后端时可能失败
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    // 开发期把 /api 请求转发到后端，避免跨域
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // 产物交给 Caddy 托管，用相对路径更灵活
    assetsDir: 'assets',
  },
})
