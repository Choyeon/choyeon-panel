<script setup lang="ts">
import { computed, h, ref, watch } from 'vue';
import {
  NAlert, NCard, NDataTable, NGrid, NGridItem, NInput, NSelect, NSpace, NTabPane, NTabs,
  NSwitch, NTag, NText,
} from 'naive-ui';
import { NButton, tapEllipsis } from '../ui';
import { req } from '../api';
import { icon } from '../icons';
import { fmtBytes } from '../composables/useFormat';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import InfoList, { type InfoItem } from '../components/InfoList.vue';
import MetricChart from '../components/MetricChart.vue';
import { useCachedFetch } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';
import { useVisiblePoll } from '../composables/useVisiblePoll';

type JItem = { ts: string; unit: string; prio: number; text: string };
type JResp = { items: JItem[]; count: number; truncated: boolean };
type SFile = { i: number; label: string; kind: string; site: string };
type NResp = { items: string[]; count: number; source: string; files: SFile[]; truncated: boolean; hint?: string; readError?: string };
type LogOpts = {
  lines_default: number; lines_max: number; ladder: number[];
  nginx_tail_bytes: number; stats_minutes: number[]; stats_days: number[];
};
type SResp = {
  hint?: string; readError?: string; files?: SFile[]; source?: string; site?: string; file?: number;
  parsed?: number; outsideWindow?: number; malformed?: number; scannedLines?: number;
  bytesRead?: number; truncatedRead?: boolean; windowMinutes?: number;
  from?: string; to?: string; bytesOut?: number; clients?: number;
  clientsOverflow?: number; pathsOverflow?: number;
  status?: Record<string, number>;
  topCodes?: { code: string; count: number }[];
  topPaths?: { path: string; count: number; bytes: number }[];
  topClients?: { key: string; count: number }[];
  methods?: { key: string; count: number }[];
  series?: { ts: number; count: number }[];
};
type DSpan = {
  pv: number; uv: number; uvFloored?: boolean; bytes: number; e4xx: number; e5xx: number;
};
type DResp = {
  hint?: string; source?: string; site?: string; file?: number;
  days?: number; anchor?: string; from?: string; prevFrom?: string;
  rows?: { date: string; pv: number; uv: number; uvFloored: boolean; bytes: number; e4xx: number; e5xx: number }[];
  current?: DSpan;
  previous?: DSpan & { days: number; partial: boolean; reason: string };
  comparable?: boolean; deltaPvPct?: number | null;
  parsed?: number; malformed?: number; outside?: number;
  scannedLines?: number; scannedFiles?: number; totalFiles?: number;
  truncatedFiles?: number; unreadableFiles?: number;
  bytesScanned?: number; diskBytes?: number; historyFrom?: string;
  series?: { name: string; gen: number; gz: boolean; bytes: number }[];
};

const err = ref('');
const tab = ref<'journal' | 'nginx' | 'stats'>('journal');

const LEVELS = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug'];
const PRIO_CN: Record<string, string> = {
  emerg: '紧急', alert: '警戒', crit: '关键', err: '错误', warning: '注意', notice: '通知', info: '信息', debug: '调试',
};

// limit/lines 先留 0：档位和默认行数是服务端的事（见 /system/log-options），
// 页面拿到清单再回填，避免这里抄一份和后端各说各话的数字。
const jf = ref({ unit: '', since: '30min', until: '', level: '', grep: '', limit: 0 });
const nf = ref({ file: 0, lines: 0, filter: '' });
const jResult = ref<JResp | null>(null);
const nResult = ref<NResp | null>(null);
const jLoading = ref(false);
const jRefreshing = ref(false);
const nLoading = ref(false);
const follow = ref(false);
const FOLLOW_MS = 5000;

const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 260 });

// unit 候选直接取自系统服务页那份列表：本机有什么 unit 由 systemd 说，前端不写死名单。
// 两份清单都是后台取的（缓存命中时不会重打），失败只会让选择器变短、不会挡检索，
// 所以它们不进 err（err 说的是"这一次检索怎么了"），而是合成一句"这一页现在是降级态"。
const { data: svcData, error: svcError } = useCachedFetch<any[]>({
  key: () => 'services:list',
  loader: () => req('/system/services'),
  maxAge: 60000,
});
const { data: logOpts, error: logOptsError } = useCachedFetch<LogOpts>({
  key: () => 'system:logopts',
  loader: () => req('/system/log-options'),
  maxAge: 300000,
});
const degraded = computed(() => {
  const parts: string[] = [];
  if (svcError.value) parts.push(`unit 清单没取到（${(svcError.value as Error).message || '请求失败'}），unit 选择器只剩「全部 unit」，检索仍可按当前条件进行`);
  if (logOptsError.value) parts.push(`行数档位没取到（${(logOptsError.value as Error).message || '请求失败'}），「多少行」由服务端按默认值决定`);
  return parts.join('；');
});
const unitOpts = computed(() => {
  const list = (svcData.value || []).map((s: any) => ({ label: s.unit, value: s.unit }));
  return [{ label: '全部 unit', value: '' }, ...list];
});

// 面板自身的日志是排障第一入口，但 unit 名对不上就没意义：和后端 cron_service.PANEL_SERVICE 同一个值。
const PANEL_UNIT = 'choyeon-panel.service';
const PRESETS = [
  { label: '面板最近 30 分钟', patch: { unit: PANEL_UNIT, since: '30min', until: '', level: '' } },
  { label: '面板只看错误', patch: { unit: PANEL_UNIT, since: '6h', until: '', level: 'err' } },
  { label: 'nginx 最近 1 小时', patch: { unit: 'nginx.service', since: '1h', until: '', level: 'warning' } },
];
function applyPreset(p: { patch: Record<string, string> }) {
  tab.value = 'journal';
  jf.value = { ...jf.value, ...p.patch };
  void searchJournal();
}

