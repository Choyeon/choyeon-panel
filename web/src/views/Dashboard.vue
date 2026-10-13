<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { NCard, NDropdown, NGrid, NGridItem, NIcon, NSelect, NSpace, NTag, NText } from 'naive-ui';
import { NButton } from '../ui';
import { api, getRole, req } from '../api';
import { icons, icon } from '../icons';
import { fmtAgo, fmtBytes } from '../composables/useFormat';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import EmptyBox from '../components/EmptyBox.vue';
import MetricChart from '../components/MetricChart.vue';
import StatGauge from '../components/StatGauge.vue';
import InfoList, { type InfoItem } from '../components/InfoList.vue';

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

/* ---------- 概览计数（一次请求拿齐，取代各页拉全表再数长度） ---------- */
const ov = ref<any>(null);
const ovError = ref('');
const ovItems = computed(() => {
  const o = ov.value;
  const list: { label: string; to: string; value: number | null; note: string; danger?: boolean }[] = [
    {
      label: '网站', to: '/sites',
      value: o ? o.sites : null,
      // sites 为 null 时后端给了 sitesReason（一般是没有 nginx），显示「—」而不是 0
      note: o ? (o.sites == null ? o.sitesReason || '未检测到站点' : 'nginx 域名数') : ovError.value || '读取中',
    },
    { label: '应用', to: '/apps', value: o?.apps ?? null, note: '已创建应用' },
    { label: '备份任务', to: '/backups', value: o?.backups ?? null, note: '启用中的计划' },
    {
      label: '未确认告警', to: '/alerts', value: o?.openAlerts ?? null,
      note: '等待处理', danger: (o?.openAlerts ?? 0) > 0,
    },
  ];
  // /cron 的每个端点服务端都是 need_admin，只读账号点了只会撞路由守卫弹回总览，
  // 所以这一格干脆不出现，而不是给个按不动的数字。
  if (isAdmin) {
    list.splice(2, 0, {
      label: '定时任务', to: '/cron',
      value: o?.cron ?? null, note: o ? `启用 ${o.cronEnabled} 个` : '启用中的计划',
    });
  }
  return list;
});

async function loadOverview() {
  try {
    const d = await req('/system/overview');
    if (disposed) return;
    ov.value = d;
    ovError.value = '';
  } catch (e: any) {
    if (disposed) return;
    ov.value = null;
    ovError.value = e?.message || '概览计数获取失败';
  }
}

/* ---------- 状态：圆环仪表 ---------- */
const num = (v: any) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const pct = (used: any, total: any) => {
  const u = num(used), t = num(total);
  return u != null && t ? Math.round((u / t) * 10000) / 100 : null;
};
const cpuVal = computed(() => num(stat.value.cpu));
const memVal = computed(() => pct(stat.value.memUsed, stat.value.memTotal));
const diskVal = computed(() => pct(stat.value.diskUsed, stat.value.diskTotal));
const cores = computed(() => num(info.value.cpuCores) ?? num(stat.value.cpuCores) ?? 0);
// 负载按「相对核数」画环：load1 = 核数即 100%，这是 1Panel 把负载放进同一排仪表的口径。
const loadVal = computed(() => {
  const l = num((stat.value.load || [])[0]);
  return l != null && cores.value ? Math.round((l / cores.value) * 10000) / 100 : null;
});
function band(v: number | null, warn: number, err: number) {
  if (v == null) return 'var(--cp-brand)';
  // 阈值 ≤0 一律当作"这一档没给"，不染红：载荷缺字段时宁可中性，也不要一个平白的红仪表。
  if (err > 0 && v >= err) return 'var(--cp-err)';
  if (warn > 0 && v >= warn) return 'var(--cp-warn)';
  return 'var(--cp-brand)';
}
const loadWord = computed(() => {
  const v = loadVal.value;
  if (v == null) return '采集中';
  return v < 70 ? '运行流畅' : v < 100 ? '有一定压力' : '负载偏高';
});
const gauges = computed(() => [
  {
    key: 'cpu', label: 'CPU', percent: cpuVal.value, text: cpuVal.value == null ? '-' : String(cpuVal.value),
    unit: '%', detail: `${cores.value || '?'} 核`,
    color: band(cpuVal.value, num(stat.value.cpuWarnPct) ?? 0, num(stat.value.cpuCritPct) ?? 0),
  },
  {
    key: 'mem', label: '内存', percent: memVal.value, text: memVal.value == null ? '-' : String(memVal.value),
    unit: '%', detail: `${fmtBytes(stat.value.memUsed || 0)} / ${fmtBytes(stat.value.memTotal || 0)}`,
    color: band(memVal.value, num(stat.value.memWarnPct) ?? 0, num(stat.value.memCritPct) ?? 0),
  },
  {
    key: 'load', label: '负载', percent: loadVal.value, text: loadWord.value, unit: '',
    // 首轮采样还没到货时 text 是"采集中"，detail 不能同时写 "1min 0.00"——
    // 同一格里两个互相矛盾的读数，用户会以为负载真的是 0。
    detail: stat.value.load?.length ? `1min ${Number(stat.value.load[0]).toFixed(2)}` : '1min —',
    // 这一格的两档是物理口径（相对核数：70% 已排队、100% 每核一个），不是设置页那条线：
    // alert_load1_x 存的是饱和之上的倍数（默认 ×2 → 环要冲到 200% 才出声），
    // 拿它当变色档位等于让负载环永远不会红。详见 alerts_service.resource_thresholds()。
    color: band(loadVal.value, 70, 100),
  },
  {
    key: 'disk', label: `磁盘 ${stat.value.diskPath || '/'}`, percent: diskVal.value, text: diskVal.value == null ? '-' : String(diskVal.value),
    unit: '%', detail: `${fmtBytes(stat.value.diskUsed || 0)} / ${fmtBytes(stat.value.diskTotal || 0)}`,
    color: band(diskVal.value, num(stat.value.diskWarnPct) ?? 0, num(stat.value.diskCritPct) ?? 0),
  },
]);

