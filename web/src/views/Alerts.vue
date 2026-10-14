<script setup lang="ts">
import { computed, h, ref, watch } from 'vue';
import {
  NAlert, NCard, NDataTable, NIcon, NInput, NSelect, NSpace, NTag, NText, useMessage,
} from 'naive-ui';
import { NButton } from '../ui';
import { getRole, req } from '../api';
import { icon, icons } from '../icons';
import CpToolbarCard from '../components/CpToolbarCard.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { invalidateCache, useCachedFetch } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';
import { fmtDateTime } from '../composables/useFormat';

type AlertRow = { id: number; ts: string; level: string; code: string; message: string; meta: string; acked: number };
type HistoryResp = { rows: AlertRow[]; limit: number; ladder: number[] };
type OpenReport = { rows: AlertRow[]; counts: Record<string, number>; total: number };

const msg = useMessage();
const isAdmin = computed(() => getRole() === 'admin');
const level = ref('');
// null = "不传 limit，按服务端的默认来"。默认值和档位都是这一页过去自己抄的
// （ref(200) + [100,200,500]），接口那边其实按 100 截断——同一件事两份数，
// 从页面进看到 200 条、从 curl 进只有 100 条。现在由响应把生效值与档位带回来。
const limit = ref<number | null>(null);
const kw = ref('');
const acking = ref<number | null>(null);
const running = ref('');

const { data, loading, error, refresh } = useCachedFetch<HistoryResp>({
  key: () => `alerts:history:${level.value}:${limit.value ?? ''}`,
  loader: () => req(`/alerts/history?${limit.value ? `limit=${limit.value}&` : ''}${level.value ? `level=${level.value}` : ''}`),
  maxAge: 15000,
});
const { data: openData, error: openError, load: loadOpen } = useCachedFetch<OpenReport>({
  key: () => 'alerts:open',
  loader: () => req('/alerts/open'),
  maxAge: 15000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });
watch(openError, (e) => { if (e) msg.error((e as Error).message || String(e)); });

