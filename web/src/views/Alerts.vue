<script setup lang="ts">
import { computed, h, onScopeDispose, ref, watch } from 'vue';
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
type HistoryResp = { rows: AlertRow[]; limit: number; ladder: number[]; q: string; matched: number | null; hasMore: boolean };
type OpenReport = { rows: AlertRow[]; counts: Record<string, number>; total: number };

const msg = useMessage();
const isAdmin = computed(() => getRole() === 'admin');
const level = ref('');
// null = "不传 limit，按服务端的默认来"。默认值和档位都是这一页过去自己抄的
// （ref(200) + [100,200,500]），接口那边其实按 100 截断——同一件事两份数，
// 从页面进看到 200 条、从 curl 进只有 100 条。现在由响应把生效值与档位带回来。
const limit = ref<number | null>(null);
const kw = ref('');
// 关键字必须由服务端过滤：页面一屏只有 limit 行，在浏览器里 filter 的话更早的命中
// 永远搜不到，空态还会把窗口边界说成历史边界。
const q = ref('');
let debounce = 0;
onScopeDispose(() => clearTimeout(debounce));
watch(kw, (v) => {
  clearTimeout(debounce);
  debounce = window.setTimeout(() => { q.value = v.trim(); }, 300);
});
const acking = ref<number | null>(null);
const running = ref('');

const { data, loading, error, refresh } = useCachedFetch<HistoryResp>({
  key: () => `alerts:history:${level.value}:${limit.value ?? ''}:${q.value}`,
  loader: () => {
    const p = new URLSearchParams();
    if (limit.value) p.set('limit', String(limit.value));
    if (level.value) p.set('level', level.value);
    if (q.value) p.set('q', q.value);
    const s = p.toString();
    return req(`/alerts/history${s ? `?${s}` : ''}`);
  },
  maxAge: 15000,
});
const { data: openData, error: openError, load: loadOpen } = useCachedFetch<OpenReport>({
  key: () => 'alerts:open',
  loader: () => req('/alerts/open'),
  maxAge: 15000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });
watch(openError, (e) => { if (e) msg.error((e as Error).message || String(e)); });

// 更早的几页只追加在本页：缓存管的永远是「当前条件下的第一屏」，
// 条件一变（换词/换级别/换条数/点刷新）第一屏重新取，追加的那几页必须一起清掉。
const older = ref<AlertRow[]>([]);
const olderBusy = ref(false);
const olderError = ref('');
const moreAfter = ref(false);
watch(data, (d) => {
  older.value = [];
  olderError.value = '';
  moreAfter.value = !!d?.hasMore;
});

const limitOpts = computed(() => (data.value?.ladder || []).map((n) => ({ label: `最近 ${n} 条`, value: n })));
// 用户没碰过选择器时回显服务端实际用掉的那一档，而不是本页猜一个默认值
const shownLimit = computed(() => limit.value ?? data.value?.limit ?? null);

const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

const rows = computed<AlertRow[]>(() => {
  const list = [...(data.value?.rows ?? []), ...older.value];
  // 未确认的钉在最上面：这一页首要用途是「还有什么没处理」，不是完整时间线。
  return list.sort((a, b) => (a.acked === b.acked ? b.id - a.id : a.acked - b.acked));
});
// 匹配数是服务端在这个过滤条件下数出来的整表条数；不能拿已加载的行数充数。
const matched = computed(() => data.value?.matched ?? null);
const hasOlder = computed(() => moreAfter.value);
const canLoadOlder = computed(() => moreAfter.value && rows.value.length > 0 && !olderBusy.value);
const countText = computed(() => {
  const n = matched.value === null ? '—' : String(matched.value);
  const head = q.value ? `匹配 ${n} 条` : (level.value ? `该级别共 ${n} 条` : `共 ${n} 条`);
  const shown = rows.value.length;
  return shown && shown < (matched.value ?? shown) ? `${head}，已加载 ${shown} 条` : head;
});
const counts = computed<Record<string, number>>(() => openData.value?.counts || { ok: 0, warn: 0, crit: 0 });
const openTotal = computed(() => openData.value?.total ?? 0);
// 「读不到未确认」和「未确认是 0」是两回事。旧写法在 /alerts/open 失败时把
// openData 留成 undefined，汇总卡照样渲染「未确认 0 条」+ 三个 0 —— 接口挂了
// 反而是一片绿灯，正好盖住最该看见的故障。失败时数字改画成「—」并点名读取失败。
const openFailed = computed(() => !openData.value && !!openError.value);
// 历史这一侧同样的第三态：读取失败时 data 是空，画成「暂无告警历史」会把"没读到"说成"没有告警"。
const histFailed = computed(() => !data.value && !!error.value);

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

