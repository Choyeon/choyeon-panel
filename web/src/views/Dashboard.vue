<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { NCard, NDropdown, NGrid, NGridItem, NIcon, NProgress, NSpace, NTag, NText } from 'naive-ui';
import { NButton } from '../ui';
import { api, getRole, req } from '../api';
import { icons, icon } from '../icons';
import { fmtAgo, fmtBytes } from '../composables/useFormat';
import EmptyBox from '../components/EmptyBox.vue';
import MetricChart from '../components/MetricChart.vue';

const router = useRouter();
const isAdmin = getRole() === 'admin';
const info = ref<any>({});
const stat = ref<any>({});
const apps = ref<any[]>([]);
const failedUnits = ref<string[]>([]);
const sideError = ref('');
const rtLoading = ref(true);

// 卸载后 in-flight 请求的回包一律丢弃（api 层不透传 AbortController，仓库不许改它）；
// 定时器在同一处清空，保证卸载后不再有任何新的请求发出。
let disposed = false;
let timer: number | null = null;

// 卡片宽度装不下 "8.0 GB / 49.0 GB"（会折成两行且把 GB 单独甩下去）。同单位时
// 把单位只写一次，交给小号 .unit 显示，主数字保持一行。
function usedTotal(used: number, total: number) {
  const a = fmtBytes(used);
  const b = fmtBytes(total);
  const su = a.replace(/^[\d.]+\s*/, '');
  return su && b.endsWith(su)
    ? { value: a.slice(0, a.length - su.length).trim(), unit: ` / ${b}` }
    : { value: a, unit: ` / ${b}` };
}
function fmtUptime(s: number) {
  if (!s) return '-';
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}天${hh}小时` : `${hh}小时${m}分`;
}
const rateAxisFmt = (v: number) => {
  const a = Math.abs(v);
  if (a >= 1048576) return `${(v / 1048576).toFixed(1)}M`;
  if (a >= 1024) return `${Math.round(v / 1024)}K`;
  return String(Math.round(v));
};
const rateTipFmt = (v: number) => `${fmtBytes(v)}/s`;

const runningApps = computed(() => apps.value.filter((a) => a.running).length);
const hour = new Date().getHours();
const greet = hour < 6 ? '夜深了' : hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好';

/* ---------- 实时：3s 轮询 /system/stats 的内存曲线 ---------- */
const rtHistory = computed(() => (stat.value.history || []).slice(-60));
const rtXs = computed<string[]>(() => rtHistory.value.map((x: any) => new Date(x.t).toLocaleTimeString()));
const rtSeries = computed(() => [
  { name: 'CPU %', color: '--cp-brand', data: rtHistory.value.map((x: any) => x.cpu) as (number | null)[], area: true },
  {
    name: '内存 GB', color: '--cp-ok', axis: 1,
    // memUsed 是字节，1GB = 1024*1024*1024；除以 1048576 得到的是 MB，轴上会显示"3000G"
    data: rtHistory.value.map((x: any) => Math.round((x.memUsed / 1073741824) * 10) / 10) as (number | null)[],
  },
]);

/* ---------- 历史：持久化行 /system/history ---------- */
const RANGES = ['1h', '6h', '24h', '7d'] as const;
type RangeKey = (typeof RANGES)[number];
const histRange = ref<RangeKey>('6h');
const hist = ref<any>(null);
const histLoading = ref(true);
const histError = ref('');
const mstat = ref<any>(null);
const histPoints = computed<number>(() => hist.value?.meta?.points ?? 0);
const histEmpty = computed(() => !histError.value && !histLoading.value && histPoints.value === 0);

const histXs = computed<string[]>(() => {
  const ts: number[] = hist.value?.ts || [];
  const useDate = histRange.value === '7d';
  return ts.map((t) => {
    const d = new Date(t);
    return useDate
      ? `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:00`
      : d.toLocaleTimeString();
  });
});
const netSeries = computed(() => [
  { name: '↓ 入向', color: '--cp-brand', data: (hist.value?.netRx || []) as (number | null)[], area: true },
  { name: '↑ 出向', color: '--cp-brand-accent', data: (hist.value?.netTx || []) as (number | null)[] },
]);
const ioSeries = computed(() => [
  { name: '读', color: '--cp-brand', data: (hist.value?.ioR || []) as (number | null)[] },
  { name: '写', color: '--cp-warn', data: (hist.value?.ioW || []) as (number | null)[] },
]);
const diskSeries = computed(() => {
  const used: (number | null)[] = hist.value?.diskUsed || [];
  const total: (number | null)[] = hist.value?.diskTotal || [];
  const data = used.map((u, i) => {
    const t = total[i];
    return u != null && t ? Math.round((u / t) * 1000) / 10 : null;
  });
  return [{ name: '使用率 %', color: '--cp-warn', data, area: true }];
});
const loadSeries = computed(() => [
  { name: 'load1', color: '--cp-brand-accent', data: (hist.value?.load1 || []) as (number | null)[], area: true },
  { name: '进程数', color: '--cp-text-dim', data: (hist.value?.procs || []) as (number | null)[], axis: 1 },
]);

/* ---------- 进程 Top 10（折叠）/ 最近告警（诚实占位） ---------- */
const procs = ref<any[]>([]);
const procsOpen = ref(false);
const alertsState = ref<'loading' | 'ready' | 'absent' | 'error'>('loading');
const alertsMsg = ref('');
const openAlerts = ref<any[]>([]);
const alertTotal = ref(0);

async function tick() {
  try {
    const s = await api.stats();
    if (disposed) return;
    stat.value = s;
  } catch {
    return; // 401 时 api 层已跳转登录；网络抖动等下个周期自动恢复
  } finally {
    if (!disposed) rtLoading.value = false;
  }
}

async function loadHistory() {
  histLoading.value = true;
  histError.value = '';
  try {
    const [d, st] = await Promise.all([
      req(`/system/history?range=${histRange.value}`),
      req('/system/metrics-status'),
    ]);
    if (disposed) return;
    hist.value = d;
    mstat.value = st;
  } catch (e: any) {
    if (disposed) return;
    hist.value = null;
    histError.value = e?.message || '历史数据获取失败';
  } finally {
    if (!disposed) histLoading.value = false;
  }
}

function pickRange(r: RangeKey) {
  if (histRange.value === r) return;
  histRange.value = r;
  loadHistory();
}

async function loadSide() {
  try {
    const [list, svc] = await Promise.all([api.apps(), api.services()]);
    if (disposed) return;
    apps.value = list;
    failedUnits.value = svc.filter((s: any) => s.active === 'failed').map((s: any) => s.unit);
    sideError.value = '';
  } catch (e: any) {
    if (disposed) return;
    // 拉不到就必须显式说"未知"：failedUnits 留空会被渲染成绿色"服务正常"，
    // 等于把 /system/services 的 500 或超时读成一切正常。
    failedUnits.value = [];
    sideError.value = e?.message || '状态获取失败';
  }
}

async function loadProcs() {
  try {
    const d = await req('/system/processes?limit=10');
    if (!disposed) procs.value = d.items || [];
  } catch { /* 保留上一帧，进程表偶发失败不清空列表 */ }
}

async function probeAlerts() {
  try {
    const d = await req('/alerts/open?limit=6');
    if (disposed) return;
    openAlerts.value = d.rows || [];
    alertTotal.value = Number(d.total || 0);
    alertsState.value = 'ready';
  } catch (e: any) {
    if (disposed) return;
    const m = String(e?.message || '');
    alertsState.value = /404|not found/i.test(m) ? 'absent' : 'error';
    alertsMsg.value = m;
  }
}

const refreshing = ref(false);
async function refresh() {
  refreshing.value = true;
  try {
    await Promise.all([tick(), loadHistory(), loadSide(), loadProcs()]);
  } finally {
    if (!disposed) refreshing.value = false;
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
}));
// 选项级 onClick 不在 DropdownOption 上，naive-ui 只认 NDropdown 的 :on-select。
function quickGo(key: string) {
  const to = quickOpts.find((o) => o.key === key)?.to;
  if (to) router.push(to);
}

onMounted(() => {
  // 挂载期全部并发取数，不再串行瀑布
  api.info().then((d) => { if (!disposed) info.value = d; }).catch(() => { /* 降级为未知主机信息 */ });
  Promise.all([tick(), loadHistory(), loadSide(), loadProcs(), probeAlerts()]);
  let n = 0;
  timer = window.setInterval(() => {
    tick();
    if (++n % 5 === 0) { loadSide(); loadProcs(); }   // 侧栏/进程 15s，随 tick 自动重试失败态
    if (n % 10 === 0) loadHistory();                  // raw 行 30s 一条，历史曲线同频更新
  }, 3000);
});

onBeforeUnmount(() => {
  disposed = true;
  if (timer) { clearInterval(timer); timer = null; }
});
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
        <!-- 统计每 3s 自动刷，这里的手动刷新是为了立刻确认刚做过的操作（部署/改配置）生效 -->
        <NButton size="small" tertiary :loading="refreshing" :icon="icon('RefreshOutline')" title="立即刷新状态" @click="refresh">刷新</NButton>
        <NDropdown trigger="click" :options="quickMenu" :on-select="quickGo">
          <NButton type="primary" round size="small" class="cp-press">快捷操作</NButton>
        </NDropdown>
      </NSpace>
    </div>

    <!-- stat cards -->
    <NGrid cols="1 s:2 m:4" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem v-for="(c, ci) in [
        { key: 'cpu', label: 'CPU', icon: 'PulseOutline', color: 'var(--cp-brand)', value: `${stat.cpu ?? '-'}`, unit: '%', pct: stat.cpu || 0, foot: `${info.cpuCores || '?'} 核 · 负载 ${(stat.load || []).map((x: number) => x.toFixed(1)).join(' / ') || '-'}` },
        { key: 'mem', label: '内存', icon: 'CubeOutline', color: 'var(--cp-ok)', ...usedTotal(stat.memUsed, stat.memTotal), pct: Math.round(((stat.memUsed || 0) / (stat.memTotal || 1)) * 100), foot: `可用 ${fmtBytes((stat.memTotal || 0) - (stat.memUsed || 0))}` },
        { key: 'disk', label: '磁盘 /', icon: 'DocumentTextOutline', color: 'var(--cp-warn)', ...usedTotal(stat.diskUsed, stat.diskTotal), pct: Math.round(((stat.diskUsed || 0) / (stat.diskTotal || 1)) * 100), foot: `剩余 ${fmtBytes((stat.diskTotal || 0) - (stat.diskUsed || 0))}` },
        { key: 'net', label: '网络', icon: 'WifiOutline', color: 'var(--cp-brand-accent)', value: `↓ ${fmtBytes(stat.netRx)}/s  ↑ ${fmtBytes(stat.netTx)}/s`, unit: '', pct: 0, foot: `在线 ${fmtUptime(stat.uptime || 0)}` },
      ]" :key="c.key" span="1">
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

    <!-- 实时：CPU + 内存双轴折线 -->
    <NCard size="small">
      <template #header><span class="section-title">实时 · CPU / 内存（3s 轮询）</span></template>
      <MetricChart :xs="rtXs" :series="rtSeries" height="220px" :loading="rtLoading"
                   :y-fmt="(v: number) => v + '%'" :y2-fmt="(v: number) => v + 'G'" />
    </NCard>

    <!-- 历史曲线（持久化行） -->
    <div class="hist-head">
      <span class="section-title">历史曲线 · 网络 / 磁盘 / 负载</span>
      <NSpace align="center" :size="8" :wrap="false">
        <span v-if="mstat?.lastSampleTs" class="sfoot">最近采样 {{ new Date(mstat.lastSampleTs * 1000).toLocaleTimeString() }}</span>
        <NTag size="small" :bordered="false" :type="mstat?.samplerAlive ? 'success' : 'warning'">
          {{ mstat?.samplerAlive ? '采样器运行中' : '采样器未运行' }}
        </NTag>
        <NButton v-for="r in RANGES" :key="r" size="tiny" :type="histRange === r ? 'primary' : 'default'"
                 :tertiary="histRange !== r" @click="pickRange(r)">{{ r }}</NButton>
      </NSpace>
    </div>
    <NGrid cols="1 m:2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem span="1">
        <NCard size="small">
          <template #header><span class="section-title">网络流量（bytes/s）</span></template>
          <EmptyBox v-if="histError" :text="`历史数据获取失败：${histError}，下个周期自动重试`" />
          <EmptyBox v-else-if="histEmpty" text="暂无历史数据：需采样器运行约 30 秒后出现首点" />
          <MetricChart v-else :xs="histXs" :series="netSeries" height="200px" :loading="histLoading"
                       :y-fmt="rateAxisFmt" :tip-fmt="rateTipFmt" />
        </NCard>
      </NGridItem>
      <NGridItem span="1">
        <NCard size="small">
          <template #header><span class="section-title">磁盘使用率（%）</span></template>
          <EmptyBox v-if="histError" :text="`历史数据获取失败：${histError}`" />
          <EmptyBox v-else-if="histEmpty" text="暂无历史数据：等待采样器写入首点" />
          <MetricChart v-else :xs="histXs" :series="diskSeries" height="200px" :loading="histLoading"
                       :y-fmt="(v: number) => v + '%'" />
        </NCard>
      </NGridItem>
      <NGridItem span="1">
        <NCard size="small">
          <template #header><span class="section-title">磁盘 IO（bytes/s）</span></template>
          <EmptyBox v-if="histError" :text="`历史数据获取失败：${histError}`" />
          <EmptyBox v-else-if="histEmpty" text="暂无历史数据：等待采样器写入首点" />
          <MetricChart v-else :xs="histXs" :series="ioSeries" height="200px" :loading="histLoading"
                       :y-fmt="rateAxisFmt" :tip-fmt="rateTipFmt" />
        </NCard>
      </NGridItem>
      <NGridItem span="1">
        <NCard size="small">
          <template #header><span class="section-title">负载与进程数</span></template>
          <EmptyBox v-if="histError" :text="`历史数据获取失败：${histError}`" />
          <EmptyBox v-else-if="histEmpty" text="暂无历史数据：等待采样器写入首点" />
          <MetricChart v-else :xs="histXs" :series="loadSeries" height="200px" :loading="histLoading" />
        </NCard>
      </NGridItem>
    </NGrid>

    <!-- 进程 Top 10 + 最近告警 -->
    <NGrid cols="1 m:2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem span="1">
        <NCard size="small">
          <template #header><span class="section-title">进程 Top 10</span></template>
          <template #header-extra>
            <NButton text type="primary" size="tiny" @click="procsOpen = !procsOpen">
              {{ procsOpen ? '收起' : '展开' }}
              <NIcon :component="icons.ChevronForwardOutline" :style="procsOpen ? 'transform: rotate(90deg)' : ''" />
            </NButton>
          </template>
          <div v-show="!procsOpen" class="sfoot">
            {{ procs.length ? `CPU 最高：${procs[0]?.name || '-'}（${procs[0]?.cpu}%）· 共 ${procs.length} 条` : '暂无进程数据' }}
          </div>
          <div v-show="procsOpen">
            <div class="proc-row proc-head">
              <span>PID</span><span>进程</span><span>用户</span><span class="num">CPU</span><span class="num">内存</span>
            </div>
            <div v-for="p in procs" :key="p.pid" class="proc-row">
              <span class="proc-pid">{{ p.pid }}</span>
              <span class="proc-name">{{ p.name }}</span>
              <span>{{ p.user }}</span>
              <span class="num">{{ p.cpu }}%</span>
              <span class="num">{{ p.memMb }} MB</span>
            </div>
            <EmptyBox v-if="!procs.length" text="进程列表为空或获取失败，15 秒轮询会自动重试" />
          </div>
        </NCard>
      </NGridItem>
      <NGridItem span="1">
        <NCard size="small">
          <template #header>
            <NSpace align="center" :size="8" :wrap="false">
              <span class="section-title">最近告警</span>
              <NTag size="tiny" :bordered="false" :type="alertsState === 'ready' ? 'success' : alertsState === 'error' ? 'warning' : 'default'">
                {{ alertsState === 'ready' ? '已接入' : alertsState === 'absent' ? '未接入' : alertsState === 'error' ? '获取失败' : '检查中' }}
              </NTag>
            </NSpace>
          </template>
          <template #header-extra>
            <NButton text type="primary" size="tiny" aria-label="前往告警中心" @click="router.push('/alerts')">告警中心 <NIcon :component="icons.ChevronForwardOutline" /></NButton>
          </template>
          <EmptyBox v-if="alertsState === 'absent'" text="告警历史接口未接入：后端暂未提供可查询的告警列表接口，此处不展示伪造数据" />
          <EmptyBox v-else-if="alertsState === 'error'" :text="`告警历史获取失败：${alertsMsg}`" />
          <EmptyBox v-else-if="alertsState === 'loading'" text="正在读取未确认告警…" />
          <template v-else>
            <div v-for="(a, ai) in openAlerts" :key="a.id" class="alert-row cp-rise" :style="`--i:${ai}`" :title="a.message">
              <span class="dot" :class="a.level === 'crit' ? 'err' : a.level === 'warn' ? 'warn' : 'ok'"></span>
              <span class="alert-msg">{{ a.message }}</span>
              <NText depth="3" class="alert-ts">{{ fmtAgo(a.ts) }}</NText>
            </div>
            <EmptyBox v-if="!openAlerts.length" text="没有未确认告警，一切正常" />
            <NText v-if="alertTotal > openAlerts.length" depth="3" class="alert-more">
              另有 {{ alertTotal - openAlerts.length }} 条未确认，去告警中心查看
            </NText>
          </template>
        </NCard>
      </NGridItem>
    </NGrid>

    <!-- apps + services overview -->
    <NGrid cols="1 m:2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem span="1">
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
      <NGridItem span="1">
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
.hist-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--space-3); flex-wrap: wrap; margin-top: var(--space-2);
}
.hist-head .sfoot { margin-top: 0; }
.app-row {
  display: flex; align-items: center; gap: var(--space-3);
  padding: var(--space-2); border-radius: var(--radius); cursor: pointer;
  transition: background var(--dur) var(--ease);
}
.app-row:hover { background: var(--cp-hover); }
.app-name { font-size: var(--fs-sm); color: var(--cp-text); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.alert-row {
  display: flex; align-items: center; gap: var(--space-3);
  padding: var(--space-2); border-radius: var(--radius);
}
.alert-msg { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-sm); color: var(--cp-text); }
.alert-ts { font-size: var(--fs-2xs); flex-shrink: 0; }
.alert-more { display: block; margin-top: var(--space-2); font-size: var(--fs-2xs); }
.proc-row {
  display: grid; grid-template-columns: 56px minmax(0, 1fr) 72px 64px 72px;
  gap: var(--space-2); align-items: center;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-xs); color: var(--cp-text);
}
.proc-row .num { text-align: right; }
.proc-head { color: var(--cp-text-mute); font-size: var(--fs-2xs); border-bottom: 1px solid var(--cp-line); }
.proc-pid { color: var(--cp-text-mute); }
.proc-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