const nginxFileOpts = computed(() =>
  (nResult.value?.files || []).map((f) => ({ label: f.label, value: f.i }))
);

// 「一次看多少行」由服务端给：档位、默认值、nginx 尾读的字节上限同出一个出处
// （backend/app/util.py 的 LOG_LINES_DEFAULT / HTTP_LOG_LINES_MAX 与 system.py 的 TAIL_BYTES）。
// 页面此前抄的是 200/500/1000/2000 + 默认 500 + 「512KB」，三个数字没有一个是自己算的，
// 上限一改就整体失真，而且失真很安静：下拉里那一档还在，服务端已经截断了。
// 这份清单的声明在上面（和 unit 清单一起，两份失败合成同一条降级提示）。
const ladder = computed(() => (logOpts.value?.ladder || []).filter((n) => Number.isInteger(n) && n >= 1));
const lineOpts = computed(() => ladder.value.map((n) => ({ label: `${n} 行`, value: n })));
const defaultLines = computed(() => {
  const d = Number(logOpts.value?.lines_default) || 0;
  if (ladder.value.includes(d)) return d;
  return ladder.value[0] || 0;
});
const tailKb = computed(() => Math.round(Number(logOpts.value?.nginx_tail_bytes) / 1024) || 0);
const tailNote = computed(() => {
  const only = '只读白名单目录里的 access.log / error.log（CP_LOG_ROOTS 加 CP_FILE_ROOTS，和统计页签同一份清单）。';
  return tailKb.value ? `服务端最多读末尾 ${tailKb.value}KB，且${only}` : only;
});
watch(defaultLines, (v) => {
  if (!v) return;
  if (!jf.value.limit) jf.value.limit = v;
  if (!nf.value.lines) nf.value.lines = v;
}, { immediate: true });

// 手动检索/跟随刷新共用 jResult：回车连按两次会并发两个 journalctl（每个都是
// 全量扫描），而两条响应谁先落地不可控——最后画出来的可能是上一次那条查询的结果。
// 认序号保证"最新那次请求说了算"，和被取代的旧响应不再写画面、也不再解 loading。
let journalSeq = 0;
async function searchJournal(opts: { silent?: boolean } = {}) {
  // silent：跟随模式下的定时刷新。这时候不能盖 loading 遮罩，
  // 否则每 5 秒画面闪一次，而且上一轮还没回来时不能并发再打（会互相覆盖结果）。
  if (opts.silent) {
    if (jRefreshing.value || jLoading.value) return;
    jRefreshing.value = true;
  } else {
    if (jLoading.value) return;
    jLoading.value = true;
  }
  const seq = ++journalSeq;
  err.value = '';
  const q = new URLSearchParams();
  Object.entries(jf.value).forEach(([k, v]) => { if (v !== '' && v !== null) q.set(k, String(v)); });
  try {
    const r = await req(`/system/logs?${q.toString()}`, { timeout: 60000 });
    if (seq !== journalSeq) return;
    jResult.value = r;
  } catch (e: any) {
    if (seq !== journalSeq) return;
    err.value = e.message || String(e);
    if (!opts.silent) jResult.value = null;
  } finally {
    if (seq === journalSeq) {
      jLoading.value = false;
      jRefreshing.value = false;
    }
  }
}

// 跟随用轮询而不是 SSE：SSE 那条 -f 流是全量转发、不按关键词/级别过滤，
// 挂着不动就占一个 journalctl 进程；轮询复用同一个检索接口，条件所见即所得。
// 标签页隐藏时由 useVisiblePoll 挂起，回到前台立刻补一次，不会错过最新几行。
const followPoll = useVisiblePoll(() => { void searchJournal({ silent: true }); }, FOLLOW_MS);
function toggleFollow(v: boolean) {
  follow.value = v;
  followPoll.stop();
  if (!v) return;
  void searchJournal({ silent: true });
  followPoll.start();
}

async function tailNginx() {
  if (nLoading.value) return;
  nLoading.value = true;
  err.value = '';
  const q = new URLSearchParams({ file: String(nf.value.file), lines: String(nf.value.lines) });
  if (nf.value.filter) q.set('filter', nf.value.filter);
  try {
    nResult.value = await req(`/system/nginx-log?${q.toString()}`, { timeout: 60000 });
  } catch (e: any) {
    err.value = e.message || String(e);
    nResult.value = null;
  } finally {
    nLoading.value = false;
  }
}

// —— 访问统计（只读：一次有界尾读 + 内存聚合，服务端不返回原始日志行）——
const sf = ref({ file: 0, minutes: 0 });
const sResult = ref<SResp | null>(null);
const sLoading = ref(false);

// 下拉只列"能统计的"来源：序号空间与尾读页签是同一份（服务端那份排序稳定的列表），
// 但错误日志没有请求行，选它只能得到一个 400。
const statsFileOpts = computed(() => (sResult.value?.files || [])
  .filter((f) => f.kind === 'access')
  .map((f) => ({ label: f.label, value: f.i })));

