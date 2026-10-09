/**
 * 临时探针（不入库，验证后删除）：在真实浏览器里挂载 ui.ts 的 NButton 包装，
 * 复现备份页操作列那三个「只有图标」的按钮，并读取图标的实际渲染尺寸/颜色，
 * 用来区分「代码没修好」与「浏览器跑的是旧 chunk」。
 */
import { createApp, h } from 'vue';
import { NIcon } from 'naive-ui';
import { NButton } from './ui';
import { icons } from './icons';

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

const cases = [
  ['run', h(NButton, { size: 'tiny', tertiary: true, type: 'primary', icon: ico('PlayOutline') }, { default: () => '立即备份' })],
  ['edit', h(NButton, { size: 'tiny', tertiary: true, icon: ico('PencilOutline'), title: '编辑备份任务' })],
  ['restore', h(NButton, { size: 'tiny', tertiary: true, type: 'warning', icon: ico('RefreshCircleOutline'), title: '从备份恢复' })],
  ['del', h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline'), title: '删除备份任务' }, { default: () => '' })],
];

createApp({
  render: () =>
    h('div', { id: 'probe', style: 'display:flex;gap:6px;align-items:center;padding:24px' },
      cases.map(([key, vnode]) => h('span', { 'data-case': key, style: 'display:inline-flex' }, [vnode])),
    ),
}).mount('#app');
