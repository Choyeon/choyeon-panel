<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  NAlert, NCard, NDataTable, NIcon, NInput, NSelect, NSpace, NTag, NText, useMessage,
} from 'naive-ui';
import { NButton } from '../ui';
import { getRole, req } from '../api';
import { icon, icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { invalidateCache, useCachedFetch } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';
import { fmtAgo, fmtDateTime } from '../composables/useFormat';

type AlertRow = { id: number; ts: string; level: string; code: string; message: string; meta: string; acked: number };
type OpenReport = { rows: AlertRow[]; counts: Record<string, number>; total: number };

const msg = useMessage();
const isAdmin = computed(() => getRole() === 'admin');
const level = ref('');
const limit = ref(200);
const kw = ref('');
const acking = ref<number | null>(null);
const running = ref('');

// 相对时间得自己往前走：这一页常被开着当监控台，不重算的话「3 分钟前」会一路挂到下午。
const nowTick = ref(Date.now());
let tick: number | null = null;
onMounted(() => { tick = window.setInterval(() => { nowTick.value = Date.now(); }, 30000); });
onBeforeUnmount(() => { if (tick) clearInterval(tick); });

const { data, loading, error, refresh } = useCachedFetch<{ rows: AlertRow[] }>({
  key: () => `alerts:history:${level.value}:${limit.value}`,
  loader: () => req(`/alerts/history?limit=${limit.value}${level.value ? `&level=${level.value}` : ''}`),
  maxAge: 15000,
});
const { data: openData, error: openError, load: loadOpen } = useCachedFetch<OpenReport>({
  key: () => 'alerts:open',
  loader: () => req('/alerts/open?limit=500'),
  maxAge: 15000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });
watch(openError, (e) => { if (e) msg.error((e as Error).message || String(e)); });

const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

const rows = computed<AlertRow[]>(() => {
  const list = data.value?.rows ?? [];
  const k = kw.value.trim().toLowerCase();
  const hit = k ? list.filter((r) => `${r.code} ${r.message}`.toLowerCase().includes(k)) : list;
  // 未确认的钉在最上面：这一页首要用途是「还有什么没处理」，不是完整时间线。
  return [...hit].sort((a, b) => (a.acked === b.acked ? b.id - a.id : a.acked - b.acked));
});
const counts = computed<Record<string, number>>(() => openData.value?.counts || { ok: 0, warn: 0, crit: 0 });
const openTotal = computed(() => openData.value?.total ?? 0);

const LEVEL: Record<string, { label: string; type: 'success' | 'warning' | 'error'; ico: string; dot: string }> = {
  crit: { label: '严重', type: 'error', ico: 'CloseCircleOutline', dot: 'err' },
  warn: { label: '注意', type: 'warning', ico: 'WarningOutline', dot: 'warn' },
  ok: { label: '恢复', type: 'success', ico: 'CheckmarkCircleOutline', dot: 'ok' },
};

function metaText(raw: string) {
  if (!raw) return '';
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

// 写操作会让所有筛选档位的历史都变（新增/确认），只作废本页那一条不够，整段前缀清掉。
function reload() {
  invalidateCache('alerts:');
  invalidateCache('audit:');
  void loadOpen().catch(() => undefined);
  return refresh().catch(() => undefined);
}

const cols: any[] = [
  {
    type: 'expand', width: 34,
    // 展开区由 columns.render 创建，scoped 样式落不到它身上（同 style.css 里的 .json-tip 注释），
    // 所以这里写内联样式；meta 是巡检写进来的 JSON，格式化失败就原样显示。
    renderExpand: (r: AlertRow) =>
      h('pre', {
        style: {
          margin: '0', padding: 'var(--space-3)', background: 'var(--cp-code-bg)',
          color: 'var(--cp-code-text)', border: '1px solid var(--cp-border)', borderRadius: '8px',
          fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-2xs)', lineHeight: '1.6',
          whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: '320px', overflow: 'auto',
        },
      }, metaText(r.meta) || '（无附加信息）'),
  },
  {
    title: '级别', key: 'level', width: 104,
    render: (r: AlertRow) => {
      const cfg = LEVEL[r.level] || { label: r.level, type: 'success' as const, ico: 'InformationCircleOutline' };
      return h(NTag, { size: 'tiny', bordered: false, type: cfg.type }, {
        icon: icon(cfg.ico, 12),
        default: () => cfg.label,
      });
    },
  },
  {
    title: '时间', key: 'ts', width: 130,
    render: (r: AlertRow) =>
      h('span', { class: 'mono-dim', title: `${fmtDateTime(r.ts)} (UTC)` }, fmtAgo(r.ts, nowTick.value)),
  },
  {
    title: '来源', key: 'code', width: 200,
    render: (r: AlertRow) =>
      h('span', { class: 'mono-dim', style: { wordBreak: 'break-all' } }, r.code),
  },
  { title: '内容', key: 'message', minWidth: 260, ellipsis: { tooltip: true } },
  {
    title: '状态', key: 'acked', width: 92,
    render: (r: AlertRow) =>
      h('span', { class: 'st ' + (r.acked ? 'idle' : (LEVEL[r.level]?.dot || 'idle')) }, [
        h('span', { class: 'dot ' + (r.acked ? 'idle' : (LEVEL[r.level]?.dot || 'idle')) }),
        r.acked ? '已确认' : '未确认',
      ]),
  },
  {
    title: '操作', key: 'ops', width: 110,
    render: (r: AlertRow) =>
      // 只读账号既看不到按钮，也不该在 DOM 里留一个必然 403 的入口
      isAdmin.value && !r.acked
        ? h(NButton, {
            size: 'tiny', tertiary: true, type: 'primary',
            loading: acking.value === r.id, disabled: !!acking.value,
            icon: icon('CheckmarkCircleOutline', 14), onClick: () => ack(r),
          }, () => '确认')
        : h('span', { class: 'cell-sub' }, r.acked ? `#${r.id}` : '—'),
  },
];

async function ack(r: AlertRow) {
  acking.value = r.id;
  try {
    await req(`/alerts/${r.id}/ack`, { body: {} });
    msg.success('已确认');
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    acking.value = null;
  }
}

async function ackAll() {
  running.value = 'ack';
  try {
    const r = await req('/alerts/ack-all', { body: {} });
    msg.success(`已确认 ${r?.acked ?? 0} 条`);
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    running.value = '';
  }
}

// 巡检内含 systemctl / certbot 子进程与逐应用探测，服务端没有硬上限：
// 最坏情况约等于「应用数 × 探测超时」。前端超时必须明显大于它，
// 否则本地先 abort 而远端还在跑，用户再点一次就会并发两轮巡检。
async function runRound(kind: 'run' | 'probe') {
  running.value = kind;
  try {
    const r = await req(kind === 'run' ? '/alerts/run' : '/alerts/probe', {
      body: {},
      timeout: kind === 'run' ? 330000 : 300000,
    });
    if (kind === 'run') msg.success(`巡检完成，新增 ${r?.count ?? 0} 条历史`);
    else msg.success(`探测完成，${r?.results?.length ?? 0} 个应用，新增 ${r?.count ?? 0} 条历史`);
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    running.value = '';
  }
}
</script>

<template>
  <div>
    <PageHeader title="告警历史" sub="巡检与存活探测的每一轮结论都留痕：严重级为连不上/单元 failed/证书过期，恢复也会记一条">
      <template #actions>
        <NInput v-model:value="kw" placeholder="过滤来源/内容" size="small" clearable style="width: 170px">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NSelect
          :value="level" size="small" style="width: 120px" aria-label="按级别过滤"
          :options="[
            { label: '全部级别', value: '' },
            { label: '仅严重', value: 'crit' },
            { label: '仅注意', value: 'warn' },
            { label: '仅恢复', value: 'ok' },
          ]"
          @update:value="(v: string) => { level = v; }"
        />
        <NSelect
          :value="limit" size="small" style="width: 110px" aria-label="加载条数"
          :options="[
            { label: '最近 100 条', value: 100 },
            { label: '最近 200 条', value: 200 },
            { label: '最近 500 条', value: 500 },
          ]"
          @update:value="(v: number) => { limit = v; }"
        />
        <NButton size="small" tertiary :loading="loading" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
        <template v-if="isAdmin">
          <NButton size="small" tertiary :loading="running === 'probe'" :icon="icon('PulseOutline', 14)" @click="runRound('probe')">存活探测</NButton>
          <NButton size="small" tertiary :loading="running === 'run'" :icon="icon('PlayOutline', 14)" @click="runRound('run')">立即巡检</NButton>
          <NButton size="small" type="primary" :disabled="!openTotal" :loading="running === 'ack'" :icon="icon('CheckmarkCircleOutline', 14)" @click="ackAll">全部确认</NButton>
        </template>
      </template>
    </PageHeader>

    <NCard size="small" style="margin-bottom: var(--space-4)">
      <div class="al-summary">
        <div class="al-side">
          <NText strong class="cp-pop" style="font-size: var(--fs-sm)">未确认 {{ openTotal }} 条</NText>
          <NSpace :size="12" class="al-levels">
            <span class="st err"><span class="dot err"></span>严重 {{ counts.crit || 0 }}</span>
            <span class="st warn"><span class="dot warn"></span>注意 {{ counts.warn || 0 }}</span>
            <span class="st ok"><span class="dot ok"></span>恢复 {{ counts.ok || 0 }}</span>
          </NSpace>
        </div>
        <p class="al-note">
          同一场持续中的告警只留一行，状态变了才会再多一行；未确认的历史不受过期裁剪影响，确认后才按保留策略清理。
        </p>
      </div>
    </NCard>

    <NAlert type="info" :bordered="false" size="small" class="al-tip">
      存活探测只对本机 127.0.0.1 的应用端口发一次 GET：连不上或超时算严重，只要有 HTTP 应答（含 4xx/5xx）就算活着，不会因此推送通知。阈值与通知通道在「设置 · 告警」里配置。
    </NAlert>

    <NDataTable
      ref="tableAnchor"
      size="small" :bordered="false" :loading="loading" :scroll-x="1000"
      :max-height="tableH"
      :columns="cols" :data="rows"
      :row-key="(r: AlertRow) => r.id"
    >
      <template #empty>
        <EmptyBox :text="kw || level ? '没有符合筛选条件的告警记录' : '暂无告警历史，点「立即巡检」跑一轮'" />
      </template>
    </NDataTable>
  </div>
</template>

<style scoped>
.al-summary { display: flex; align-items: flex-start; gap: var(--space-5); flex-wrap: wrap; }
.al-side { flex: 1 1 260px; min-width: 0; }
.al-note {
  display: block;
  margin-top: var(--space-2);
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--cp-text-mute);
}
.al-levels { margin-top: var(--space-2); }
.al-tip { margin-bottom: var(--space-4); }
@media (max-width: 640px) {
  .al-summary { gap: var(--space-3); }
}
</style>