/* ---------- 监控：网卡选择 + 流量/磁盘 IO 段选 + 拖动条曲线 ---------- */
type MonMode = 'net' | 'io';
const monMode = ref<MonMode>('net');
const ALL_NIC = '__all';
const monNic = ref(ALL_NIC);
// 每网卡速率是后端每 3s 现算的增量，历史不入库；这里在客户端攒环形缓冲，
// 一次请求都不多发，切网卡也能立刻画出各自曲线。
const MON_CAP = 240;
interface MonPoint {
  t: number;
  net: Record<string, [number, number]>;
  all: [number, number];
  ioR: number;
  ioW: number;
}
const monBuf = ref<MonPoint[]>([]);

const nicList = computed<any[]>(() => stat.value.nics || []);
const nicOpts = computed(() => [
  { label: '全部网卡', value: ALL_NIC },
  ...nicList.value.map((n: any) => ({ label: n.up ? n.name : `${n.name}（已断开）`, value: n.name })),
]);
// 网卡热插拔/重命名后原名就没了，选中项必须自己回到「全部网卡」，
// 否则曲线会静止在一条 0 线上，看着像链路断了。
watch(nicList, (list) => {
  if (monNic.value !== ALL_NIC && !list.some((n: any) => n.name === monNic.value)) monNic.value = ALL_NIC;
});

const monXs = computed(() => monBuf.value.map((p) => new Date(p.t).toLocaleTimeString()));
const monSeries = computed(() => {
  const buf = monBuf.value;
  // 1Panel 的监控曲线是**直连折线**（不平滑）：网速尖峰要能看出毛刺，平滑会把峰抹平。
  if (monMode.value === 'io') {
    return [
      { name: '读取', color: '--cp-ok', data: buf.map((p) => p.ioR), area: true, straight: true },
      { name: '写入', color: '--cp-warn', data: buf.map((p) => p.ioW), area: true, straight: true },
    ];
  }
  const pick = (p: MonPoint, i: 0 | 1) => (monNic.value === ALL_NIC ? p.all[i] : p.net[monNic.value]?.[i] ?? 0);
  return [
    { name: '上行', color: '--cp-ok', data: buf.map((p) => pick(p, 1)), area: true, straight: true },
    { name: '下行', color: '--cp-warn', data: buf.map((p) => pick(p, 0)), area: true, straight: true },
  ];
});
const monChips = computed(() => {
  const s = stat.value;
  if (monMode.value === 'io') {
    return [
      { k: '读取', v: `${fmtBytes(s.ioRead || 0)}/s` },
      { k: '写入', v: `${fmtBytes(s.ioWrite || 0)}/s` },
    ];
  }
  const one = monNic.value === ALL_NIC ? null : nicList.value.find((n: any) => n.name === monNic.value);
  if (one) {
    return [
      { k: '上行', v: `${fmtBytes(one.tx || 0)}/s` }, { k: '下行', v: `${fmtBytes(one.rx || 0)}/s` },
      { k: '总发送', v: fmtBytes(one.totalTx || 0) }, { k: '总接收', v: fmtBytes(one.totalRx || 0) },
    ];
  }
  return [
    { k: '上行', v: `${fmtBytes(s.netTx || 0)}/s` }, { k: '下行', v: `${fmtBytes(s.netRx || 0)}/s` },
    { k: '总发送', v: fmtBytes(s.netTotalTx || 0) }, { k: '总接收', v: fmtBytes(s.netTotalRx || 0) },
  ];
});
const monNicDown = computed(() => {
  const one = monNic.value === ALL_NIC ? null : nicList.value.find((n: any) => n.name === monNic.value);
  return one && !one.up;
});

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

