<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, h } from 'vue';
import { useRouter } from 'vue-router';
import { NCard, NGrid, NGridItem, NSpace, NText, NIcon, NProgress, NTag, NButton, NDropdown, NSkeleton } from 'naive-ui';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { api, getRole } from '../api';
import { icons } from '../icons';
import { cssVar } from '../theme';
import EmptyBox from '../components/EmptyBox.vue';

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

const router = useRouter();
const isAdmin = getRole() === 'admin';
const info = ref<any>({});
const stat = ref<any>({});
const apps = ref<any[]>([]);
const failedUnits = ref<string[]>([]);
const sideError = ref('');
const loading = ref(true);
const cpuEl = ref<HTMLElement | null>(null);
const memEl = ref<HTMLElement | null>(null);
let cpuChart: echarts.ECharts | null = null;
let memChart: echarts.ECharts | null = null;
let timer: number | null = null;
let ro: ResizeObserver | null = null;
let mo: MutationObserver | null = null;

function fmtBytes(n: number) {
  if (n == null || Number.isNaN(n)) return '-';
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function fmtUptime(s: number) {
  if (!s) return '-';
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}天${hh}小时` : `${hh}小时${m}分`;
}

const runningApps = computed(() => apps.value.filter((a) => a.running).length);
const hour = new Date().getHours();
const greet = hour < 6 ? '夜深了' : hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好';

function axisColor() {
  return {
    label: cssVar('--cp-text-dim', '#8a90a0'),
    line: cssVar('--cp-border-strong', '#2c2c33'),
    split: cssVar('--cp-line', '#eef1f7'),
  };
}

function chartOpts(times: string[], series: any[], yfmt: (v: number) => string) {
  const c = axisColor();
  return {
    grid: { left: 8, right: 14, top: 14, bottom: 4, containLabel: true },
    xAxis: {
      type: 'category', data: times, boundaryGap: false,
      axisLine: { lineStyle: { color: c.line } },
      axisLabel: { color: c.label, fontSize: 11.5, showMaxLabel: true, hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: c.split } },
      axisLabel: { color: c.label, fontSize: 11.5, formatter: yfmt },
    },
    series,
    tooltip: { trigger: 'axis' },
    animation: false,
  };
}

function rgbaFromVar(name: string, fallback: string, alpha: number) {
  const hex = cssVar(name, fallback).replace('#', '');
  const full = hex.length === 3 ? hex.split('').map((x) => x + x).join('') : hex;
  const r = parseInt(full.slice(0, 2), 16);
  const g = parseInt(full.slice(2, 4), 16);
  const b = parseInt(full.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function areaGradient(varName: string, fallback: string) {
  return {
    type: 'linear' as const, x: 0, y: 0, x2: 0, y2: 1,
    colorStops: [
      { offset: 0, color: rgbaFromVar(varName, fallback, 0.32) },
      { offset: 1, color: rgbaFromVar(varName, fallback, 0) },
    ],
  };
}

function renderCharts() {
  const hist: any[] = (stat.value.history || []).slice(-60);
  const times = hist.map((x) => new Date(x.t).toLocaleTimeString());
  cpuChart?.setOption(
    chartOpts(times, [{
      name: 'CPU', type: 'line', smooth: 0.4, showSymbol: false,
      data: hist.map((x) => x.cpu),
      lineStyle: { width: 2, color: cssVar('--cp-brand', '#4f7cff') },
      areaStyle: { color: areaGradient('--cp-brand', '#4f7cff') },
    }], (v: number) => v + '%'),
    { notMerge: true },
  );
  memChart?.setOption(
    chartOpts(times, [{
      name: '内存', type: 'line', smooth: 0.4, showSymbol: false,
      data: hist.map((x) => (x.memUsed || 0) / 1048576),
      lineStyle: { width: 2, color: cssVar('--cp-ok', '#34c77b') },
      areaStyle: { color: areaGradient('--cp-ok', '#34c77b') },
    }], (v: number) => (v / 1024).toFixed(1) + 'G'),
    { notMerge: true },
  );
}

async function tick() {
  try {
    stat.value = await api.stats();
    renderCharts();
  } catch {
    return; // 401 时 api 层已跳转登录；网络抖动等下个周期自动恢复
  } finally {
    loading.value = false;
  }
}

async function loadSide() {
  try {
    apps.value = await api.apps();
    const svc = await api.services();
    failedUnits.value = svc.filter((s: any) => s.active === 'failed').map((s: any) => s.unit);
    sideError.value = '';
  } catch (e: any) {
    // 拉不到就必须显式说"未知"：failedUnits 留空会被下面渲染成绿色"服务正常"，
    // 等于把 /system/services 的 500 或超时读成一切正常。
    failedUnits.value = [];
    sideError.value = e?.message || '状态获取失败';
  }
}

const quickOpts = [
  { label: '部署新应用', key: 'deploy', icon: 'AddOutline', to: '/apps' },
  { label: '打开终端', key: 'term', icon: 'TerminalOutline', to: '/terminal', disabled: !isAdmin },
  { label: '查看备份', key: 'bk', icon: 'TimeOutline', to: '/backups' },
];
const quickMenu = quickOpts.map((o) => ({
  label: o.label,
  key: o.key,
  disabled: o.disabled,
  icon: () => h(NIcon, { component: icons[o.icon], size: 16 }),
  onClick: () => router.push(o.to),
}));

onMounted(async () => {
  try {
    info.value = await api.info();
  } catch { /* 忽略：降级为未知主机信息 */ }
  await nextTick();
  if (cpuEl.value) cpuChart = echarts.init(cpuEl.value);
  if (memEl.value) memChart = echarts.init(memEl.value);
  await tick();
  loadSide();
  // 应用/服务状态原本只在挂载时取一次，页面开着就越看越旧；
  // 每 5 个统计周期（15s）随 tick 一起刷，也让 loadSide 失败后的"状态未知"能自动恢复。
  let sideTicks = 0;
  timer = window.setInterval(() => {
    tick();
    if (++sideTicks % 5 === 0) loadSide();
  }, 3000);

  // 容器尺寸变化（含侧栏折叠）自动重绘
  if (window.ResizeObserver && cpuEl.value) {
    ro = new ResizeObserver(() => {
      cpuChart?.resize();
      memChart?.resize();
    });
    ro.observe(cpuEl.value);
  }
  // 主题切换后重绘坐标轴颜色
  mo = new MutationObserver(() => renderCharts());
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
});

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize);
  if (timer) clearInterval(timer);
  ro?.disconnect();
  mo?.disconnect();
  cpuChart?.dispose();
  memChart?.dispose();
});

function onResize() { cpuChart?.resize(); memChart?.resize(); }
window.addEventListener('resize', onResize);
</script>

<template>
  <NSpace vertical :size="16">
    <!-- hero -->
    <div class="hero">
      <div style="min-width: 0">
        <div class="hero-greet">{{ greet }}，欢迎回到 <b>choyeon panel</b></div>
        <div class="hero-host">
          <NIcon :component="icons.ServerOutline" :size="15" color="var(--cp-brand-soft)" />
          <span class="hero-host-text">{{ info.hostname || '本机' }} · {{ info.prettyName || info.platform || '加载中' }}</span>
        </div>
      </div>
      <NSpace align="center" :size="8" :wrap="false">
        <NTag v-if="info.node" round size="small" :bordered="false" type="info">{{ info.node }}</NTag>
        <span v-if="sideError" class="st warn"><span class="dot warn"></span>服务状态未知</span>
        <span v-else-if="!failedUnits.length" class="st ok"><span class="dot ok"></span>服务正常</span>
        <span v-else class="st err"><span class="dot err"></span>{{ failedUnits.length }} 个服务异常</span>
        <NDropdown trigger="click" :options="quickMenu">
          <NButton type="primary" round size="small" class="cp-press">快捷操作</NButton>
        </NDropdown>
      </NSpace>
    </div>

    <!-- stat cards -->
    <NGrid :cols="4" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem v-for="(c, ci) in [
        { key: 'cpu', label: 'CPU', icon: 'PulseOutline', color: 'var(--cp-brand)', value: `${stat.cpu ?? '-'}`, unit: '%', pct: stat.cpu || 0, foot: `${info.cpuCores || '?'} 核 · 负载 ${(stat.load || []).map((x: number) => x.toFixed(1)).join(' / ') || '-'}` },
        { key: 'mem', label: '内存', icon: 'CubeOutline', color: 'var(--cp-ok)', value: fmtBytes(stat.memUsed), unit: ` / ${fmtBytes(stat.memTotal)}`, pct: Math.round(((stat.memUsed || 0) / (stat.memTotal || 1)) * 100), foot: `可用 ${fmtBytes((stat.memTotal || 0) - (stat.memUsed || 0))}` },
        { key: 'disk', label: '磁盘 /', icon: 'DocumentTextOutline', color: 'var(--cp-warn)', value: fmtBytes(stat.diskUsed), unit: ` / ${fmtBytes(stat.diskTotal)}`, pct: Math.round(((stat.diskUsed || 0) / (stat.diskTotal || 1)) * 100), foot: `剩余 ${fmtBytes((stat.diskTotal || 0) - (stat.diskUsed || 0))}` },
        { key: 'net', label: '网络', icon: 'WifiOutline', color: 'var(--cp-brand-accent)', value: `↓ ${fmtBytes(stat.netRx)}/s  ↑ ${fmtBytes(stat.netTx)}/s`, unit: '', pct: 0, foot: `在线 ${fmtUptime(stat.uptime || 0)}` },
      ]" :key="c.key" span="4 2:2 1:1">
        <NCard size="small" class="scard cp-rise" :style="`--i:${ci}`">
          <div class="srow">
            <div class="sicon" :style="`--c:${c.color}`"><NIcon :component="icons[c.icon]" :size="19" /></div>
            <div style="flex: 1; min-width: 0">
              <div class="slabel">{{ c.label }}</div>
              <div class="stat-num cp-pop" :class="{ small: c.key === 'net' }" :key="c.value">{{ c.value }}<span v-if="c.unit" class="unit">{{ c.unit }}</span></div>
            </div>
          </div>
          <template v-if="c.key !== 'net'">
            <NProgress
              type="line" :show-indicator="false" :percentage="c.pct" :height="4"
              :color="c.key === 'cpu' ? (c.pct > 85 ? 'var(--cp-err)' : 'var(--cp-brand)') : c.key === 'mem' ? (c.pct > 88 ? 'var(--cp-warn)' : 'var(--cp-ok)') : 'var(--cp-warn)'"
              style="margin-top: var(--space-3)"
            />
          </template>
          <div class="sfoot">{{ c.foot }}</div>
        </NCard>
      </NGridItem>
    </NGrid>

    <!-- charts -->
    <NGrid :cols="2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem span="2 1:2">
        <NCard size="small">
          <template #header><span class="section-title">CPU 趋势</span></template>
          <NSkeleton v-if="loading" height="60px" :repeat="3" class="cp-shimmer" style="border-radius: var(--radius-sm)" />
          <div ref="cpuEl" style="height: 200px; width: 100%; min-width: 0"></div>
        </NCard>
      </NGridItem>
      <NGridItem span="2 1:2">
        <NCard size="small">
          <template #header><span class="section-title">内存趋势</span></template>
          <NSkeleton v-if="loading" height="60px" :repeat="3" class="cp-shimmer" style="border-radius: var(--radius-sm)" />
          <div ref="memEl" style="height: 200px; width: 100%; min-width: 0"></div>
        </NCard>
      </NGridItem>
    </NGrid>

    <!-- apps + services overview -->
    <NGrid :cols="2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem span="2 1:2">
        <NCard size="small">
          <template #header><span class="section-title">应用（{{ runningApps }}/{{ apps.length }} 运行中）</span></template>
          <template #header-extra>
            <NButton text type="primary" size="tiny" aria-label="查看全部应用" @click="router.push('/apps')">全部 <NIcon :component="icons.ChevronForwardOutline" /></NButton>
          </template>
          <div v-for="(a, ai) in apps.slice(0, 6)" :key="a.id" class="app-row cp-rise" :style="`--i:${ai}`" role="button" tabindex="0"
               :aria-label="`查看应用 ${a.name}`"
               @click="router.push(`/apps/${a.id}`)" @keyup.enter="router.push(`/apps/${a.id}`)" @keyup.space.prevent="router.push(`/apps/${a.id}`)">
            <NIcon :component="a.type === 'node' ? icons.LogoElectron : icons.LogoPython" :size="18" :color="a.type === 'node' ? 'var(--cp-node)' : 'var(--cp-python)'" />
            <span class="app-name">{{ a.name }}</span>
            <span class="dot" :class="a.running ? 'ok' : 'idle'"></span>
            <NText depth="3" style="font-size: var(--fs-xs)">{{ a.running ? '运行中' : a.deploying ? '部署中' : '已停止' }}</NText>
          </div>
          <EmptyBox v-if="!apps.length" :text="sideError ? '应用列表获取失败，下个刷新周期自动重试' : '暂无应用，去应用页创建第一个'" />
        </NCard>
      </NGridItem>
      <NGridItem span="2 1:2">
        <NCard size="small">
          <template #header><span class="section-title">服务健康</span></template>
          <template #header-extra>
            <NButton text type="primary" size="tiny" aria-label="查看全部服务" @click="router.push('/services')">全部 <NIcon :component="icons.ChevronForwardOutline" /></NButton>
          </template>
          <EmptyBox v-if="sideError" :text="`服务状态获取失败：${sideError}`" />
          <EmptyBox v-else-if="!failedUnits.length" text="所有 systemd 服务运行正常，无 failed 单元" />
          <div v-else>
            <div v-for="u in failedUnits.slice(0, 6)" :key="u" class="app-row" role="button" tabindex="0"
                 :aria-label="`查看异常服务 ${u}`"
                 @click="router.push('/services')" @keyup.enter="router.push('/services')" @keyup.space.prevent="router.push('/services')">
              <NIcon :component="icons.AlertCircleOutline" :size="18" color="var(--cp-err)" />
              <span class="app-name">{{ u }}</span>
              <span class="st err"><span class="dot err"></span>failed</span>
            </div>
          </div>
        </NCard>
      </NGridItem>
    </NGrid>
  </NSpace>
</template>

<style scoped>
.hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
  background: linear-gradient(120deg, color-mix(in srgb, var(--cp-brand) 12%, transparent), color-mix(in srgb, var(--cp-brand-accent) 7%, transparent));
  border: 1px solid color-mix(in srgb, var(--cp-brand) 18%, transparent);
  border-radius: var(--radius-lg);
  padding: var(--space-4) var(--space-5);
}
.hero-greet { font-size: var(--fs-md); color: var(--cp-text); }
.hero-greet b { color: var(--cp-brand-soft); }
.hero-host { display: flex; align-items: center; gap: var(--space-2); font-size: var(--fs-xs); color: var(--cp-text-mute); margin-top: var(--space-1); }
.hero-host-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.scard { position: relative; }
.srow { display: flex; align-items: center; gap: var(--space-3); }
.sicon {
  width: 40px; height: 40px; border-radius: 11px;
  display: flex; align-items: center; justify-content: center;
  color: var(--c);
  background: color-mix(in srgb, var(--c) 14%, transparent);
}
.slabel { font-size: var(--fs-xs); color: var(--cp-text-mute); margin-bottom: var(--space-1); }
.stat-num.small { font-size: var(--fs-lg); }
.unit { font-size: var(--fs-xs); color: var(--cp-text-mute); font-weight: 400; }
.sfoot { font-size: var(--fs-2xs); color: var(--cp-text-mute); margin-top: var(--space-1); }
.app-row {
  display: flex; align-items: center; gap: var(--space-3);
  padding: var(--space-2); border-radius: var(--radius); cursor: pointer;
  transition: background var(--dur) var(--ease);
}
.app-row:hover { background: var(--cp-hover); }
.app-name { font-size: var(--fs-sm); color: var(--cp-text); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
</style>
