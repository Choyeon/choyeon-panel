<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, computed, h, nextTick } from 'vue';
import { useRouter } from 'vue-router';
import { NCard, NGrid, NGridItem, NSpace, NText, NIcon, NProgress, NTag, NButton, NDropdown } from 'naive-ui';
import * as echarts from 'echarts';
import { api, getRole } from '../api';
import { icons } from '../icons';

const router = useRouter();
const isAdmin = getRole() === 'admin';
const info = ref<any>({});
const stat = ref<any>({});
const apps = ref<any[]>([]);
const failedUnits = ref<string[]>([]);
const cpuEl = ref<HTMLElement | null>(null);
const memEl = ref<HTMLElement | null>(null);
let cpuChart: echarts.ECharts | null = null;
let memChart: echarts.ECharts | null = null;
let timer: number | null = null;

function fmtBytes(n: number) {
  if (n == null) return '-';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function fmtUptime(s: number) {
  const d = Math.floor(s / 86400), h2 = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}天${h2}小时` : `${h2}小时${m}分`;
}

const runningApps = computed(() => apps.value.filter((a) => a.running).length);
const hour = new Date().getHours();
const greet = hour < 6 ? '夜深了' : hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好';

function chartOpts(times: string[], series: any[], yfmt: (v: number) => string) {
  return {
    grid: { left: 8, right: 14, top: 14, bottom: 4, containLabel: true },
    xAxis: { type: 'category', data: times, boundaryGap: false, axisLine: { lineStyle: { color: '#2c2c33' } }, axisLabel: { color: '#666', fontSize: 10, showMaxLabel: true } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(255,255,255,0.05)' } }, axisLabel: { color: '#666', fontSize: 10, formatter: yfmt } },
    series, tooltip: { trigger: 'axis' },
    animation: false,
  };
}

async function tick() {
  try {
    stat.value = await api.stats();
  } catch {
    return; // 401 时 api.req 已跳转登录；网络抖动等下个周期自动恢复
  }
  const hist: any[] = (stat.value.history || []).slice(-60);
  const times = hist.map((x) => new Date(x.t).toLocaleTimeString());
  cpuChart?.setOption(
    chartOpts(times, [{ name: 'CPU', type: 'line', smooth: 0.4, showSymbol: false, data: hist.map((x) => x.cpu), lineStyle: { width: 2, color: '#4f7cff' }, areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(79,124,255,0.35)' }, { offset: 1, color: 'rgba(79,124,255,0)' }]) } }], (v: number) => v + '%'),
  );
  memChart?.setOption(
    chartOpts(times, [{ name: '内存', type: 'line', smooth: 0.4, showSymbol: false, data: hist.map((x) => x.memUsed / 1048576), lineStyle: { width: 2, color: '#34c77b' }, areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(52,199,123,0.3)' }, { offset: 1, color: 'rgba(52,199,123,0)' }]) } }], (v: number) => (v / 1024).toFixed(1) + 'G'),
  );
}

async function loadSide() {
  try {
    apps.value = await api.apps();
    const svc = await api.services();
    failedUnits.value = svc.filter((s: any) => s.active === 'failed').map((s: any) => s.unit);
  } catch {}
}

const quickOpts = [
  { label: '部署新应用', key: 'deploy', icon: icons.AddOutline, onClick: () => router.push('/apps') },
  { label: '打开终端', key: 'term', icon: icons.TerminalOutline, disabled: !isAdmin, onClick: () => router.push('/terminal') },
  { label: '查看备份', key: 'bk', icon: icons.TimeOutline, onClick: () => router.push('/backups') },
];

onMounted(async () => {
  try {
    info.value = await api.info();
  } catch {}
  await nextTick();
  if (cpuEl.value) cpuChart = echarts.init(cpuEl.value);
  if (memEl.value) memChart = echarts.init(memEl.value);
  await tick();
  loadSide();
  timer = window.setInterval(tick, 3000);
});

function onResize() { cpuChart?.resize(); memChart?.resize(); }
window.addEventListener('resize', onResize);
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize);
  if (timer) clearInterval(timer);
  cpuChart?.dispose();
  memChart?.dispose();
});
</script>

<template>
  <NSpace vertical :size="18">
    <!-- hero -->
    <div class="hero">
      <div>
        <div class="hero-greet">{{ greet }}，欢迎回到 <b>choyeon panel</b></div>
        <div class="hero-host">
          <NIcon :component="icons.ServerOutline" :size="15" color="#8fa8ff" />
          {{ info.hostname }} · {{ info.prettyName || info.platform }}
        </div>
      </div>
      <NSpace align="center" :size="10">
        <NTag round size="small" :bordered="false" type="info">Node {{ info.node }}</NTag>
        <NTag v-if="!failedUnits.length" round size="small" type="success" :bordered="false">
          <template #icon><NIcon :component="icons.CheckmarkCircleOutline" /></template>
          服务全部正常
        </NTag>
        <NTag v-else round size="small" type="error" :bordered="false">{{ failedUnits.length }} 个服务异常</NTag>
        <NDropdown trigger="click" :options="quickOpts.map((o) => ({ ...o, icon: () => h(NIcon, { component: o.icon, size: 16 }) }))">
          <NButton type="primary" round size="small">快捷操作</NButton>
        </NDropdown>
      </NSpace>
    </div>

    <!-- stat cards -->
    <NGrid :cols="4" :x-gap="14" :y-gap="14" responsive="screen" item-responsive>
      <NGridItem span="4 2:2 1:1">
        <NCard size="small" class="scard">
          <div class="srow">
            <div class="sicon" style="--c:#4f7cff"><NIcon :component="icons.PulseOutline" :size="19" /></div>
            <div style="flex:1">
              <div class="slabel">CPU</div>
              <div class="stat-num">{{ stat.cpu ?? '-' }}<span class="unit">%</span></div>
            </div>
          </div>
          <NProgress type="line" :show-indicator="false" :percentage="stat.cpu || 0" :height="4" :color="stat.cpu > 85 ? '#f5616c' : '#4f7cff'" style="margin-top: 10px" />
          <div class="sfoot">{{ info.cpuCores }} 核 · 负载 {{ (stat.load || []).map((x: number) => x.toFixed(1)).join(' / ') }}</div>
        </NCard>
      </NGridItem>
      <NGridItem span="4 2:2 1:1">
        <NCard size="small" class="scard">
          <div class="srow">
            <div class="sicon" style="--c:#34c77b"><NIcon :component="icons.CubeOutline" :size="19" /></div>
            <div style="flex:1">
              <div class="slabel">内存</div>
              <div class="stat-num">{{ fmtBytes(stat.memUsed) }}<span class="unit"> / {{ fmtBytes(stat.memTotal) }}</span></div>
            </div>
          </div>
          <NProgress type="line" :show-indicator="false" :percentage="Math.round(((stat.memUsed || 0) / (stat.memTotal || 1)) * 100)" :height="4" :color="(stat.memUsed / stat.memTotal) * 100 > 88 ? '#f5a623' : '#34c77b'" style="margin-top: 10px" />
          <div class="sfoot">可用 {{ fmtBytes((stat.memTotal || 0) - (stat.memUsed || 0)) }}</div>
        </NCard>
      </NGridItem>
      <NGridItem span="4 2:2 1:1">
        <NCard size="small" class="scard">
          <div class="srow">
            <div class="sicon" style="--c:#f5a623"><NIcon :component="icons.DocumentTextOutline" :size="19" /></div>
            <div style="flex:1">
              <div class="slabel">磁盘 /</div>
              <div class="stat-num">{{ fmtBytes(stat.diskUsed) }}<span class="unit"> / {{ fmtBytes(stat.diskTotal) }}</span></div>
            </div>
          </div>
          <NProgress type="line" :show-indicator="false" :percentage="Math.round(((stat.diskUsed || 0) / (stat.diskTotal || 1)) * 100)" :height="4" color="#f5a623" style="margin-top: 10px" />
          <div class="sfoot">剩余 {{ fmtBytes((stat.diskTotal || 0) - (stat.diskUsed || 0)) }}</div>
        </NCard>
      </NGridItem>
      <NGridItem span="4 2:2 1:1">
        <NCard size="small" class="scard">
          <div class="srow">
            <div class="sicon" style="--c:#7f5bff"><NIcon :component="icons.WifiOutline ?? icons.GlobeOutline" :size="19" /></div>
            <div style="flex:1">
              <div class="slabel">网络</div>
              <div class="stat-num" style="font-size:17px">↓ {{ fmtBytes(stat.netRx) }}/s&nbsp; ↑ {{ fmtBytes(stat.netTx) }}/s</div>
            </div>
          </div>
          <div class="sfoot" style="margin-top: 18px">在线 {{ fmtUptime(stat.uptime || 0) }}</div>
        </NCard>
      </NGridItem>
    </NGrid>

    <!-- charts -->
    <NGrid :cols="2" :x-gap="14" responsive="screen" item-responsive>
      <NGridItem span="2 1:2">
        <NCard title="CPU 趋势" size="small" header-text-style="font-size:13px;color:#a9a9b4">
          <div ref="cpuEl" style="height: 200px"></div>
        </NCard>
      </NGridItem>
      <NGridItem span="2 1:2">
        <NCard title="内存趋势" size="small" header-text-style="font-size:13px;color:#a9a9b4">
          <div ref="memEl" style="height: 200px"></div>
        </NCard>
      </NGridItem>
    </NGrid>

    <!-- apps + services overview -->
    <NGrid :cols="2" :x-gap="14" responsive="screen" item-responsive>
      <NGridItem span="2 1:2">
        <NCard size="small">
          <template #header><span style="font-size:13.5px">应用（{{ runningApps }}/{{ apps.length }} 运行中）</span></template>
          <template #header-extra><NButton text type="primary" size="tiny" @click="router.push('/apps')">全部 <NIcon :component="icons.ChevronForwardOutline" /></NButton></template>
          <div v-for="a in apps.slice(0, 6)" :key="a.id" class="app-row" @click="router.push(`/apps/${a.id}`)">
            <NIcon :component="a.type === 'node' ? icons.LogoElectron : icons.LogoPython" :size="16" :color="a.type === 'node' ? '#8fd460' : '#4f7cff'" />
            <span class="app-name">{{ a.name }}</span>
            <span class="dot" :class="a.running ? 'ok' : 'idle'"></span>
            <NText depth="3" style="font-size: 12px">{{ a.running ? '运行中' : a.deploying ? '部署中' : '已停止' }}</NText>
          </div>
        </NCard>
      </NGridItem>
      <NGridItem span="2 1:2">
        <NCard size="small">
          <template #header><span style="font-size:13.5px">服务健康</span></template>
          <template #header-extra><NButton text type="primary" size="tiny" @click="router.push('/services')">全部 <NIcon :component="icons.ChevronForwardOutline" /></NButton></template>
          <div v-if="!failedUnits.length" class="all-good">
            <NIcon :component="icons.ShieldCheckmarkOutline" :size="26" color="#34c77b" />
            <span>所有 systemd 服务运行正常，无 failed 单元</span>
          </div>
          <div v-else>
            <div v-for="u in failedUnits.slice(0, 6)" :key="u" class="app-row" @click="router.push('/services')">
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
  background: linear-gradient(120deg, rgba(79, 124, 255, 0.12), rgba(127, 91, 255, 0.07));
  border: 1px solid rgba(79, 124, 255, 0.18);
  border-radius: 12px;
  padding: 16px 20px;
}
.hero-greet { font-size: 15px; color: #d6d6e0; }
.hero-greet b { color: #8fa8ff; }
.hero-host { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: #80808c; margin-top: 5px; }
.scard { position: relative; }
.srow { display: flex; align-items: center; gap: 12px; }
.sicon {
  width: 40px;
  height: 40px;
  border-radius: 11px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--c);
  background: color-mix(in srgb, var(--c) 14%, transparent);
}
.slabel { font-size: 12px; color: #77777f; margin-bottom: 2px; }
.unit { font-size: 12.5px; color: #77777f; font-weight: 400; }
.sfoot { font-size: 11.5px; color: #606068; margin-top: 4px; }
.app-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 6px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.12s;
}
.app-row:hover { background: rgba(255, 255, 255, 0.045); }
.app-name { font-size: 13px; color: #cfcfd8; flex: 1; }
.all-good { display: flex; align-items: center; gap: 12px; padding: 22px 8px; color: #8b8b96; font-size: 13px; }
</style>
