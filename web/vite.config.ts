import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

/**
 * naive-ui 的完整依赖闭包（vueuc/vooks/treemate/evtd/css-render/date-fns/…）。
 * 这些包内部会反向 import naive-ui，若被分到 vendor 就产生 naive -> vendor -> naive
 * 的 circular chunk：构建告警之外，跨 chunk 的初始化顺序还不稳定，可能拿到 undefined 组件。
 * 用 package.json 递归解析而不是硬编码列表，naive-ui 升级时不会漏项。
 */
const EXCLUDE_CHUNK = new Set(['vue', 'vue-router', '@vue/shared', '@vue/runtime-core', '@vue/runtime-dom']);
// 相对本配置文件解析，避免从仓库根或其它目录调用 vite 时找不到 package.json
const NM = fileURLToPath(new URL('node_modules/', import.meta.url));
function depsOf(name: string): string[] {
  const seen = new Set<string>();
  const queue = [name];
  while (queue.length) {
    const pkg = queue.shift() as string;
    if (seen.has(pkg) || EXCLUDE_CHUNK.has(pkg)) continue;
    seen.add(pkg);
    try {
      const json = JSON.parse(readFileSync(`${NM}${pkg}/package.json`, 'utf8'));
      for (const dep of Object.keys(json.dependencies || {})) queue.push(dep);
    } catch {
      /* 可选依赖未安装：读不到就跳过 */
    }
  }
  return [...seen];
}
const NAIVE_PKGS = depsOf('naive-ui');

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
    // 阈值按"第三方库基线"设定：framework（vue+naive-ui 合并，约 870KB）/ echarts(~460KB)
    // / xterm(~330KB) 本身就在该量级且已按路由懒加载，设太低只会产生噪音；业务 chunk 普遍 < 40KB。
    chunkSizeWarningLimit: 900,
    reportCompressedSize: false,
    rollupOptions: {
      output: {
        // 按"变更频率"拆分：框架库长期不变可长期命中浏览器缓存。
        // naive-ui 与 vue 必须同 chunk：naive 依赖树（vueuc 等）内部 import vue，
        // 而 rollup 又会把共享符号提到 naive，两者分开必然形成 circular chunk
        // （naive -> vue -> naive），构建告警且初始化顺序不确定。
        manualChunks(id) {
          if (!id.includes('node_modules')) return;
          if (id.includes('/echarts/') || id.includes('/zrender/')) return 'echarts';
          if (id.includes('/@xterm/')) return 'xterm';
          const inNaiveTree = NAIVE_PKGS.some((p) => id.includes(`/node_modules/${p}/`));
          const inVue = id.includes('/vue/') || id.includes('/vue-router/') || id.includes('/@vue/');
          if (inNaiveTree || inVue) return 'framework';
          return 'vendor';
        },
      },
    },
  },
});
