import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:3210' },
  },
  build: {
    target: 'es2020',
    // 生产环境不产出 sourcemap：避免把源码结构暴露给面板使用者
    sourcemap: false,
    // 阈值按"第三方库基线"设定：naive-ui(~670KB) / echarts(~460KB) / xterm(~330KB) 本身
    // 就在该量级且已按路由懒加载，设太低只会产生噪音；业务 chunk 普遍 < 40KB。
    chunkSizeWarningLimit: 700,
    reportCompressedSize: false,
    rollupOptions: {
      output: {
        // 按"变更频率"拆分：框架/UI 库长期不变可长期命中浏览器缓存
        manualChunks(id) {
          if (!id.includes('node_modules')) return;
          if (id.includes('/echarts/') || id.includes('/zrender/')) return 'echarts';
          if (id.includes('/naive-ui/') || id.includes('/css-render/') || id.includes('/seemly/') || id.includes('/date-fns/')) return 'naive';
          if (id.includes('/@xterm/')) return 'xterm';
          if (id.includes('/vue/') || id.includes('/vue-router/') || id.includes('/@vue/')) return 'vue';
          return 'vendor';
        },
      },
    },
  },
});
