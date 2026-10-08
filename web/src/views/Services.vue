<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, h, computed } from 'vue';
import { NDataTable, NButton, NSpace, NInput, NCard, useMessage, NIcon } from 'naive-ui';
import { api } from '../api';
import { icons } from '../icons';
import LogStream from '../components/LogStream.vue';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const rows = ref<any[]>([]);
const filter = ref('');
const showAll = ref(false);
const logUnit = ref<string | null>(null);
const loading = ref(false);
const busy = ref<{ unit: string; verb: string } | null>(null);
const busyOn = (unit: string, verb: string) => busy.value?.unit === unit && busy.value?.verb === verb;
const unitBusy = (unit: string) => busy.value?.unit === unit;
// 延迟刷新不能拖过页面卸载：那是一次没人看的请求，失败还会把错误 toast 打到新页面上。
const timers: number[] = [];
function later(fn: () => void, ms: number) {
  timers.push(window.setTimeout(fn, ms));
}
onBeforeUnmount(() => timers.forEach(clearTimeout));

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

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

async function load() {
  loading.value = true;
  try {
    rows.value = await api.services();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    loading.value = false;
  }
}
async function act(unit: string, verb: string) {
  busy.value = { unit, verb };
  try {
    await api.serviceAction(unit, verb);
    later(load, 400);
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
          h(NButton, { text: true, type: 'primary', class: 'cell-name', onClick: () => (logUnit.value = r.unit) }, () => r.unit),
          h('div', { class: 'cell-sub' }, r.desc || '—'),
        ]),
      ]),
  },
  {
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
        h(NButton, { size: 'tiny', tertiary: true, type: 'success', disabled: r.active === 'active' || unitBusy(r.unit), loading: busyOn(r.unit, 'start'), icon: ico('PlayOutline'), onClick: () => act(r.unit, 'start') }, () => '启动'),
        h(NButton, { size: 'tiny', tertiary: true, type: 'warning', disabled: r.active !== 'active' || unitBusy(r.unit), loading: busyOn(r.unit, 'stop'), icon: ico('StopOutline'), onClick: () => act(r.unit, 'stop') }, () => '停止'),
        h(NButton, { size: 'tiny', tertiary: true, disabled: r.active !== 'active' || unitBusy(r.unit), loading: busyOn(r.unit, 'restart'), icon: ico('SyncOutline'), onClick: () => act(r.unit, 'restart') }, () => '重启'),
        h(NButton, { size: 'tiny', quaternary: true, disabled: unitBusy(r.unit), loading: busyOn(r.unit, 'enable'), icon: ico('KeyOutline'), onClick: () => act(r.unit, 'enable') }, () => '自启'),
      ]),
  },
];

onMounted(load);
</script>

<template>
  <div>
    <PageHeader title="系统服务" :sub="`systemd 服务 ${services.length} 个 · ${activeCount} 个运行中${failedCount ? ` · ${failedCount} 个异常` : ''}，点击服务名可看实时日志`">
      <template #actions>
        <NButton size="small" :quaternary="!showAll" :type="showAll ? 'primary' : 'default'" @click="showAll = !showAll">
          {{ showAll ? '只看关键服务' : '查看全部单元' }}
        </NButton>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <NInput :value="filter" @update:value="(v: string) => (filter = v)" placeholder="搜索 unit / 描述…" size="small" clearable style="width: 320px; max-width: 100%; margin-bottom: var(--space-3)">
      <template #prefix><NIcon :component="icons.SearchOutline" /></template>
    </NInput>

    <NDataTable :columns="columns" :data="filtered" size="small" :bordered="false" :loading="loading" :scroll-x="900" :max-height="'calc(100vh - 308px)'" :row-key="(r: any) => r.unit">
      <template #empty><EmptyBox text="没有匹配的服务" /></template>
    </NDataTable>

    <NCard v-if="logUnit" size="small" closable :title="`${logUnit} · journalctl 实时日志`" style="margin-top: var(--space-4)" @close="logUnit = null">
      <LogStream :url-path="`/system/services/${encodeURIComponent(logUnit)}/logs`" />
    </NCard>
  </div>
</template>
