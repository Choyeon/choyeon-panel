<script setup lang="ts">
import { computed, h, onBeforeUnmount, ref } from 'vue';
import {
  NAlert, NDataTable, NInput, NSelect, NSpace, NTabPane, NTabs, NSwitch, NTag, NText,
} from 'naive-ui';
import { NButton } from '../ui';
import { req } from '../api';
import { icon } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { useCachedFetch } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';

type JItem = { ts: string; unit: string; prio: number; text: string };
type JResp = { items: JItem[]; count: number; truncated: boolean };
type NResp = { items: string[]; count: number; source: string; files: { i: number; label: string }[]; truncated: boolean; hint?: string };

const err = ref('');
const tab = ref<'journal' | 'nginx'>('journal');

const LEVELS = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug'];
const PRIO_CN: Record<string, string> = {
  emerg: '紧急', alert: '警戒', crit: '关键', err: '错误', warning: '注意', notice: '通知', info: '信息', debug: '调试',
};

const jf = ref({ unit: '', since: '30min', until: '', level: '', grep: '', limit: 500 });
const nf = ref({ file: 0, lines: 500, filter: '' });
const jResult = ref<JResp | null>(null);
const nResult = ref<NResp | null>(null);
const jLoading = ref(false);
const jRefreshing = ref(false);
const nLoading = ref(false);
const follow = ref(false);
const FOLLOW_MS = 5000;

const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 260 });

// unit 候选直接取自系统服务页那份列表：本机有什么 unit 由 systemd 说，前端不写死名单。
const { data: svcData } = useCachedFetch<any[]>({
  key: () => 'services:list',
  loader: () => req('/system/services'),
  maxAge: 60000,
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

async function searchJournal(opts: { silent?: boolean } = {}) {
  // silent：跟随模式下的定时刷新。这时候不能盖 loading 遮罩，
  // 否则每 5 秒画面闪一次，而且上一轮还没回来时不能并发再打（会互相覆盖结果）。
  if (opts.silent) {
    if (jRefreshing.value || jLoading.value) return;
    jRefreshing.value = true;
  } else {
    jLoading.value = true;
  }
  err.value = '';
  const q = new URLSearchParams();
  Object.entries(jf.value).forEach(([k, v]) => { if (v !== '' && v !== null) q.set(k, String(v)); });
  try {
    jResult.value = await req(`/system/logs?${q.toString()}`, { timeout: 60000 });
  } catch (e: any) {
    err.value = e.message || String(e);
    if (!opts.silent) jResult.value = null;
  } finally {
    jLoading.value = false;
    jRefreshing.value = false;
  }
}

// 跟随用轮询而不是 SSE：SSE 那条 -f 流是全量转发、不按关键词/级别过滤，
// 挂着不动就占一个 journalctl 进程；轮询复用同一个检索接口，条件所见即所得。
let pollTimer: number | null = null;
function stopFollow() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = null;
  follow.value = false;
}
function toggleFollow(v: boolean) {
  follow.value = v;
  if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; }
  if (!v) return;
  void searchJournal({ silent: true });
  pollTimer = window.setInterval(() => { void searchJournal({ silent: true }); }, FOLLOW_MS);
}
onBeforeUnmount(stopFollow);