/* ---------- 系统信息 ---------- */
const sysItems = computed<InfoItem[]>(() => [
  { k: '主机名', v: info.value.hostname || '' },
  { k: '系统', v: info.value.prettyName || info.value.platform || '' },
  // 1Panel 的系统信息卡把「内核版本」单列一行；后端的 platform 就是 "Linux <release>"
  { k: '内核版本', v: String(info.value.platform || '').split(' ').slice(1).join(' ') },
  { k: '架构', v: info.value.arch || '' },
  { k: 'CPU', v: info.value.cpuModel || '', mono: false },
  { k: '核心数', v: cores.value ? String(cores.value) : '' },
  { k: '负载', v: (stat.value.load || []).map((x: number) => x.toFixed(2)).join(' / ') },
  { k: '运行时长', v: stat.value.uptime ? fmtUptime(stat.value.uptime) : '' },
  { k: '解释器', v: info.value.node || '', mono: true },
  { k: '采样方式', v: stat.value.source === 'psutil' ? 'psutil' : stat.value.source ? '/proc 直读' : '' },
]);

/* ---------- 历史：持久化行 /system/history ---------- */
const RANGES = ['1h', '6h', '24h', '7d'] as const;
type RangeKey = (typeof RANGES)[number];
const histRange = ref<RangeKey>('6h');
const hist = ref<any>(null);
const histLoading = ref(true);
const histError = ref('');
const mstat = ref<any>(null);
let histSeq = 0;
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
    const net: Record<string, [number, number]> = {};
    let ar = 0;
    let at = 0;
    for (const n of s.nics || []) {
      net[n.name] = [n.rx || 0, n.tx || 0];
      ar += n.rx || 0;
      at += n.tx || 0;
    }
    const point: MonPoint = { t: Date.now(), net, all: [ar, at], ioR: s.ioRead || 0, ioW: s.ioWrite || 0 };
    monBuf.value = [...monBuf.value.slice(-(MON_CAP - 1)), point];
  } catch {
    return; // 401 时 api 层已跳转登录；网络抖动等下个周期自动恢复
  } finally {
    if (!disposed) rtLoading.value = false;
  }
}

// 服务端算速率靠的是"上一次采样的全局快照"（system_metrics.snapshot 的 _prev），
// 所以同一时刻只允许有一个 /system/stats 在飞：交叠的第二个请求 dt≈0，
// _rate 归零，曲线上就留下一个真实的 0 采样点并一直挂在 12 分钟窗口里，
// 网速/IO 芯片同时也会闪一下 0 B/s。tick 每 3s 发一次、请求偶尔超过 3s 就必然交叠。
let tickInflight: Promise<void> | null = null;
function tickSingleton(): Promise<void> {
  // 复用同一个 promise：手动刷新撞上轮询时不是"跳过"，而是等这一轮真正拿回数据，
  // 否则刷新按钮会秒回、画面却什么都没变。
  if (!tickInflight) {
    tickInflight = tick().finally(() => { tickInflight = null; });
  }
  return tickInflight;
}