// 窗口档位来自 /system/log-options（后端 access_stats.WINDOW_LADDER），页面不抄数字：
// 抄一份 15/30/60 就会在上限改动后安静失真——下拉里还有 24 小时，服务端已经截到 720。
const minuteLadder = computed(() =>
  (logOpts.value?.stats_minutes || []).filter((n) => Number.isInteger(n) && n >= 1));
const minuteOpts = computed(() => minuteLadder.value.map((n) => ({
  label: n % 60 === 0 ? `${n / 60} 小时` : `${n} 分钟`,
  value: n,
})));
const defaultMinutes = computed(() => minuteLadder.value.find((n) => n === 60) || minuteLadder.value[0] || 0);
watch(defaultMinutes, (v) => { if (v && !sf.value.minutes) sf.value.minutes = v; });

// 同一次统计只允许一份在路上：连点两下会并发两次全量尾读，而先回来的那个
// 会盖掉后回来的那个（和 journal 那条同一个病），所以认序号。
let statsSeq = 0;
async function runStats() {
  if (sLoading.value) return;
  sLoading.value = true;
  const seq = ++statsSeq;
  err.value = '';
  const q = new URLSearchParams();
  // 第一枪不带 file：让服务端挑一个「能统计的」来源并回写序号。
  // 写死 0 的话，撞上排序靠前的 error.log 就是「点了统计却报错」。
  if (typeof sResult.value?.file === 'number') q.set('file', String(sf.value.file));
  if (sf.value.minutes) q.set('minutes', String(sf.value.minutes));
  try {
    const r: SResp = await req(`/system/nginx-stats?${q.toString()}`, { timeout: 60000 });
    if (seq !== statsSeq) return;
    sResult.value = r;
    if (typeof r.file === 'number') sf.value.file = r.file;
  } catch (e: any) {
    if (seq !== statsSeq) return;
    err.value = e.message || String(e);
  } finally {
    if (seq === statsSeq) sLoading.value = false;
  }
}

// 进页签自动跑一次：这一页没有"还没查"和"查了是空"两种状态的区别，
// 留白只会让人以为统计坏了（和访问日志页签要手点不一样，那边一次尾读几十行是有代价的）。
watch(tab, (t) => { if (t === 'stats' && !sResult.value && !sLoading.value) void runStats(); });

