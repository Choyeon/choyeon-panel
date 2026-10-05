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

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

const router = useRouter();
const isAdmin = getRole() === 'admin';
const info = ref<any>({});
const stat = ref<any>({});
const apps = ref<any[]>([]);
const failedUnits = ref<string[]>([]);
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
  const dark = document.documentElement.dataset.theme !== 'light';
  return { label: dark ? '#666' : '#8a90a0', line: dark ? '#2c2c33' : '#e3e6ef', split: dark ? 'rgba(255,255,255,0.05)' : '#eef1f7' };
}

function chartOpts(times: string[], series: any[], yfmt: (v: number) => string) {
  const c = axisColor();
  return {
    grid: { left: 8, right: 14, top: 14, bottom: 4, containLabel: true },
    xAxis: {
      type: 'category', data: times, boundaryGap: false,
      axisLine: { lineStyle: { color: c.line } },
      axisLabel: { color: c.label, fontSize: 10, showMaxLabel: true, hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: c.split } },
      axisLabel: { color: c.label, fontSize: 10, formatter: yfmt },
    },
    series,
    tooltip: { trigger: 'axis' },
    animation: false,
  };
}

function areaColor(rgb: string) {
  return {
    type: 'linear' as const, x: 0, y: 0, x2: 0, y2: 1,
    colorStops: [
      { offset: 0, color: `rgba(${rgb},0.32)` },
      { offset: 1, color: `rgba(${rgb},0)` },
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
      lineStyle: { width: 2, color: '#4f7cff' },
      areaStyle: { color: areaColor('79,124,255') },
    }], (v: number) => v + '%'),
    { notMerge: true },
  );
  memChart?.setOption(
    chartOpts(times, [{
      name: '内存', type: 'line', smooth: 0.4, showSymbol: false,
      data: hist.map((x) => (x.memUsed || 0) / 1048576),
      lineStyle: { width: 2, color: '#34c77b' },
      areaStyle: { color: areaColor('52,199,123') },
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
  } catch { /* 单行失败不影响主面板 */ }
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
  timer = window.setInterval(tick, 3000);

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
  <NSpace vertical :size="18">
    <!-- hero -->
    <div class="hero">
      <div style="min-width: 0">
        <div class="hero-greet">{{ greet }}，欢迎回到 <b>choyeon panel</b></div>
        <div class="hero-host">
          <NIcon :component="icons.ServerOutline" :size="15" color="#8fa8ff" />
          <span class="hero-host-text">{{ info.hostname || '本机' }} · {{ info.prettyName || info.platform || '加载中' }}</span>
        </div>
      </div>
      <NSpace align="center" :size="10" :wrap="false">
        <NTag v-if="info.node" round size="small" :bordered="false" type="info">{{ info.node }}</NTag>
        <NTag v-if="!failedUnits.length" round size="small" type="success" :bordered="false">
          <template #icon><NIcon :component="icons.CheckmarkCircleOutline" /></template>
          服务正常
        </NTag>
        <NTag v-else round size="small" type="error" :bordered="false">{{ failedUnits.length }} 个服务异常</NTag>
        <NDropdown trigger="click" :options="quickMenu">
          <NButton type="primary" round size="small">快捷操作</NButton>
        </NDropdown>
      </NSpace>
    </div>

    <!-- stat cards -->
    <NGrid :cols="4" :x-gap="14" :y-gap="14" responsive="screen" item-responsive>
      <NGridItem v-for="c in [
        { key: 'cpu', label: 'CPU', icon: 'PulseOutline', color: '#4f7cff', value: `${stat.cpu ?? '-'}`, unit: '%', pct: stat.cpu || 0, foot: `${info.cpuCores || '?'} 核 · 负载 ${(stat.load || []).map((x: number) => x.toFixed(1)).join(' / ') || '-'}` },
        { key: 'mem', label: '内存', icon: 'CubeOutline', color: '#34c77b', value: fmtBytes(stat.memUsed), unit: ` / ${fmtBytes(stat.memTotal)}`, pct: Math.round(((stat.memUsed || 0) / (stat.memTotal || 1)) * 100), foot: `可用 ${fmtBytes((stat.memTotal || 0) - (stat.memUsed || 0))}` },
        { key: 'disk', label: '磁盘 /', icon: 'DocumentTextOutline', color: '#f5a623', value: fmtBytes(stat.diskUsed), unit: ` / ${fmtBytes(stat.diskTotal)}`, pct: Math.round(((stat.diskUsed || 0) / (stat.diskTotal || 1)) * 100), foot: `剩余 ${fmtBytes((stat.diskTotal || 0) - (stat.diskUsed || 0))}` },
        { key: 'net', label: '网络', icon: 'WifiOutline', color: '#7f5bff', value: `↓ ${fmtBytes(stat.netRx)}/s  ↑ ${fmtBytes(stat.netTx)}/s`, unit: '', pct: 0, foot: `在线 ${fmtUptime(stat.uptime || 0)}` },
      ]" :key="c.key" span="4 2:2 1:1">
        <NCard size="small" class="scard">
          <div class="srow">
            <div class="sicon" :style="`--c:${c.color}`"><NIcon :component="icons[c.icon]" :size="19" /></div>
            <div style="flex: 1; min-width: 0">
              <div class="slabel">{{ c.label }}</div>
              <div class="stat-num" :class="{ small: c.key === 'net' }">{{ c.value }}<span v-if="c.unit" class="unit">{{ c.unit }}</span></div>
            </div>
          </div>
          <template v-if="c.key !== 'net'">
            <NProgress
              type="line" :show-indicator="false" :percentage="c.pct" :height="4"
              :color="c.key === 'cpu' ? (c.pct > 85 ? '#f5616c' : '#4f7cff') : c.key === 'mem' ? (c.pct > 88 ? '#f5a623' : '#34c77b') : '#f5a623'"
              style="margin-top: 10px"
            />
          </template>
          <div class="sfoot">{{ c.foot }}</div>
        </NCard>
      </NGridItem>
    </NGrid>

    <!-- charts -->
    <NGrid :cols="2" :x-gap="14" :y-gap="14" responsive="screen" item-responsive>
      <NGridItem span="2 1:2">
        <NCard title="CPU 趋势" size="small" header-text-style="font-size:13px">
          <NSkeleton v-if="loading" text :repeat="3" />
          <div ref="cpuEl" style="height: 200px; width: 100%"></div>
        </NCard>
      </NGridItem>
      <NGridItem span="2 1:2">
        <NCard title="内存趋势" size="small" header-text-style="font-size:13px">
          <NSkeleton v-if="loading" text :repeat="3" />
          <div ref="memEl" style="height: 200px; width: 100%"></div>
        </NCard>
      </NGridItem>
    </NGrid>

    <!-- apps + services overview -->
    <NGrid :cols="2" :x-gap="14" :y-gap="14" responsive="screen" item-responsive>
      <NGridItem span="2 1:2">
        <NCard size="small">
          <template #header><span style="font-size:13.5px">应用（{{ runningApps }}/{{ apps.length }} 运行中）</span></template>
          <template #header-extra>
            <NButton text type="primary" size="tiny" @click="router.push('/apps')">全部 <NIcon :component="icons.ChevronForwardOutline" /></NButton>
          </template>
          <div v-for="a in apps.slice(0, 6)" :key="a.id" class="app-row" role="button" tabindex="0"
               @click="router.push(`/apps/${a.id}`)" @keyup.enter="router.push(`/apps/${a.id}`)">
            <NIcon :component="a.type === 'node' ? icons.LogoElectron : icons.LogoPython" :size="16" :color="a.type === 'node' ? '#8fd460' : '#4f7cff'" />
            <span class="app-name">{{ a.name }}</span>
            <span class="dot" :class="a.running ? 'ok' : 'idle'"></span>
            <NText depth="3" style="font-size: 12px">{{ a.running ? '运行中' : a.deploying ? '部署中' : '已停止' }}</NText>
          </div>
          <div v-if="!apps.length" class="all-good"><span>暂无应用</span></div>
        </NCard>
      </NGridItem>
      <NGridItem span="2 1:2">
        <NCard size="small">
          <template #header><span style="font-size:13.5px">服务健康</span></template>
          <template #header-extra>
            <NButton text type="primary" size="tiny" @click="router.push('/services')">全部 <NIcon :component="icons.ChevronForwardOutline" /></NButton>
          </template>
          <div v-if="!failedUnits.length" class="all-good">
            <NIcon :component="icons.ShieldCheckmarkOutline" :size="26" color="#34c77b" />
            <span>所有 systemd 服务运行正常，无 failed 单元</span>
          </div>
          <div v-else>
            <div v-for="u in failedUnits.slice(0, 6)" :key="u" class="app-row" role="button" tabindex="0"
                 @click="router.push('/services')" @keyup.enter="router.push('/services')">
              <NIcon :component="icons.AlertCircleOutline" :size="16" color="#f5616c" />
              <span class="app-name">{{ u }}</span>
              <NTag size="tiny" type="error" :bordered="false">failed</NTag>
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
  gap: 12px;
  flex-wrap: wrap;
  background: linear-gradient(120deg, rgba(79, 124, 255, 0.12), rgba(127, 91, 255, 0.07));
  border: 1px solid rgba(79, 124, 255, 0.18);
  border-radius: 12px;
  padding: 16px 20px;
}
.hero-greet { font-size: 15px; color: var(--cp-text); }
.hero-greet b { color: var(--brand-soft); }
.hero-host { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--cp-text-mute); margin-top: 5px; }
.hero-host-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.scard { position: relative; }
.srow { display: flex; align-items: center; gap: 12px; }
.sicon {
  width: 40px; height: 40px; border-radius: 11px;
  display: flex; align-items: center; justify-content: center;
  color: var(--c);
  background: color-mix(in srgb, var(--c) 14%, transparent);
}
.slabel { font-size: 12px; color: var(--cp-text-mute); margin-bottom: 2px; }
.stat-num.small { font-size: 16px; }
.unit { font-size: 12.5px; color: var(--cp-text-mute); font-weight: 400; }
.sfoot { font-size: 11.5px; color: var(--cp-text-mute); margin-top: 4px; }
.app-row {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 6px; border-radius: 8px; cursor: pointer;
  transition: background var(--dur) var(--ease);
}
.app-row:hover { background: var(--cp-hover); }
.app-name { font-size: 13px; color: var(--cp-text); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.all-good { display: flex; align-items: center; gap: 12px; padding: 22px 8px; color: var(--cp-text-dim); font-size: 13px; }
</style>
