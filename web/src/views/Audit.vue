<script setup lang="ts">
import { computed, ref, watch, h } from 'vue';
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
// null = 不传 limit，用服务端自己的默认值。默认 200、档位 [100,200,500] 过去都是这一页
// 抄的，接口那边按 100 截断（api.ts 里还有第三个 100）；现在由响应带回来。
const limit = ref<number | null>(null);

// 审计只由面板写操作追加，20s 内再次进这一页直接画上一次的结果，
// 不再出现「整表空白 + 转圈」；点刷新才是强制回源。
const { data, error, loading, busy, refresh } = useCachedFetch<{ rows: any[]; limit: number; ladder: number[] }>({
  key: () => `audit:${limit.value ?? ''}`,
  loader: () => api.audit(limit.value ?? undefined),
  maxAge: 20000,
});

// 原实现是 onMounted(load) 里 msg.error(e.message)；错误经 error ref 出来，提示口径不变
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });

// 接口只认 limit（没有 offset/page），服务端还把它夹在自己的上限，
// 所以做不了真分页；上限那一档改用虚拟滚动，DOM 里只保留可见范围的行。
const rows = computed<any[]>(() => data.value?.rows ?? []);
const limitOpts = computed(() => (data.value?.ladder || []).map((n) => ({ label: `最近 ${n} 条`, value: n })));
const shownLimit = computed(() => limit.value ?? data.value?.limit ?? null);
const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

function reload() {
  return refresh().catch(() => undefined);
}

const ACTION_TYPE: [RegExp, 'success' | 'error' | 'warning' | 'default'][] = [
  [/delete|drop|purge|stop|remove/i, 'error'],
  [/create|deploy|start|save|apply|fix|success/i, 'success'],
  [/update|reset|renew|toggle|password|set/i, 'warning'],
];
function actionType(a: string) {
  return ACTION_TYPE.find(([re]) => re.test(a))?.[1] || 'default';
}

const filtered = computed(() => {
  if (!kw.value) return rows.value;
  const k = kw.value.toLowerCase();
  // 昵称是用户看得见的名字，过滤必须一起匹配，否则按昵称搜会"查无此人"
  return rows.value.filter((r) => `${r.username || ''} ${r.nickname || ''} ${r.action} ${r.detail || ''}`.toLowerCase().includes(k));
});

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
        <NInput v-model:value="kw" placeholder="过滤用户/操作/详情" size="small" clearable style="width: 200px">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NSelect
          :value="shownLimit" size="small" style="width: 126px" aria-label="加载条数"
          :options="limitOpts" :loading="!limitOpts.length"
          @update:value="(v: number) => { limit = v; }"
        />
        <NButton size="small" tertiary :loading="busy" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
      </template>
    </PageHeader>

    <NCard size="small">
      <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-3)">
        共 {{ filtered.length }} 条{{ kw ? `（已过滤，加载 ${rows.length} 条）` : '' }}。审计按保留策略自动裁剪，仅管理员可见。
      </NText>
      <NDataTable
        ref="tableAnchor"
        size="small" :bordered="false" :loading="loading" :scroll-x="800"
        :max-height="tableH" :min-row-height="40" virtual-scroll :pagination="false"
        :columns="cols" :data="filtered"
        :row-key="(r: any) => r.id"
      >
        <template #empty><EmptyBox :text="error ? '审计记录获取失败，点上方「刷新」重试' : (kw ? '没有匹配的记录' : '暂无操作记录')" /></template>
      </NDataTable>
    </NCard>
  </div>
</template>
