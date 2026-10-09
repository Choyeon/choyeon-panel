<script setup lang="ts">
import { computed, ref, watch, h } from 'vue';
import { NDataTable, NSpace, NInput, NCard, NText, useMessage, NIcon } from 'naive-ui';
import { NButton } from '../ui';
import { api } from '../api';
import { icon, icons } from '../icons';
import LogStream from '../components/LogStream.vue';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { useCachedFetch, invalidateCache } from '../composables/useCachedFetch';
import { useDelayedRefresh } from '../composables/useDelayedRefresh';
import { useTableHeight } from '../composables/useTableHeight';

const msg = useMessage();
const filter = ref('');
const showAll = ref(false);
const logUnit = ref<string | null>(null);
const busy = ref<{ unit: string; verb: string } | null>(null);
const busyOn = (unit: string, verb: string) => busy.value?.unit === unit && busy.value?.verb === verb;
const unitBusy = (unit: string) => busy.value?.unit === unit;

// systemd 的 unit 列表变化很快（启停之后 active 还要一会儿才落地），缓存给得很短：
// 秒开靠它，15s 后再进这一页仍会后台回源，不会读到半天的旧状态。
const { data, error, loading, refreshing, refresh } = useCachedFetch<any[]>({
  key: () => 'services',
  loader: () => api.services(),
  maxAge: 15000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });

const rows = computed<any[]>(() => data.value ?? []);
// 延迟刷新不能拖过页面卸载：那是一次没人看的请求，失败还会把错误 toast 打到新页面上。
const { pending: pendingRefresh, later } = useDelayedRefresh();
const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

function reload() {
  invalidateCache('services');
  // 启停/自启都会写一条审计：审计页的缓存同样作废，切过去不会看到旧记录
  invalidateCache('audit:');
  return refresh().catch(() => undefined);
}

const important = ['nginx', 'postgresql', 'redis', 'rosetta', 'choyeon', 'ssh', 'panel-'];
const services = computed(() => rows.value.filter((r) => r.unit.endsWith('.service')));
const activeCount = computed(() => services.value.filter((r) => r.active === 'active').length);
const failedCount = computed(() => services.value.filter((r) => r.active === 'failed').length);
const filtered = computed(() => {
  let list = services.value;
  if (!showAll.value) list = list.filter((r) => important.some((k) => r.unit.toLowerCase().includes(k)) || r.active === 'active');
  if (filter.value) list = list.filter((r) => (r.unit + r.desc).toLowerCase().includes(filter.value.toLowerCase()));
  return list;
});

async function act(unit: string, verb: string) {
  busy.value = { unit, verb };
  try {
    await api.serviceAction(unit, verb);
    later(reload, 400);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}

const columns: any[] = [
  {
    title: '服务', key: 'unit',
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: icons.ServerOutline, size: 17, color: r.active === 'active' ? 'var(--cp-ok)' : r.active === 'failed' ? 'var(--cp-err)' : 'var(--cp-text-mute)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h(NButton, { text: true, type: 'primary', class: 'cell-name', title: r.unit, onClick: () => (logUnit.value = r.unit) }, () => r.unit),
          h('div', { class: 'cell-sub', title: r.desc || '—' }, r.desc || '—'),
        ]),
      ]),
  },
  {
    // 状态不只用颜色：文字（运行中/异常/已停止）始终在场，色点只是加强
    title: '状态', key: 'active', width: 100,
    render: (r: any) =>
      h('span', { class: 'st ' + (r.active === 'active' ? 'ok' : r.active === 'failed' ? 'err' : 'idle') }, [
        h('span', { class: 'dot ' + (r.active === 'active' ? 'ok' : r.active === 'failed' ? 'err' : 'idle') }),
        r.active === 'active' ? '运行中' : r.active === 'failed' ? '异常' : '已停止',
      ]),
  },
  {
    title: '操作', key: 'ops', width: 300,
    render: (r: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, type: 'success', disabled: r.active === 'active' || unitBusy(r.unit), loading: busyOn(r.unit, 'start'), icon: icon('PlayOutline', 14), onClick: () => act(r.unit, 'start') }, () => '启动'),
        h(NButton, { size: 'tiny', tertiary: true, type: 'warning', disabled: r.active !== 'active' || unitBusy(r.unit), loading: busyOn(r.unit, 'stop'), icon: icon('StopOutline', 14), onClick: () => act(r.unit, 'stop') }, () => '停止'),
        h(NButton, { size: 'tiny', tertiary: true, disabled: r.active !== 'active' || unitBusy(r.unit), loading: busyOn(r.unit, 'restart'), icon: icon('SyncOutline', 14), onClick: () => act(r.unit, 'restart') }, () => '重启'),
        h(NButton, { size: 'tiny', quaternary: true, disabled: unitBusy(r.unit), loading: busyOn(r.unit, 'enable'), icon: icon('KeyOutline', 14), onClick: () => act(r.unit, 'enable') }, () => '自启'),
      ]),
  },
];

// unit 数动辄几百行，一次全渲染会让首屏卡顿；分页只渲染当前页。
// 不传 page/pageSize 之外的受控字段：分页状态交给 naive 自己维护。
const paging = { pageSize: 50, showSizePicker: false, showQuickJumper: false };
</script>

<template>
  <div>
    <PageHeader title="系统服务" :sub="`systemd 服务 ${services.length} 个 · ${activeCount} 个运行中${failedCount ? ` · ${failedCount} 个异常` : ''}，点击服务名可看实时日志`">
      <template #actions>
        <NText v-if="pendingRefresh" depth="3" style="font-size: var(--fs-xs)">状态回读中…</NText>
        <NButton size="small" :quaternary="!showAll" :type="showAll ? 'primary' : 'default'" @click="showAll = !showAll">
          {{ showAll ? '只看关键服务' : '查看全部单元' }}
        </NButton>
        <NButton size="small" tertiary :loading="loading || refreshing || pendingRefresh" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
      </template>
    </PageHeader>

    <NInput :value="filter" @update:value="(v: string) => (filter = v)" placeholder="搜索 unit / 描述…" size="small" clearable style="width: 320px; max-width: 100%; margin-bottom: var(--space-3)">
      <template #prefix><NIcon :component="icons.SearchOutline" /></template>
    </NInput>

    <NDataTable
      ref="tableAnchor"
      :columns="columns" :data="filtered" size="small" :bordered="false" :loading="loading"
      :scroll-x="900" :max-height="tableH" :pagination="paging" :row-key="(r: any) => r.unit"
    >
      <template #empty><EmptyBox text="没有匹配的服务" /></template>
    </NDataTable>

    <NCard v-if="logUnit" size="small" closable :title="`${logUnit} · journalctl 实时日志`" style="margin-top: var(--space-4)" @close="logUnit = null">
      <LogStream :url-path="`/system/services/${encodeURIComponent(logUnit)}/logs`" />
    </NCard>
  </div>
</template>