// 游标取「已加载里最小的 id」，不是表格最后一行：未确认的行被钉在最上面，
// 最后一行的 id 往往不是最小，拿它当游标会在两页之间漏掉一段。
async function loadOlder() {
  if (!canLoadOlder.value) return;
  const ids = rows.value.map((r) => r.id);
  if (!ids.length) return;
  const before = Math.min(...ids);
  const mineQ = q.value;
  const mineLevel = level.value;
  const mineLimit = limit.value;
  olderBusy.value = true;
  olderError.value = '';
  try {
    const p = new URLSearchParams();
    if (mineLimit) p.set('limit', String(mineLimit));
    if (mineLevel) p.set('level', mineLevel);
    if (mineQ) p.set('q', mineQ);
    p.set('before', String(before));
    const r: HistoryResp = await req(`/alerts/history?${p.toString()}`);
    // 迟到响应：期间换了条件，这一页属于旧条件，扔掉而不是拼进来
    if (q.value !== mineQ || level.value !== mineLevel || limit.value !== mineLimit) return;
    older.value = [...older.value, ...(r.rows || [])];
    moreAfter.value = !!r.hasMore;
  } catch (e: any) {
    olderError.value = e?.message || String(e);
  } finally {
    olderBusy.value = false;
  }
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
  // running 与 acking 互斥：巡检/批量确认进行中时单条确认会打到即将消失的行。
  if (acking.value !== null || running.value) return;
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
  if (running.value) return;
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
  if (running.value) return;
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
          <NText strong class="cp-pop" style="font-size: var(--fs-sm)">
            <template v-if="openFailed">未确认 —（未确认统计读取失败）</template>
            <template v-else>未确认 {{ openTotal }} 条</template>
          </NText>
          <NSpace :size="12" class="al-levels">
            <template v-if="openFailed">
              <span class="st err"><span class="dot err"></span>严重 —</span>
              <span class="st warn"><span class="dot warn"></span>注意 —</span>
              <span class="st ok"><span class="dot ok"></span>恢复 —</span>
            </template>
            <template v-else>
              <span class="st err"><span class="dot err"></span>严重 {{ counts.crit || 0 }}</span>
              <span class="st warn"><span class="dot warn"></span>注意 {{ counts.warn || 0 }}</span>
              <span class="st ok"><span class="dot ok"></span>恢复 {{ counts.ok || 0 }}</span>
            </template>
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
          <NButton size="small" tertiary :loading="running === 'run'" :disabled="!!running || acking !== null" :icon="icon('PlayOutline', 14)" @click="runRound('run')">立即巡检</NButton>
          <NButton size="small" tertiary :loading="running === 'probe'" :disabled="!!running || acking !== null" :icon="icon('PulseOutline', 14)" @click="runRound('probe')">存活探测</NButton>
          <NButton size="small" type="primary" :disabled="openFailed || !openTotal || !!running || acking !== null" :loading="running === 'ack'" :icon="icon('CheckmarkCircleOutline', 14)" @click="ackAll">全部确认</NButton>
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

      <!-- 这一屏只装了 limit 行：把「匹配置信数」和「还有更早的没加载」写在同一句里，
           用户才不会把窗口边界当成历史的边界。 -->
      <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-3)">
        {{ countText }}{{ hasOlder ? '；还有更早的记录未加载' : '' }}。
      </NText>

      <NDataTable
        ref="tableAnchor"
        size="small" :bordered="false" :loading="loading" :scroll-x="'auto'"
        :max-height="tableH"
        :columns="cols" :data="rows"
        :row-key="(r: AlertRow) => r.id"
      >
        <template #empty>
          <EmptyBox :text="histFailed ? '告警历史读取失败，点「刷新」重试' : (q ? `没有匹配「${q}」的告警记录` : (level ? '该级别没有告警记录' : '暂无告警历史，点「立即巡检」跑一轮'))" />
        </template>
      </NDataTable>
      <div v-if="canLoadOlder || olderError" style="display: flex; align-items: center; justify-content: center; gap: var(--space-3); margin-top: var(--space-3)">
        <NButton v-if="canLoadOlder" size="small" tertiary :loading="olderBusy" @click="loadOlder">
          {{ olderError ? '重试加载更早的记录' : '加载更早的记录' }}
        </NButton>
        <NText v-if="olderError" type="error" style="font-size: var(--fs-xs)">{{ olderError }}</NText>
      </div>
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