async function loadHistory() {
  // 区间切换/30s 轮询/手动刷新会并发打同一条 history：两个请求的返回顺序没有保证，
  // 后发先至时画面就停在旧区间上（要等下一次轮询才纠正），而且 histXs 是按
  // histRange 格式化时间的，标签与数据会对不上。用自增序号只认最后一次。
  const seq = ++histSeq;
  histLoading.value = true;
  histError.value = '';
  try {
    const [d, st] = await Promise.all([
      req(`/system/history?range=${histRange.value}`),
      req('/system/metrics-status'),
    ]);
    if (disposed || seq !== histSeq) return;
    hist.value = d;
    mstat.value = st;
  } catch (e: any) {
    if (disposed || seq !== histSeq) return;
    hist.value = null;
    histError.value = e?.message || '历史数据获取失败';
  } finally {
    if (!disposed && seq === histSeq) histLoading.value = false;
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
    await Promise.all([tickSingleton(), loadHistory(), loadSide(), loadProcs(), probeAlerts(), loadOverview()]);
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

let n = 0;
// 后台标签页继续 3s 一跳等于白跑 systemctl/journalctl 子进程，见 useVisiblePoll
const poll = useVisiblePoll(() => {
  tickSingleton();
  if (++n % 5 === 0) { loadSide(); loadProcs(); }   // 侧栏/进程 15s，随 tick 自动重试失败态
  // 概览"未确认告警"计数 30s 一跳，但"最近告警"列表过去只在挂载时拉过一次：
  // 挂着页面时新告警进来，计数 +1 而列表不出新行，两个口径互相打脸。同频一起刷。
  if (n % 10 === 0) { loadHistory(); loadOverview(); probeAlerts(); }
}, 3000);

onMounted(() => {
  // 挂载期全部并发取数，不再串行瀑布
  api.info().then((d) => { if (!disposed) info.value = d; }).catch(() => { /* 降级为未知主机信息 */ });
  Promise.all([tickSingleton(), loadHistory(), loadSide(), loadProcs(), probeAlerts(), loadOverview()]);
  poll.start();
});

onBeforeUnmount(() => {
  disposed = true;
  poll.stop();
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
      <NSpace align="center" :size="8">
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

    <!-- 主体两列（1Panel 首页是 el-col 16/8）：左 = 概览 → 状态 → 监控，右 = 系统信息 → 应用 → 服务健康 -->
    <div class="home-grid">
      <div class="home-main">
        <!-- 概览：1Panel 的卡片高 166px，格子里"标签在上、数字在下"，数字是 link 按钮 -->
        <NCard size="small" class="ov-card">
          <template #header><span class="section-title">概览</span></template>
          <div class="ov-row">
            <div
              v-for="(it, i) in ovItems" :key="it.label" class="ov-item cp-rise" :class="{ danger: it.danger }"
              :style="`--i:${i}`" role="link" tabindex="0" :aria-label="`${it.label} ${it.value ?? '未知'}，前往${it.label}页面`"
              @click="router.push(it.to)" @keyup.enter="router.push(it.to)" @keyup.space.prevent="router.push(it.to)"
            >
              <div class="ov-label">{{ it.label }}</div>
              <div class="ov-num">{{ it.value ?? '—' }}</div>
              <div class="ov-note">{{ ov ? it.note : (ovError || '读取中') }}</div>
            </div>
          </div>
          <EmptyBox v-if="ovError" :text="`概览计数获取失败：${ovError}，30 秒轮询会自动重试`" />
        </NCard>

        <NCard size="small">
          <template #header><span class="section-title">状态</span></template>
          <template #header-extra>
            <NText depth="3" class="sfoot">每 3 秒采样</NText>
          </template>
          <div class="gauge-row">
            <StatGauge
              v-for="g in gauges" :key="g.key" :percent="g.percent" :text="g.text" :unit="g.unit"
              :label="g.label" :detail="g.detail" :color="g.color"
            />
          </div>
        </NCard>

        <NCard size="small">
          <template #header><span class="section-title">监控</span></template>
          <template #header-extra>
            <NSpace align="center" :size="8">
              <NSelect
                v-if="monMode === 'net'"
                v-model:value="monNic" size="tiny" class="nic-select" :options="nicOpts"
                aria-label="选择网卡"
              />
              <div class="cp-seg" role="group" aria-label="监控指标">
                <button type="button" :aria-pressed="monMode === 'net'" @click="monMode = 'net'">流量</button>
                <button type="button" :aria-pressed="monMode === 'io'" @click="monMode = 'io'">磁盘 IO</button>
              </div>
            </NSpace>
          </template>
          <div class="chip-row">
            <span v-for="c in monChips" :key="c.k" class="cp-chip">{{ c.k }} <b>{{ c.v }}</b></span>
            <span v-if="monNicDown" class="cp-chip down">{{ monNic }} 链路已断开，速率仅供占位</span>
            <span v-else-if="!nicList.length" class="cp-chip down">未识别到物理网卡</span>
          </div>
          <MetricChart
            :xs="monXs" :series="monSeries" height="383px" :loading="rtLoading"
            :y-fmt="rateAxisFmt" :tip-fmt="rateTipFmt" y-name="( B/s )" data-zoom legend
          />
          <NText depth="3" class="chart-foot">
            实时窗口最多 {{ MON_CAP * 3 / 60 }} 分钟（{{ MON_CAP }} 个 3 秒采样点），底部滑块可框选回看。
          </NText>
        </NCard>
      </div>

      <div class="home-aside">
        <NCard size="small">
          <template #header><span class="section-title">系统信息</span></template>
          <InfoList :items="sysItems" />
        </NCard>

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
      </div>
    </div>

    <!-- 实时：CPU + 内存双轴折线 -->
    <NCard size="small">
      <template #header><span class="section-title">实时 · CPU / 内存（3s 轮询）</span></template>
      <MetricChart :xs="rtXs" :series="rtSeries" height="220px" :loading="rtLoading"
                   :y-fmt="(v: number) => v + '%'" :y2-fmt="(v: number) => v + 'G'" />
    </NCard>

    <!-- 历史曲线（持久化行） -->
    <div class="hist-head">
      <span class="section-title">历史曲线 · 网络 / 磁盘 / 负载</span>
      <NSpace align="center" :size="8">
        <span v-if="mstat?.lastSampleTs" class="sfoot">最近采样 {{ new Date(mstat.lastSampleTs * 1000).toLocaleTimeString() }}</span>
        <NTag size="small" :bordered="false" :type="mstat?.samplerAlive ? 'success' : 'warning'">
          {{ mstat?.samplerAlive ? '采样器运行中' : '采样器未运行' }}
        </NTag>
        <div class="cp-seg" role="group" aria-label="历史区间">
          <button v-for="r in RANGES" :key="r" type="button" :aria-pressed="histRange === r" @click="pickRange(r)">{{ r }}</button>
        </div>
      </NSpace>
    </div>
    <NGrid cols="1 m:2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem span="1">
        <NCard size="small">
          <template #header><span class="section-title">网络流量（bytes/s）</span></template>
          <EmptyBox v-if="histError" :text="`历史数据获取失败：${histError}，下个周期自动重试`" />
          <EmptyBox v-else-if="histEmpty" text="暂无历史数据：需采样器运行约 30 秒后出现首点" />
          <MetricChart v-else :xs="histXs" :series="netSeries" height="200px" :loading="histLoading"
                       :y-fmt="rateAxisFmt" :tip-fmt="rateTipFmt" data-zoom legend />
        </NCard>
      </NGridItem>
      <NGridItem span="1">
        <NCard size="small">
          <template #header><span class="section-title">磁盘使用率（%）</span></template>
          <EmptyBox v-if="histError" :text="`历史数据获取失败：${histError}`" />
          <EmptyBox v-else-if="histEmpty" text="暂无历史数据：等待采样器写入首点" />
          <MetricChart v-else :xs="histXs" :series="diskSeries" height="200px" :loading="histLoading"
                       :y-fmt="(v: number) => v + '%'" data-zoom legend />
        </NCard>
      </NGridItem>
      <NGridItem span="1">
        <NCard size="small">
          <template #header><span class="section-title">磁盘 IO（bytes/s）</span></template>
          <EmptyBox v-if="histError" :text="`历史数据获取失败：${histError}`" />
          <EmptyBox v-else-if="histEmpty" text="暂无历史数据：等待采样器写入首点" />
          <MetricChart v-else :xs="histXs" :series="ioSeries" height="200px" :loading="histLoading"
                       :y-fmt="rateAxisFmt" :tip-fmt="rateTipFmt" data-zoom legend />
        </NCard>
      </NGridItem>
      <NGridItem span="1">
        <NCard size="small">
          <template #header><span class="section-title">负载与进程数</span></template>
          <EmptyBox v-if="histError" :text="`历史数据获取失败：${histError}`" />
          <EmptyBox v-else-if="histEmpty" text="暂无历史数据：等待采样器写入首点" />
          <MetricChart v-else :xs="histXs" :series="loadSeries" height="200px" :loading="histLoading" data-zoom legend />
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
.sfoot { font-size: var(--fs-2xs); color: var(--cp-text-mute); margin-top: var(--space-1); }

/* ---------- 概览条（1Panel：卡片 166px 高，标签在上、数字在下、数字是 link 按钮） ---------- */
.ov-card :deep(.n-card__content) { min-height: 106px; }
/* 概览格子：用 flex + flex-grow 而不是 grid。grid 的 auto-fit 列数和格子数（管理员 5、
   只读 4）永远除不尽，最后一段会露出容器底色，实测在 375/640/1100 档都是一块灰色空洞；
   flex-grow 让最后一行的格子自己摊满剩余宽度，任何档数、任何格子数都不留洞。
   分隔线改由格子自己画（右边 + 下边），容器补一圈同色描边，边缘那两条正好被描边盖住。 */
.ov-row {
  display: flex;
  flex-wrap: wrap;
  border: 1px solid var(--cp-line);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.ov-item {
  flex: 1 1 105px;
  background: var(--cp-bg-1);
  border-bottom: 1px solid var(--cp-line);
  padding: var(--space-3) var(--space-2);
  text-align: center;
  cursor: pointer;
  transition: background var(--dur) var(--ease);
  min-width: 0;
}
.ov-item:hover, .ov-item:focus-visible { background: var(--cp-hover); }
.ov-item + .ov-item { border-left: 1px solid var(--cp-line); }
.ov-item:focus-visible { outline: 2px solid var(--cp-brand); outline-offset: -2px; }
.ov-label { font-size: var(--fs-md); color: var(--cp-text-dim); }
.ov-num {
  margin-top: 10px;
  font-size: 18px;
  line-height: 32px;
  font-weight: 500;
  color: var(--cp-brand);
  font-variant-numeric: tabular-nums;
}
.ov-item.danger .ov-num { color: var(--cp-err); }
.ov-note { font-size: var(--fs-xs); color: var(--cp-text-mute); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ---------- 状态仪表 + 监控 / 侧栏 两列（1Panel 首页是 el-col 16/8 = 2fr/1fr） ---------- */
.home-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: var(--space-4); align-items: start; }
.home-main, .home-aside { display: flex; flex-direction: column; gap: 7px; min-width: 0; }
/* 1Panel 的状态区是 12 栅格、每环占 3（一行四个），窄屏每环占 6（一行两个） */
.gauge-row { display: grid; grid-template-columns: repeat(12, 1fr); gap: 10px; }
.gauge-row > * { grid-column: span 3; }
.chip-row { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: var(--space-3); }
.cp-chip.down {
  background: color-mix(in srgb, var(--cp-err) 9%, transparent);
  border-color: color-mix(in srgb, var(--cp-err) 42%, transparent);
  color: var(--cp-err);
}
.cp-chip.down b { color: var(--cp-err); }
.nic-select { width: 200px; }
/* 网卡选择 200px + 段选 ~120px 在 375 档放不下（卡片头内宽 301px），实测工具条折成两行、
   卡片头高到 150px。压到 140px 后两者同排一行，网卡名过长时选择框自己出省略号。 */
@media (max-width: 640px) { .nic-select { width: 140px; } }
.chart-foot { display: block; margin-top: var(--space-2); font-size: var(--fs-xs); line-height: 1.5; }
@media (max-width: 1023px) {
  .home-grid { grid-template-columns: minmax(0, 1fr); }
  .gauge-row > * { grid-column: span 6; }
}
@media (max-width: 560px) {
  .gauge-row > * { grid-column: span 12; }
}

/* ---------- 列表行 ---------- */
.hist-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--space-3); flex-wrap: wrap; margin-top: var(--space-2);
}
.hist-head .sfoot { margin-top: 0; }
/* 应用行做成 1Panel 的 h-app-card 那种带描边小卡（8px 10px 内边距、10px 圆角、
   hover 换成主色描边）；图标格按侧栏宽度缩到 38px，58px 在 320px 栏里会挤掉名字。 */
.app-row {
  display: flex; align-items: center; gap: var(--space-3);
  padding: 8px 10px; min-height: 48px;
  border: 1px solid var(--cp-border); border-radius: var(--radius-xl);
  cursor: pointer;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.app-row + .app-row { margin-top: var(--space-2); }
.app-row:hover { border-color: var(--cp-brand); background: var(--cp-hover); }
.app-name { font-size: var(--fs-md); font-weight: 600; color: var(--cp-text-strong); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
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
