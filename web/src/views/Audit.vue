<script setup lang="ts">
import { computed, onScopeDispose, ref, watch, h } from 'vue';
import { NCard, NDataTable, NInput, NSelect, NIcon, NTag, NText, useMessage } from 'naive-ui';
import { NButton, tapEllipsis } from '../ui';
import { api } from '../api';
import { icon, icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { useCachedFetch } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';
import { fmtDateTime } from '../composables/useFormat';

const msg = useMessage();
const kw = ref('');
// 过滤词防抖后才发给服务端：每敲一个字都打一次 LIKE 全表没有必要，
// 但过滤本身必须在服务端——一屏只有 limit 行，在客户端过滤等于「只看得到最近这屏」，
// 更早的记录会永远搜不到，还会被显示成「没有匹配的记录」。
const q = ref('');
// null = 不传 limit，用服务端自己的默认值。默认 200、档位 [100,200,500] 过去都是这一页
// 抄的，接口那边按 100 截断（api.ts 里还有第三个 100）；现在由响应带回来。
const limit = ref<number | null>(null);

// 审计只由面板写操作追加，20s 内再次进这一页直接画上一次的结果，
// 不再出现「整表空白 + 转圈」；点刷新才是强制回源。
const { data, error, loading, busy, refresh } = useCachedFetch<{
  rows: any[]; limit: number; ladder: number[]; q: string; matched: number | null; hasMore: boolean;
}>({
  key: () => `audit:${limit.value ?? ''}:${q.value}`,
  loader: () => api.audit({ limit: limit.value ?? undefined, q: q.value || undefined }),
});

let debounce = 0;
onScopeDispose(() => clearTimeout(debounce));
watch(kw, (v) => {
  clearTimeout(debounce);
  debounce = window.setTimeout(() => { q.value = v.trim(); }, 300);
});

// 原实现是 onMounted(load) 里 msg.error(e.message)；错误经 error ref 出来，提示口径不变
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });

// 更早的几页只追加在本页：缓存管的永远是「当前条件下的第一屏」，
// 条件一变（换搜索词/换每页条数/点刷新）第一屏重新取，追加的那几页必须一起清掉——
// 旧条件的行拼在新结果后面，看起来像是「过滤没生效」。
const older = ref<any[]>([]);
const olderBusy = ref(false);
const olderError = ref('');
const moreAfter = ref(false);
watch(data, (d) => {
  older.value = [];
  olderError.value = '';
  moreAfter.value = !!d?.hasMore;
});

const rows = computed<any[]>(() => [...(data.value?.rows ?? []), ...older.value]);
const matched = computed(() => data.value?.matched ?? null);
const canLoadOlder = computed(() => moreAfter.value && rows.value.length > 0 && !olderBusy.value);
const limitOpts = computed(() => (data.value?.ladder || []).map((n) => ({ label: `每页 ${n} 条`, value: n })));
const shownLimit = computed(() => limit.value ?? data.value?.limit ?? null);
const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

const countText = computed(() => {
  const n = matched.value === null ? '—' : String(matched.value);
  const head = q.value ? `匹配 ${n} 条` : `共 ${n} 条`;
  return rows.value.length ? `${head}，已加载 ${rows.value.length} 条` : head;
});

function reload() {
  return refresh().catch(() => undefined);
}

async function loadOlder() {
  // 连点会打出两页同游标：第二页会从第一页末尾之后再取，
  // 两页之间没有重叠，但夹在中间的新记录会漏（id 游标是严格小于）。
  if (!canLoadOlder.value) return;
  const last = rows.value[rows.value.length - 1];
  if (!last) return;
  const mineQ = q.value;
  const mineLimit = limit.value;
  olderBusy.value = true;
  olderError.value = '';
  try {
    const r: any = await api.audit({
      limit: mineLimit ?? undefined, q: mineQ || undefined, before: last.id,
    });
    // 迟到响应：期间换了搜索词/每页条数，这一页属于旧条件，扔掉而不是拼进来
    if (q.value !== mineQ || limit.value !== mineLimit) return;
    older.value = [...older.value, ...(r.rows || [])];
    moreAfter.value = !!r.hasMore;
  } catch (e: any) {
    olderError.value = e?.message || String(e);
  } finally {
    olderBusy.value = false;
  }
}