async function tailNginx() {
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
  { title: '时间', key: 'ts', width: 148, render: (r: JItem) => h('span', { class: 'mono-dim' }, r.ts || '—') },
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
</script>

<template>
  <div>
    <PageHeader title="日志中心" sub="journal 按 unit/时间/级别/关键词检索，访问日志 tail 白名单目录里的文件，跟随模式按同样条件定时重查">
      <template #actions>
        <NText class="cell-sub" style="font-size: var(--fs-xs)">只读查询，不写入任何数据</NText>
      </template>
    </PageHeader>

    <NAlert v-if="err" type="error" :bordered="false" size="small" role="alert" style="margin-bottom: var(--space-3)">{{ err }}</NAlert>

    <NTabs v-model:value="tab" type="line" size="small" display-directive="show:lazy" class="lg-tabs">
      <NTabPane name="journal" tab="系统日志检索">
        <NSpace :size="8" wrap align="end" style="margin-bottom: var(--space-3)">
          <NSelect
            :value="jf.unit" size="small" style="width: 210px" filterable aria-label="按 unit 过滤"
            :options="unitOpts" placeholder="全部 unit"
            @update:value="(v: string) => { jf.unit = v; }"
          />
          <NInput :value="jf.since" size="small" style="width: 152px" placeholder="起始 30min/6h" aria-label="起始时间" @update:value="(v: string) => { jf.since = v; }" />
          <NInput :value="jf.until" size="small" style="width: 152px" placeholder="结束（留空=现在）" aria-label="结束时间" @update:value="(v: string) => { jf.until = v; }" />
          <NSelect
            :value="jf.level" size="small" style="width: 132px" clearable aria-label="按级别过滤" placeholder="全部级别"
            :options="LEVELS.map((l) => ({ label: `${PRIO_CN[l] || l} (${l})`, value: l }))"
            @update:value="(v: string) => { jf.level = v || ''; }"
          />
          <NInput :value="jf.grep" size="small" style="width: 220px" placeholder="关键词（PCRE 正则）" aria-label="关键词" @update:value="(v: string) => { jf.grep = v; }" @keyup.enter="searchJournal()" />
          <NSelect
            :value="jf.limit" size="small" style="width: 122px" aria-label="返回行数"
            :options="[{ label: '200 行', value: 200 }, { label: '500 行', value: 500 }, { label: '1000 行', value: 1000 }, { label: '2000 行', value: 2000 }]"
            @update:value="(v: number) => { jf.limit = v; }"
          />
          <NButton size="small" type="primary" :loading="jLoading || jRefreshing" :icon="icon('SearchOutline', 14)" @click="searchJournal()">检索</NButton>
          <span class="lg-follow">
            <NSwitch size="small" :value="follow" aria-label="跟随最新日志" @update:value="toggleFollow" />
            <NText class="cell-sub" style="font-size: var(--fs-xs)">跟随（每 {{ FOLLOW_MS / 1000 }} 秒按同样条件重查）</NText>
          </span>
        </NSpace>
        <NSpace :size="8" wrap style="margin-bottom: var(--space-3)">
          <NText class="cell-sub" style="font-size: var(--fs-xs); align-self: center">快捷入口：</NText>
          <NButton v-for="p in PRESETS" :key="p.label" size="tiny" tertiary :icon="icon('DocumentTextOutline', 12)" @click="applyPreset(p)">{{ p.label }}</NButton>
        </NSpace>
        <NText v-if="jResult" class="cell-sub" style="display: block; margin-bottom: var(--space-2); font-size: var(--fs-xs)">
          命中 {{ jResult.count }} 行{{ jResult.truncated ? '（已达上限，结果从新到旧截断，请缩小时间窗或加关键词）' : '' }}；
          时间写法支持 30min / 6h / 2d 或 2026-10-09 10:00:00，按服务器本地时区解析。
        </NText>
        <NDataTable
          ref="tableAnchor"
          :columns="jCols" :data="jRows" size="small" :bordered="false" :loading="jLoading"
          :scroll-x="900" :max-height="tableH" :virtual-scroll="true"
          :row-key="(r: any) => r.i"
        >
          <template #empty><EmptyBox :text="jResult ? '这一轮检索没有命中，试试放宽时间窗或去掉关键词' : '设置条件后点「检索」'" /></template>
        </NDataTable>
      </NTabPane>

      <NTabPane name="nginx" tab="访问/错误日志">
        <NSpace :size="8" wrap align="end" style="margin-bottom: var(--space-3)">
          <NSelect
            :value="nf.file" size="small" style="width: 260px" :options="nginxFileOpts" :disabled="!nginxFileOpts.length"
            filterable placeholder="先取一次候选文件" aria-label="日志文件"
            @update:value="(v: number) => { nf.file = v; }"
          />
          <NInput :value="nf.filter" size="small" style="width: 220px" placeholder="过滤词（子串，不区分大小写）" aria-label="过滤词" @update:value="(v: string) => { nf.filter = v; }" @keyup.enter="tailNginx" />
          <NSelect
            :value="nf.lines" size="small" style="width: 122px" aria-label="尾读行数"
            :options="[{ label: '200 行', value: 200 }, { label: '500 行', value: 500 }, { label: '1000 行', value: 1000 }, { label: '2000 行', value: 2000 }]"
            @update:value="(v: number) => { nf.lines = v; }"
          />
          <NButton size="small" type="primary" :loading="nLoading" :icon="icon('SearchOutline', 14)" @click="tailNginx">读取末尾</NButton>
        </NSpace>
        <NAlert v-if="nResult?.hint" type="info" :bordered="false" size="small">{{ nResult.hint }}</NAlert>
        <NText v-else-if="nResult" class="cell-sub" style="display: block; margin-bottom: var(--space-2); font-size: var(--fs-xs)">
          {{ nResult.source }} · 返回 {{ nResult.count }} 行{{ nResult.truncated ? '（已到上限）' : '' }}；
          服务端最多读末尾 512KB，且只允许文件管理白名单目录下的 access.log / error.log。
        </NText>
        <NDataTable
          :columns="nCols" :data="nRows" size="small" :bordered="false" :loading="nLoading"
          :scroll-x="900" :max-height="tableH" :virtual-scroll="true" class="cp-tbl tbl-log"
          :row-key="(r: { i: number }) => r.i"
        >
          <template #empty><EmptyBox :text="nResult ? '这段日志里没有命中行（换个过滤词，或加大尾读行数）' : '点「读取末尾」取一份日志'" /></template>
        </NDataTable>
      </NTabPane>
    </NTabs>
  </div>
</template>

<style scoped>
.lg-tabs :deep(.n-tabs-nav) { margin-bottom: var(--space-3); }
.lg-follow { display: inline-flex; align-items: center; gap: 6px; padding-bottom: 4px; }
.lg-text { font-size: var(--fs-2xs); line-height: 1.65; word-break: break-all; white-space: pre-wrap; }
.lg-hit { background: var(--cp-warn-soft, rgba(250, 200, 80, .28)); color: inherit; border-radius: 3px; padding: 0 1px; }
</style>
