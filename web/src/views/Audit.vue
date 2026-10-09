<script setup lang="ts">
import { computed, onMounted, ref, h } from 'vue';
import { NCard, NSpace, NDataTable, NButton, NInput, NSelect, NIcon, NTag, NText, useMessage } from 'naive-ui';
import { api } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const rows = ref<any[]>([]);
const loading = ref(false);
const kw = ref('');
const limit = ref(200);

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

async function load() {
  loading.value = true;
  try {
    rows.value = await api.audit(limit.value);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    loading.value = false;
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

const filtered = computed(() => {
  if (!kw.value) return rows.value;
  const k = kw.value.toLowerCase();
  // 昵称是用户看得见的名字，过滤必须一起匹配，否则按昵称搜会"查无此人"
  return rows.value.filter((r) => `${r.username || ''} ${r.nickname || ''} ${r.action} ${r.detail || ''}`.toLowerCase().includes(k));
});

const cols: any[] = [
  { title: '时间 (UTC)', key: 'created_at', width: 165, render: (r: any) => h('span', { class: 'mono-dim' }, r.created_at) },
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
  { title: '操作', key: 'action', width: 190, render: (r: any) => h(NTag, { size: 'tiny', bordered: false, type: actionType(r.action) }, () => r.action) },
  { title: '详情', key: 'detail', minWidth: 220, ellipsis: { tooltip: true }, render: (r: any) => h('span', { class: 'mono-dim' }, r.detail || '—') },
];

onMounted(load);
</script>

<template>
  <div>
    <PageHeader title="操作审计" sub="面板内全部写操作留痕：部署、启停、删除、配置修改、终端会话与登录口令变更">
      <template #actions>
        <NInput v-model:value="kw" placeholder="过滤用户/操作/详情" size="small" clearable style="width: 200px">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NSelect
          :value="limit" size="small" style="width: 110px" aria-label="加载条数"
          :options="[{ label: '最近 100 条', value: 100 }, { label: '最近 200 条', value: 200 }, { label: '最近 500 条', value: 500 }]"
          @update:value="(v: number) => { limit = v; load(); }"
        />
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <NCard size="small">
      <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-3)">
        共 {{ filtered.length }} 条{{ kw ? `（已过滤，加载 ${rows.length} 条）` : '' }}。审计按保留策略自动裁剪，仅管理员可见。
      </NText>
      <NDataTable
        size="small" :bordered="false" :loading="loading" :scroll-x="800"
        :max-height="'calc(100vh - 330px)'" :columns="cols" :data="filtered"
        :row-key="(r: any) => r.id"
      >
        <template #empty><EmptyBox text="暂无操作记录" /></template>
      </NDataTable>
    </NCard>
  </div>
</template>