const ACTION_TYPE: [RegExp, 'success' | 'error' | 'warning' | 'default'][] = [
  [/delete|drop|purge|stop|remove/i, 'error'],
  [/create|deploy|start|save|apply|fix|success/i, 'success'],
  [/update|reset|renew|toggle|password|set/i, 'warning'],
];
function actionType(a: string) {
  return ACTION_TYPE.find(([re]) => re.test(a))?.[1] || 'default';
}

const cols: any[] = [
  { title: '时间 (UTC)', key: 'created_at', width: 165, render: (r: any) => h('span', { class: 'mono-dim' }, fmtDateTime(r.created_at)) },
  {
    title: '用户', key: 'username', width: 150,
    render: (r: any) =>
      h('div', { class: 'cell-main', style: 'gap:var(--space-2)' }, [
        ...(r.avatar
          ? [h('span', { class: 'cell-avatar sm' }, [h('img', { src: r.avatar, alt: '' })])]
          : [h(NIcon, { component: icons.PersonCircleOutline, size: 18, color: 'var(--cp-text-mute)' })]),
        // 有昵称显示昵称，没有则回退登录名；system 等审计主体不在 users 表里，原样显示。
        h('span', { class: 'cell-name', title: r.nickname ? `登录名：${r.username || '—'}` : r.username }, r.nickname || r.username || '—'),
      ]),
  },
  // action 最长的是 `terminal.exec`/`app.deploy.rollback` 这类点分名，190px 会截断，给它一个 title。
  { title: '操作', key: 'action', width: 190, render: (r: any) => h(NTag, { size: 'tiny', bordered: false, type: actionType(r.action), title: r.action }, () => r.action) },
  // 这张表开着 virtual-scroll，行高必须齐，详情不能折行；那就把全文的入口从 hover 换成点按。
  { title: '详情', key: 'detail', minWidth: 220, ellipsis: tapEllipsis, render: (r: any) => h('span', { class: 'mono-dim', title: r.detail || '' }, r.detail || '—') },
];
</script>

<template>
  <div>
    <PageHeader title="操作审计" sub="面板内全部写操作留痕：部署、启停、删除、配置修改、终端会话与登录口令变更">
      <template #actions>
        <NInput v-model:value="kw" placeholder="过滤用户/操作/详情" size="small" clearable style="width: 200px; max-width: 100%">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NSelect
          :value="shownLimit" size="small" style="width: 126px; max-width: 100%" aria-label="加载条数"
          :options="limitOpts" :loading="!limitOpts.length"
          @update:value="(v: number) => { limit = v; }"
        />
        <NButton size="small" tertiary :loading="busy" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
      </template>
    </PageHeader>

    <NCard size="small">
      <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-3)">
        {{ countText }}{{ moreAfter ? '；还有更早的记录未加载' : '' }}。审计按保留策略自动裁剪，仅管理员可见。
      </NText>
      <NDataTable
        ref="tableAnchor"
        size="small" :bordered="false" :loading="loading" :scroll-x="'auto'"
        :max-height="tableH" :min-row-height="40" virtual-scroll :pagination="false"
        :columns="cols" :data="rows"
        :row-key="(r: any) => r.id"
      >
        <template #empty><EmptyBox :text="error ? '审计记录获取失败，点上方「刷新」重试' : (q ? `没有匹配「${q}」的记录` : '暂无操作记录')" /></template>
      </NDataTable>
      <div v-if="canLoadOlder || olderError" style="display: flex; align-items: center; justify-content: center; gap: var(--space-3); margin-top: var(--space-3)">
        <NButton v-if="canLoadOlder" size="small" tertiary :loading="olderBusy" @click="loadOlder">
          {{ olderError ? '重试加载更早的记录' : '加载更早的记录' }}
        </NButton>
        <NText v-if="olderError" type="error" style="font-size: var(--fs-xs)">{{ olderError }}</NText>
      </div>
    </NCard>
  </div>
</template>