const STATUS_CN: Record<string, string> = {
  '0xx': '无效/其他', '1xx': '信息 1xx', '2xx': '成功 2xx',
  '3xx': '重定向 3xx', '4xx': '客户端 4xx', '5xx': '服务端 5xx',
};
// 类别由服务端给（0xx-5xx 全量），页面只负责排序与配色，不再抄一份键名。
const statusRows = computed(() => {
  const total = sResult.value?.parsed || 0;
  return Object.entries(sResult.value?.status || {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => {
      const pct = total ? Math.round((n / total) * 100) : 0;
      return { k, label: STATUS_CN[k] || k, n, pct, w: pct > 0 ? Math.max(2, pct) : 0 };
    });
});
function statusColor(k: string) {
  if (k === '5xx') return 'var(--cp-err)';
  if (k === '4xx') return 'var(--cp-warn)';
  if (k === '2xx') return 'var(--cp-ok)';
  return 'var(--cp-text-mute)';
}

// 曲线刻度按 UTC 标：from/to 与窗口边界在服务端就是 UTC，
// 用浏览器时区画横轴会让「16:00 那一分钟」在两个时区的屏幕上指不同的请求。
const utcHM = (ts: number) => new Date(ts * 1000).toISOString().slice(11, 16);
const statXs = computed(() => (sResult.value?.series || []).map((p) => utcHM(p.ts)));
const statChart = computed(() => [{
  name: '请求数',
  color: '--cp-brand',
  data: (sResult.value?.series || []).map((p) => p.count),
  area: true,
  straight: true,
}]);

const statsInfo = computed<InfoItem[]>(() => {
  const d = sResult.value;
  if (!d || !d.source) return [];
  const rows: InfoItem[] = [
    { k: '来源', v: d.source, mono: true },
    { k: '站点', v: d.site || '-' },
    { k: '窗口', v: `${d.windowMinutes} 分钟 · ${d.from || '?'} → ${d.to || '?'}（UTC）` },
    { k: '请求数', v: `${d.parsed} 次` },
    { k: '独立客户端', v: `${d.clients} 个${d.clientsOverflow ? `（另有 ${d.clientsOverflow} 个未上榜）` : ''}` },
    { k: '出站流量', v: fmtBytes(d.bytesOut || 0) },
    {
      k: '扫描范围',
      v: `${d.scannedLines} 行 / ${Math.round((d.bytesRead || 0) / 1024)}KB`
        + `${d.truncatedRead ? '（日志比这更大，只读了尾部）' : ''}`,
    },
  ];
  if (d.outsideWindow) rows.push({ k: '窗口外', v: `${d.outsideWindow} 行更早，未计入本次窗口` });
  if (d.malformed) rows.push({ k: '解析不了', v: `${d.malformed} 行（不是 common/combined 格式，或时间戳读不出）` });
  if (d.pathsOverflow) rows.push({ k: '路径溢出', v: `${d.pathsOverflow} 个唯一路径并进「…其他」` });
  return rows;
});

// —— 按天流量与环比（读 logrotate 轮替出来的世代，比分钟窗口重，所以这一步要人点）——
const df = ref({ days: 0 });
const dResult = ref<DResp | null>(null);
const dLoading = ref(false);

// 天数档位同样来自 /system/log-options（后端 access_stats.DAY_LADDER）。这里抄一份 3/7/14/30
// 的后果和分钟窗口那版一样：服务端把上限放宽后，下拉里还是老档位，而校验早已放行。
const dayLadder = computed(() => (logOpts.value?.stats_days || []).filter((n) => Number.isInteger(n) && n >= 1));
const dayOpts = computed(() => dayLadder.value.map((n) => ({ label: `${n} 天`, value: n })));
const defaultDays = computed(() => dayLadder.value.find((n) => n === 7) || dayLadder.value[0] || 0);
watch(defaultDays, (v) => { if (v && !df.value.days) df.value.days = v; });

// 分钟统计和按天共用服务端那份清单（同一个序号空间），所以"有没有可统计的来源"也只认一次：
// 按天按钮在跑过一次分钟统计（哪怕答案是"白名单里没有访问日志"）之前是灰的，
// 灰的原因是还不知道有哪些来源，而不是按天这个功能坏了。
const hasAccessSource = computed(() => (sResult.value?.files || []).some((f) => f.kind === 'access'));

// 一次按天回看最多读 31 份世代（含解压），比分钟窗口的单份尾读重得多，
// 所以并发同样认序号：连点两下会打两份全量扫描，而先回来的会盖掉后回来的。
let dailySeq = 0;
async function runDaily() {
  if (dLoading.value) return;
  dLoading.value = true;
  const seq = ++dailySeq;
  err.value = '';
  const q = new URLSearchParams();
  // 序号空间与分钟窗口是同一份（服务端 _pick_access_source），所以这里直接跟随下拉当前选的号；
  // 一次统计都没跑过时不带 file，交给服务端挑一个「能统计的」来源。
  if (typeof sResult.value?.file === 'number') q.set('file', String(sf.value.file));
  if (df.value.days) q.set('days', String(df.value.days));
  try {
    const r: DResp = await req(`/system/nginx-daily?${q.toString()}`, { timeout: 120000 });
    if (seq !== dailySeq) return;
    dResult.value = r;
  } catch (e: any) {
    if (seq !== dailySeq) return;
    err.value = e.message || String(e);
  } finally {
    if (seq === dailySeq) dLoading.value = false;
  }
}

// 环比的判词只照抄服务端给的 `comparable` 与 `previous.reason`：
// 「上一周期读满了吗」是扫描侧的事实，页面拿 rows 自己拼只会拼出一个假的涨跌。
const dailyVerdict = computed(() => {
  const d = dResult.value;
  if (!d || !d.current || !d.source) return null;
  if (d.comparable) {
    const pct = Number(d.deltaPvPct) || 0;
    const dir = pct > 0 ? '上升' : pct < 0 ? '下降' : '持平';
    return {
      tone: pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat',
      text: `最近 ${d.days} 天 ${d.current.pv} 次请求，比上一周期（${d.previous?.pv ?? 0} 次）${dir}${pct === 0 ? '' : ` ${Math.abs(pct)}%`}`,
    };
  }
  const r = d.previous?.reason || 'history';
  const why = r === 'no-data'
    ? '这些日志里读不出可用的时间戳'
    : r === 'clipped'
      ? `上一周期那几份有被截断或读不到的（截断 ${d.truncatedFiles ?? 0} 份、读不到 ${d.unreadableFiles ?? 0} 份）`
      : `日志只到 ${d.historyFrom || '?'}，往前凑不满一个周期（上一周期只留住 ${d.previous?.days ?? 0} 天）`;
  return { tone: 'none', text: `不做环比：${why}` };
});

// 换了下拉但没重取：这张表还是上一个来源的数字，得说清楚，不能让它顶着新选择的标题装作刚算过。
const dailyStale = computed(() => {
  const d = dResult.value;
  if (!d || typeof d.file !== 'number' || d.file === sf.value.file) return '';
  return '来源已经换了，下面这份还是上一个来源的数（点「按天回看」重取）';
});

const dailyInfo = computed<InfoItem[]>(() => {
  const d = dResult.value;
  if (!d || !d.source) return [];
  const rows: InfoItem[] = [
    { k: '来源', v: d.source, mono: true },
    { k: '站点', v: d.site || '-' },
    { k: '区间', v: `${d.days} 天 · ${d.from || '?'} → ${d.anchor || '?'}` },
    { k: '请求数', v: `${d.current?.pv ?? 0} 次` },
    { k: '独立客户端', v: `${d.current?.uv ?? 0} 个${d.current?.uvFloored ? '（到封顶值，实际更多）' : ''}` },
    { k: '出站流量', v: fmtBytes(d.current?.bytes || 0) },
    { k: '4xx / 5xx', v: `${d.current?.e4xx ?? 0} / ${d.current?.e5xx ?? 0}` },
    {
      k: '回看',
      v: `读了 ${d.scannedFiles ?? 0}/${d.totalFiles ?? 0} 份世代、${d.scannedLines ?? 0} 行`
        + `（盘上共 ${fmtBytes(d.diskBytes || 0)}）${d.historyFrom ? `，最早到 ${d.historyFrom}` : ''}`,
    },
  ];
  if (d.truncatedFiles) rows.push({ k: '读到底的世代', v: `${d.truncatedFiles} 份比单次读量还大，只算了读进来的那部分` });
  if (d.unreadableFiles) rows.push({ k: '这次读不到', v: `${d.unreadableFiles} 份（多半正好赶上 logrotate 改名）` });
  if (d.outside) rows.push({ k: '窗口外', v: `${d.outside} 行比日历天窗口更早，未计入` });
  if (d.malformed) rows.push({ k: '解析不了', v: `${d.malformed} 行（不是 common/combined 格式，或时间戳读不出）` });
  return rows;
});

type DRow = NonNullable<DResp['rows']>[number];
const dailyXs = computed(() => (dResult.value?.rows || []).map((r) => r.date.slice(5)));
const dailyChart = computed(() => {
  const rows = dResult.value?.rows || [];
  return [
    { name: '请求数', color: '--cp-brand', data: rows.map((r) => r.pv), area: true, straight: true },
    { name: '独立客户端', color: '--cp-ok', data: rows.map((r) => r.uv), axis: 1, thin: true },
  ];
});
const dCols: any[] = [
  { title: '日期', key: 'date', minWidth: 96, render: (r: DRow) => h('span', { class: 'mono-dim' }, r.date) },
  { title: '请求', key: 'pv', width: 76, align: 'right' },
  {
    // 封顶那天如实写「2000+」：写成 2000 会让人以为客户端数刚好摸到上限。
    title: '独立客户端', key: 'uv', width: 100, align: 'right',
    render: (r: DRow) => (r.uvFloored ? `${r.uv}+` : String(r.uv)),
  },
  { title: '下行', key: 'bytes', width: 92, align: 'right', render: (r: DRow) => fmtBytes(r.bytes) },
  { title: '4xx', key: 'e4xx', width: 66, align: 'right' },
  { title: '5xx', key: 'e5xx', width: 66, align: 'right' },
];

const pCols: any[] = [
  {
    title: '路径（不含查询串）', key: 'path', minWidth: 200, ellipsis: tapEllipsis,
    render: (r: { path: string }) => h('span', { class: 'mono-dim', title: r.path }, r.path),
  },
  { title: '次数', key: 'count', width: 74, align: 'right' },
  { title: '字节', key: 'bytes', width: 92, align: 'right', render: (r: { bytes: number }) => fmtBytes(r.bytes) },
];
const cCols: any[] = [
  {
    title: '客户端', key: 'key', minWidth: 130, ellipsis: tapEllipsis,
    render: (r: { key: string }) => h('span', { class: 'mono-dim', title: r.key }, r.key),
  },
  { title: '次数', key: 'count', width: 74, align: 'right' },
];

// 命中高亮只做纯文本切分：日志内容里可能带 < > 与口令片段，走 v-html 既不安全也没必要。
function highlight(line: string, needle: string) {
  const k = (needle || '').trim().toLowerCase();
  if (!k) return line;
  const out: any[] = [];
  const low = line.toLowerCase();
  let i = 0;
  let guard = 0;
  while (i < line.length && guard < 400) {
    const at = low.indexOf(k, i);
    if (at < 0) break;
    guard++;
    if (at > i) out.push(line.slice(i, at));
    out.push(h('mark', { class: 'lg-hit' }, line.slice(at, at + k.length)));
    i = at + k.length;
  }
  out.push(line.slice(i));
  return out;
}

function prioTag(p: number) {
  // journal 的 PRIORITY 就是 syslog 级别数字（0 最严重）；解析不出来的按未知处理，不猜。
  const name = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug'][p];
  const label = name ? PRIO_CN[name] : '未知';
  const type = p < 0 ? 'default' : p <= 3 ? 'error' : p === 4 ? 'warning' : 'success';
  return h(NTag, { size: 'tiny', bordered: false, type: type as any }, { default: () => label });
}

const jCols: any[] = [
  // 后端 _journal_items 用 time.localtime 格式化，和下方 since/until 的「按服务器本地时区解析」
  // 是同一套口径；这里如果只写「时间」，用户会拿它和审计页的 UTC 列对表。
  { title: '时间（本地）', key: 'ts', width: 158, render: (r: JItem) => h('span', { class: 'mono-dim', title: r.ts }, r.ts || '—') },
  {
    title: '来源', key: 'unit', width: 180,
    render: (r: JItem) => h('span', { class: 'mono-dim cp-break', style: { fontSize: 'var(--fs-xs)' } }, r.unit || '—'),
  },
  { title: '级别', key: 'prio', width: 78, render: (r: JItem) => prioTag(r.prio) },
  {
    title: '内容', key: 'text', minWidth: 420,
    render: (r: JItem) => h('span', { class: 'mono lg-text' }, highlight(r.text, jf.value.grep)),
  },
];

const nCols: any[] = [
  {
    title: '日志行', key: 'line', minWidth: 900,
    render: (r: { line: string }) => h('span', { class: 'mono lg-text' }, highlight(r.line, nf.value.filter)),
  },
];

// 虚拟滚动要求稳定 row-key：journal 行本身没有 id，同一份结果里序号是稳定的，
// 用数组下标当 key 会让滚动时行内容错位复用。
const jRows = computed(() => (jResult.value?.items || []).map((it, i) => ({ ...it, i })));
const nRows = computed(() => (nResult.value?.items || []).map((line, i) => ({ i, line })));

// 空表格里那一格写什么：读不到、没命中、还没读，是三件事，写成一句就有一句是谎话。
// "这段日志里没有命中行"尤其不能顶替"读不到"——它会把权限问题说成日志内容问题。
const nEmptyText = computed(() => {
  if (nResult.value?.readError) return '这个来源读不到，表格里没有行不是因为没有请求，是因为没读进来。';
  if (nResult.value) return '这段日志里没有命中行（换个过滤词，或加大尾读行数）';
  return '点「读取末尾」取一份日志';
});
</script>

<template>
  <div>
    <PageHeader title="日志中心" sub="journal 按 unit/时间/级别/关键词检索，访问日志 tail 白名单目录里的文件，访问统计把一次有界尾读聚成流量榜，跟随模式按同样条件定时重查">
      <template #actions>
        <NText class="cell-sub" style="font-size: var(--fs-xs)">只读查询，不写入任何数据</NText>
      </template>
    </PageHeader>

    <NAlert v-if="err" type="error" :bordered="false" size="small" role="alert" style="margin-bottom: var(--space-3)">{{ err }}</NAlert>
    <NAlert v-if="degraded" type="warning" :bordered="false" size="small" role="status" style="margin-bottom: var(--space-3)">{{ degraded }}</NAlert>

    <NTabs v-model:value="tab" type="line" size="small" display-directive="show:lazy" class="lg-tabs">
      <NTabPane name="journal" tab="系统日志检索">
        <NSpace :size="8" wrap align="end" style="margin-bottom: var(--space-3)">
          <NSelect
            :value="jf.unit" size="small" style="width: 210px; max-width: 100%" filterable aria-label="按 unit 过滤"
            :options="unitOpts" placeholder="全部 unit"
            @update:value="(v: string) => { jf.unit = v; }"
          />
          <NInput :value="jf.since" size="small" style="width: 152px; max-width: 100%" placeholder="起始 30min/6h" aria-label="起始时间" @update:value="(v: string) => { jf.since = v; }" />
          <NInput :value="jf.until" size="small" style="width: 152px; max-width: 100%" placeholder="结束（留空=现在）" aria-label="结束时间" @update:value="(v: string) => { jf.until = v; }" />
          <NSelect
            :value="jf.level" size="small" style="width: 132px; max-width: 100%" clearable aria-label="按级别过滤" placeholder="全部级别"
            :options="LEVELS.map((l) => ({ label: `${PRIO_CN[l] || l} (${l})`, value: l }))"
            @update:value="(v: string) => { jf.level = v || ''; }"
          />
          <NInput :value="jf.grep" size="small" style="width: 220px; max-width: 100%" placeholder="关键词（PCRE 正则）" aria-label="关键词" @update:value="(v: string) => { jf.grep = v; }" @keyup.enter="searchJournal()" />
          <NSelect
            :value="jf.limit || null" size="small" style="width: 122px; max-width: 100%" aria-label="返回行数"
            :options="lineOpts" placeholder="行数"
            @update:value="(v: number) => { jf.limit = v; }"
          />
          <NButton size="small" type="primary" :loading="jLoading || jRefreshing" :disabled="jLoading" :icon="icon('SearchOutline', 14)" @click="searchJournal()">检索</NButton>
          <span class="lg-follow">
            <NSwitch size="small" :value="follow" aria-label="跟随最新日志" @update:value="toggleFollow" />
            <NText class="cell-sub" style="font-size: var(--fs-xs)">跟随（每 {{ FOLLOW_MS / 1000 }} 秒按同样条件重查）</NText>
          </span>
        </NSpace>
        <NSpace :size="8" wrap style="margin-bottom: var(--space-3)">
          <NText class="cell-sub" style="font-size: var(--fs-xs); align-self: center">快捷入口：</NText>
          <NButton v-for="p in PRESETS" :key="p.label" size="small" tertiary :icon="icon('DocumentTextOutline', 12)" @click="applyPreset(p)">{{ p.label }}</NButton>
        </NSpace>
        <div v-if="jResult" class="cp-help" style="margin-bottom: var(--space-2)">
          命中 {{ jResult.count }} 行{{ jResult.truncated ? '（已达上限，结果从新到旧截断，请缩小时间窗或加关键词）' : '' }}；
          时间写法支持 30min / 6h / 2d 或 2026-10-09 10:00:00，按服务器本地时区解析。
        </div>
        <NDataTable
          ref="tableAnchor"
          :columns="jCols" :data="jRows" size="small" :bordered="false" :loading="jLoading"
          :scroll-x="'auto'" :max-height="tableH" :virtual-scroll="true"
          :row-key="(r: any) => r.i"
        >
          <template #empty><EmptyBox :text="jResult ? '这一轮检索没有命中，试试放宽时间窗或去掉关键词' : '设置条件后点「检索」'" /></template>
        </NDataTable>
      </NTabPane>

      <NTabPane name="nginx" tab="访问/错误日志">
        <NSpace :size="8" wrap align="end" style="margin-bottom: var(--space-3)">
          <!-- 两个下拉的初值都是 0，而 0 在没有对应选项时会被 naive 直接画成"0"：
               候选文件还没取、行数清单还没回来时，框里是一个孤零零的数字而不是提示语。
               文件那个 0 是合法序号（第一个候选），所以要按"有没有候选"判，不能按真假判。 -->
          <NSelect
            :value="nginxFileOpts.length ? nf.file : null" size="small" style="width: 260px; max-width: 100%" :options="nginxFileOpts" :disabled="!nginxFileOpts.length"
            filterable placeholder="先取一次候选文件" aria-label="日志文件"
            @update:value="(v: number) => { nf.file = v; }"
          />
          <NInput :value="nf.filter" size="small" style="width: 220px; max-width: 100%" placeholder="过滤词（子串，不区分大小写）" aria-label="过滤词" @update:value="(v: string) => { nf.filter = v; }" @keyup.enter="tailNginx" />
          <NSelect
            :value="nf.lines || null" size="small" style="width: 122px; max-width: 100%" aria-label="尾读行数"
            :options="lineOpts" placeholder="行数"
            @update:value="(v: number) => { nf.lines = v; }"
          />
          <NButton size="small" type="primary" :loading="nLoading" :disabled="nLoading" :icon="icon('SearchOutline', 14)" @click="tailNginx">读取末尾</NButton>
        </NSpace>
        <NAlert v-if="nResult?.readError" type="warning" :bordered="false" size="small">{{ nResult.source }}：{{ nResult.readError }}</NAlert>
        <NAlert v-else-if="nResult?.hint" type="info" :bordered="false" size="small">{{ nResult.hint }}</NAlert>
        <div v-else-if="nResult" class="cp-help" style="margin-bottom: var(--space-2)">
          {{ nResult.source }} · 返回 {{ nResult.count }} 行{{ nResult.truncated ? '（已到上限）' : '' }}；
          {{ tailNote }}
        </div>
        <NDataTable
          :columns="nCols" :data="nRows" size="small" :bordered="false" :loading="nLoading"
          :scroll-x="'auto'" :max-height="tableH" :virtual-scroll="true" class="cp-tbl tbl-log"
          :row-key="(r: { i: number }) => r.i"
        >
          <template #empty><EmptyBox :text="nEmptyText" /></template>
        </NDataTable>
      </NTabPane>

      <NTabPane name="stats" tab="访问统计">
        <NSpace :size="8" wrap align="end" style="margin-bottom: var(--space-3)">
          <NSelect
            :value="statsFileOpts.length ? sf.file : null" size="small" style="width: 260px; max-width: 100%"
            :options="statsFileOpts" :disabled="!statsFileOpts.length" filterable
            placeholder="先跑一次取来源清单" aria-label="统计来源"
            @update:value="(v: number) => { sf.file = v; }"
          />
          <NSelect
            :value="sf.minutes || null" size="small" style="width: 132px; max-width: 100%"
            :options="minuteOpts" placeholder="窗口" aria-label="统计窗口"
            @update:value="(v: number) => { sf.minutes = v; }"
          />
          <NButton size="small" type="primary" :loading="sLoading" :disabled="sLoading" :icon="icon('SearchOutline', 14)" @click="runStats">统计</NButton>
          <NText class="cell-sub cp-break" style="font-size: var(--fs-xs); align-self: center">
            只读：解析访问日志的请求行，不返回原始日志行
          </NText>
        </NSpace>

        <NAlert v-if="sResult?.readError" type="warning" :bordered="false" size="small">{{ sResult.source }}：{{ sResult.readError }}</NAlert>
        <NAlert v-else-if="sResult?.hint" type="info" :bordered="false" size="small">{{ sResult.hint }}</NAlert>
        <EmptyBox v-else-if="!sResult && !sLoading" text="这一页会自己跑一次；也可以换来源或窗口后点「统计」" />

        <template v-else-if="sResult">
          <div v-if="!sResult.parsed" class="cp-help" style="margin-bottom: var(--space-3)">
            这个窗口里没有解析出请求行{{ sResult.malformed ? `（读到 ${sResult.malformed} 行都不是 common/combined 格式）` : '' }}。
            先把站点的 access.log 开起来，或把窗口放宽一点。
          </div>
          <NGrid cols="1 m:2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive style="margin-bottom: var(--space-4)">
            <NGridItem span="1 m:1">
              <NCard size="small">
                <template #header><span class="section-title">统计口径</span></template>
                <InfoList :items="statsInfo" />
              </NCard>
            </NGridItem>
            <NGridItem span="1 m:1">
              <NCard size="small">
                <template #header><span class="section-title">状态码分布</span></template>
                <div v-if="!statusRows.length" class="cp-help">这个窗口里没有可统计的响应。</div>
                <div v-else>
                  <div v-for="row in statusRows" :key="row.k" class="ls-row">
                    <span class="ls-k">{{ row.label }}</span>
                    <span class="ls-bar"><i :style="{ width: row.w + '%', background: statusColor(row.k) }" /></span>
                    <span class="ls-n">{{ row.n }} · {{ row.pct }}%</span>
                  </div>
                  <div class="chip-row" style="margin-top: var(--space-3)">
                    <span v-for="c in sResult.topCodes || []" :key="c.code" class="cp-chip">{{ c.code }} <b>{{ c.count }}</b></span>
                  </div>
                  <div class="chip-row" style="margin-top: var(--space-2)">
                    <span v-for="m in sResult.methods || []" :key="m.key" class="cp-chip">{{ m.key }} <b>{{ m.count }}</b></span>
                  </div>
                </div>
              </NCard>
            </NGridItem>
          </NGrid>

          <NCard size="small" style="margin-bottom: var(--space-4)">
            <template #header><span class="section-title">每分钟请求数（UTC）</span></template>
            <MetricChart :xs="statXs" :series="statChart" height="200px" :loading="sLoading" y-name="( 次/分钟 )" />
          </NCard>

          <NGrid cols="1 m:2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
            <NGridItem span="1 m:1">
              <NCard size="small">
                <template #header><span class="section-title">TOP 路径</span></template>
                <NDataTable
                  :columns="pCols" :data="sResult.topPaths || []" size="small" :bordered="false"
                  :scroll-x="'auto'" :max-height="tableH"
                >
                  <template #empty><EmptyBox text="这个窗口里没有请求行" /></template>
                </NDataTable>
              </NCard>
            </NGridItem>
            <NGridItem span="1 m:1">
              <NCard size="small">
                <template #header><span class="section-title">TOP 客户端</span></template>
                <NDataTable
                  :columns="cCols" :data="sResult.topClients || []" size="small" :bordered="false"
                  :scroll-x="'auto'" :max-height="tableH"
                >
                  <template #empty><EmptyBox text="这个窗口里没有请求行" /></template>
                </NDataTable>
              </NCard>
            </NGridItem>
          </NGrid>
        </template>

        <NCard size="small" class="lg-daily">
          <template #header><span class="section-title">按天流量与环比</span></template>
          <NSpace :size="8" wrap align="center" style="margin-bottom: var(--space-3)">
            <NSelect
              :value="df.days || null" size="small" style="width: 104px; max-width: 100%"
              :options="dayOpts" placeholder="天数" aria-label="回看天数"
              @update:value="(v: number) => { df.days = v; }"
            />
            <NButton
              size="small" :loading="dLoading" :disabled="dLoading || !hasAccessSource"
              :icon="icon('SearchOutline', 14)" @click="runDaily"
            >按天回看</NButton>
            <NText class="cell-sub cp-break" style="font-size: var(--fs-xs)">
              {{ hasAccessSource
                ? '按天要读轮替出来的历史世代（含 .gz），比分钟窗口慢，所以这一步不自动跑'
                : '先点上面的「统计」拿到来源清单——这一页还不知道有哪些访问日志可回看' }}
            </NText>
          </NSpace>

          <NAlert v-if="dResult?.hint" type="info" :bordered="false" size="small">{{ dResult.hint }}</NAlert>
          <template v-else-if="dResult && dResult.source">
            <div v-if="dailyStale" class="cp-help" style="margin-bottom: var(--space-2)">{{ dailyStale }}</div>
            <div v-if="dailyVerdict" class="lg-verdict" :class="`t-${dailyVerdict.tone}`">
              {{ dailyVerdict.text }}
            </div>
            <NGrid cols="1 m:2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
              <NGridItem span="1 m:1">
                <div class="lg-sub">回看口径</div>
                <InfoList :items="dailyInfo" />
              </NGridItem>
              <NGridItem span="1 m:1">
                <div class="lg-sub">按天请求数与独立客户端</div>
                <MetricChart :xs="dailyXs" :series="dailyChart" height="200px" :loading="dLoading" legend />
              </NGridItem>
            </NGrid>
            <div class="lg-sub" style="margin-top: var(--space-4)">按天明细</div>
            <NDataTable
              :columns="dCols" :data="dResult.rows || []" size="small" :bordered="false"
              :scroll-x="'auto'" :max-height="tableH" :row-key="(r: { date: string }) => r.date"
            >
              <template #empty><EmptyBox text="这些世代里没有落在窗口内的请求行" /></template>
            </NDataTable>
          </template>
          <EmptyBox v-else-if="!dLoading" text="换好来源与天数后点「按天回看」" />
        </NCard>
      </NTabPane>
    </NTabs>
  </div>
</template>

<style scoped>
.lg-tabs :deep(.n-tabs-nav) { margin-bottom: var(--space-3); }
.lg-follow { display: inline-flex; align-items: center; gap: 6px; padding-bottom: 4px; }
/* h() 生成的单元格拿不到本组件的 data-v 作用域（scoped 纯 class 规则一条都不命中），
必须从模板上的类走 :deep。 这两条一旦失效，日志会丢掉 pre-wrap（缩进与换行被折叠成一行）、
   grep 命中也变成看不见的高亮——两个都是静默的。 */
:deep(.lg-text) { font-size: var(--fs-2xs); line-height: 1.65; word-break: break-all; white-space: pre-wrap; }
:deep(.lg-hit) { background: var(--cp-code-hl); color: inherit; border-radius: 3px; padding: 0 1px; }
/* 状态码分布：窄屏上标签列用 em 而不是 px（6.5em 在最小档也不顶穿），
   条本身走 flex + min-width:0，宽度百分比只作用于条，不作用于卡片。 */
.chip-row { display: flex; flex-wrap: wrap; gap: 8px; }
.ls-row { display: flex; align-items: center; gap: var(--space-2); min-width: 0; }
.ls-row + .ls-row { margin-top: 7px; }
.ls-k {
  flex: 0 0 6.5em; font-size: var(--fs-xs); color: var(--cp-text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ls-bar {
  flex: 1 1 auto; min-width: 0; height: 8px; border-radius: 4px;
  background: var(--cp-bg-soft); overflow: hidden;
}
.ls-bar > i { display: block; height: 100%; border-radius: 4px; }
.ls-n {
  flex: 0 0 auto; font-size: var(--fs-xs); color: var(--cp-text-dim); white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
/* 按天回看自成一块（它读的是轮替出来的世代，代价和分钟窗口不是一个量级）。
   判词紧贴工具条：这一屏最要紧的是"比上一周期涨还是跌、还是根本没比成"，
   要人滚到表格里才发现没环比，等于把诚实的结论藏起来了。 */
.lg-daily { margin-top: var(--space-4); }
.lg-sub { font-size: var(--fs-xs); color: var(--cp-text-dim); margin-bottom: var(--space-2); }
.lg-verdict {
  display: inline-block; margin-bottom: var(--space-3); padding: 4px 10px;
  border-radius: 8px; background: var(--cp-bg-soft);
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere;
}
.lg-verdict.t-up { color: var(--cp-brand); }
.lg-verdict.t-down { color: var(--cp-text); }
.lg-verdict.t-flat { color: var(--cp-text-dim); }
.lg-verdict.t-none { color: var(--cp-warn); }
</style>