const limitOpts = computed(() => (data.value?.ladder || []).map((n) => ({ label: `最近 ${n} 条`, value: n })));
// 用户没碰过选择器时回显服务端实际用掉的那一档，而不是本页猜一个默认值
const shownLimit = computed(() => limit.value ?? data.value?.limit ?? null);

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
    // 展开区由 columns.render 创建，本组件的 scoped 样式落不到它身上（同 style.css 里的 .json-tip 注释），
    // 所以尺寸与配色写内联；折行用全局 .cp-break（它是全局样式，够得着）。
    // meta 是巡检写进来的 JSON，格式化失败就原样显示。
    renderExpand: (r: AlertRow) =>
      h('pre', {
        class: 'cp-break',
        style: {
          margin: '0', padding: 'var(--space-3)', background: 'var(--cp-code-bg)',
          color: 'var(--cp-code-text)', border: '1px solid var(--cp-border)', borderRadius: '8px',
          fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-2xs)', lineHeight: '1.6',
          maxHeight: '320px', overflow: 'auto',
        },
      }, metaText(r.meta) || '（无附加信息）'),
  },
  {
    title: '级别', key: 'level', width: 104,
    render: (r: AlertRow) => {
      const cfg = LEVEL[r.level] || { label: r.level, type: 'success' as const, ico: 'InformationCircleOutline' };
      return h(NTag, { size: 'small', bordered: false, round: true, type: cfg.type }, {
        icon: icon(cfg.ico, 12),
        default: () => cfg.label,
      });
    },
  },
  {
    // 1Panel 列表不用相对时间：绝对时间 + width 180（对齐 alert log createdAt 列）。
    // (UTC) 写在表头而不是每格 title 里：表头不需要 hover 就一直在。
    title: '时间 (UTC)', key: 'ts', width: 180,
    render: (r: AlertRow) => h('span', { class: 'mono-dim' }, fmtDateTime(r.ts)),
  },
  {
    title: '来源', key: 'code', width: 200,
    // 内联 wordBreak 只赢 word-break 这一条：white-space:nowrap 仍由 .mono-dim 提供，
    // 于是这条「打算换行」的声明一个字都没换过。类名交给 .cp-break（它自带打赢 nowrap 的权重）。
    render: (r: AlertRow) =>
      h('span', { class: 'mono-dim cp-break' }, r.code),
  },
  {
    // 告警内容就是这一行的结论。手机上没有 hover，tooltip 与 title 都摸不到，
    // 所以让它折行（三行封顶），tooltip 退成桌面端的补充。
    title: '内容', key: 'message', minWidth: 260,
    render: (r: AlertRow) => h('span', { class: 'cp-prose', title: r.message }, r.message),
  },
  {
    title: '状态', key: 'acked', width: 92,
    render: (r: AlertRow) =>
      h(NTag, {
        size: 'small', bordered: false, round: true,
        type: r.acked ? 'info' : (LEVEL[r.level]?.type || 'default'),
      }, { default: () => (r.acked ? '已确认' : '未确认') }),
  },
  {
    title: '操作', key: 'ops', width: 110, align: 'center', fixed: 'right' as const,
    render: (r: AlertRow) =>
      // 只读账号既看不到按钮，也不该在 DOM 里留一个必然 403 的入口
      isAdmin.value && !r.acked
        ? h(NButton, {
            text: true, size: 'small', type: 'primary', class: 'cp-op',
            loading: acking.value === r.id, disabled: !!acking.value,
            onClick: () => ack(r),
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
          巡检与存活探测的每一轮结论都留痕：严重级为连不上/单元 failed/证书过期，恢复也会记一条。
          同一场持续中的告警只留一行，状态变了才会再多一行；未确认的历史不受过期裁剪影响，确认后才按保留策略清理。
        </p>
      </div>
    </NCard>

    <CpToolbarCard title="告警历史" class="cp-card-gap">
      <template #left>
        <template v-if="isAdmin">
          <NButton size="small" tertiary :loading="running === 'run'" :icon="icon('PlayOutline', 14)" @click="runRound('run')">立即巡检</NButton>
          <NButton size="small" tertiary :loading="running === 'probe'" :icon="icon('PulseOutline', 14)" @click="runRound('probe')">存活探测</NButton>
          <NButton size="small" type="primary" :disabled="!openTotal" :loading="running === 'ack'" :icon="icon('CheckmarkCircleOutline', 14)" @click="ackAll">全部确认</NButton>
        </template>
      </template>
      <template #right>
        <NInput v-model:value="kw" placeholder="过滤来源/内容" size="small" clearable style="width: 200px; max-width: 100%">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NSelect
          :value="level" size="small" style="width: 200px; max-width: 100%" aria-label="按级别过滤"
          :options="[
            { label: '全部级别', value: '' },
            { label: '仅严重', value: 'crit' },
            { label: '仅注意', value: 'warn' },
            { label: '仅恢复', value: 'ok' },
          ]"
          @update:value="(v: string) => { level = v; }"
        />
        <NSelect
          :value="shownLimit" size="small" style="width: 110px; max-width: 100%" aria-label="加载条数"
          :options="limitOpts" :loading="!limitOpts.length"
          @update:value="(v: number) => { limit = v; }"
        />
        <NButton size="small" tertiary :loading="loading" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
      </template>

      <NAlert type="info" :bordered="false" size="small" class="al-tip">
        存活探测只对本机 127.0.0.1 的应用端口发一次 GET：连不上或超时算严重，只要有 HTTP 应答（含 4xx/5xx）就算活着，不会因此推送通知。阈值与通知通道在「设置 · 告警」里配置。
      </NAlert>

      <NDataTable
        ref="tableAnchor"
        size="small" :bordered="false" :loading="loading" :scroll-x="1030"
        :max-height="tableH"
        :columns="cols" :data="rows"
        :row-key="(r: AlertRow) => r.id"
      >
        <template #empty>
          <EmptyBox :text="kw || level ? '没有符合筛选条件的告警记录' : '暂无告警历史，点「立即巡检」跑一轮'" />
        </template>
      </NDataTable>
    </CpToolbarCard>
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
.al-tip { margin-bottom: var(--space-3); }
@media (max-width: 640px) {
  .al-summary { gap: var(--space-3); }
}
</style>
