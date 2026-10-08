<script setup lang="ts">
import { onMounted, ref, h } from 'vue';
import {
  NDataTable, NButton, NSpace, NTag, NModal, NForm, NFormItem, NInput, NInputNumber,
  NSelect, NPopconfirm, useMessage, NIcon, NAlert, NSwitch,
} from 'naive-ui';
import { api, getToken } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const rows = ref<any[]>([]);
const apps = ref<any[]>([]);
const show = ref(false);
const busy = ref<number | null>(null);
const form = ref<any>({ kind: 'pg', target: 'rosetta', schedule: 'daily', hour: 3, minute: 30, keep: 7, enabled: true });

const kindOpts = [
  { label: 'PostgreSQL 数据库', value: 'pg' },
  { label: '应用目录 (tar.gz)', value: 'app' },
];
const schedOpts = [
  { label: '手动执行', value: 'manual' },
  { label: '每日', value: 'daily' },
  { label: '每周一', value: 'weekly' },
];

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

function fmt(n: number) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

const columns: any[] = [
  {
    title: '任务', key: 'target',
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: r.kind === 'pg' ? icons.CubeOutline : icons.FolderOutline, size: 18, color: r.kind === 'pg' ? 'var(--cp-brand-soft)' : 'var(--cp-node)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name' }, r.target),
          r.target_missing
            ? h(NTag, { size: 'tiny', type: 'warning', bordered: false, class: 'cell-sub' }, () => '目标应用已删除，备份将失败')
            : h('div', { class: 'cell-sub' }, r.kind === 'pg' ? 'pg_dump 自定义格式' : '应用目录打包（排除依赖目录）'),
        ]),
      ]),
  },
  {
    title: '计划', key: 'schedule', width: 170,
    render: (r: any) =>
      r.schedule === 'manual'
        ? h(NTag, { size: 'small', bordered: false }, () => '手动')
        : h('span', { class: 'st ' + (r.enabled ? 'ok' : 'idle') }, [
            h('span', { class: 'dot ' + (r.enabled ? 'ok' : 'idle') }),
            `${r.schedule === 'daily' ? '每日' : '每周一'} ${String(r.hour).padStart(2, '0')}:${String(r.minute).padStart(2, '0')}`,
          ]),
  },
  { title: '保留', key: 'keep', width: 70, render: (r: any) => h('span', { class: 'mono-dim' }, `${r.keep} 份`) },
  {
    title: '备份文件', key: 'files',
    render: (r: any) =>
      r.files.length
        ? h(NSpace, { size: 4 }, () =>
            r.files.slice(0, 5).map((f: any) =>
              h(
                NButton,
                { size: 'tiny', tertiary: true, tag: 'a', href: `/api/files/download?path=${encodeURIComponent((r.dir || '/root/backups/panel') + '/' + f.name)}&token=${getToken()}`, target: '_blank' },
                { icon: ico('DownloadOutline', 12), default: () => `${f.name.replace(`bk${r.id}-`, '').replace('.dump.gz', '').replace('.tar.gz', '')} · ${fmt(f.size)}` },
              ),
            ))
        : h('span', { class: 'cell-sub' }, '尚无产物'),
  },
  {
    title: '操作', key: 'ops', width: 170,
    render: (r: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, type: 'primary', loading: busy.value === r.id, icon: ico('PlayOutline'), onClick: () => run(r.id) }, () => '立即备份'),
        h(NPopconfirm, { onPositiveClick: () => del(r.id) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline'), title: '删除备份任务', 'aria-label': '删除备份任务' }, { default: () => '' }),
          default: () => '删除任务及其 systemd timer（已生成的备份文件保留）',
        }),
      ]),
  },
];

async function load() {
  try {
    rows.value = await api.backups();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function run(id: number) {
  busy.value = id;
  try {
    const r = await api.runBackup(id);
    if (r.code === 0) msg.success('备份完成');
    else msg.error(`备份失败 (exit ${r.code})`);
    load();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}
async function del(id: number) {
  try {
    await api.deleteBackup(id);
    msg.success('已删除');
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function create() {
  try {
    await api.createBackup(form.value);
    msg.success('已创建');
    show.value = false;
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}

onMounted(async () => {
  await load();
  try {
    apps.value = (await api.apps()).map((a: any) => ({ label: a.name, value: a.name }));
  } catch { /* 应用列表仅用于下拉建议 */ }
});
</script>

<template>
  <div>
    <PageHeader title="备份" sub="systemd timer 驱动的定时备份，产物存放于 /root/backups/panel/，点击文件名即可下载">
      <template #actions>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
        <NButton size="small" type="primary" :icon="ico('AddOutline')" @click="show = true">新建备份任务</NButton>
      </template>
    </PageHeader>

    <NAlert type="info" :bordered="false" size="small" style="margin-bottom: 14px">
      调度由 systemd timer 独立完成，面板进程停止不影响备份执行。PG 使用 pg_dump 自定义格式（pg_restore 可恢复）；应用打包自动排除 node_modules / .venv / .next / .output。
    </NAlert>

    <NDataTable :columns="columns" :data="rows" size="small" :bordered="false" :scroll-x="980" :row-key="(r: any) => r.id">
      <template #empty><EmptyBox text="还没有备份任务，点击右上角新建" /></template>
    </NDataTable>

    <NModal v-model:show="show" preset="card" title="新建备份任务" style="width: 400px; max-width: 94vw">
      <NForm label-placement="left" label-width="86">
        <NSpace vertical :size="12">
          <NFormItem label="类型"><NSelect v-model:value="form.kind" :options="kindOpts" aria-label="备份类型" /></NFormItem>
          <NFormItem v-if="form.kind === 'pg'" label="数据库">
            <NSpace :size="8">
              <NInput v-model:value="form.target" placeholder="如 rosetta，或 all 全库" style="width: 210px" :input-props="{ 'aria-label': '数据库名称' }" />
              <NButton size="small" tertiary @click="form.target = 'all'">全部</NButton>
            </NSpace>
          </NFormItem>
          <NFormItem v-else label="应用"><NSelect v-model:value="form.target" :options="apps" aria-label="备份应用" /></NFormItem>
          <NFormItem label="计划"><NSelect v-model:value="form.schedule" :options="schedOpts" aria-label="备份计划" /></NFormItem>
          <NFormItem v-if="form.schedule !== 'manual'" label="执行时刻">
            <NSpace align="center" :size="8">
              <NInputNumber v-model:value="form.hour" :min="0" :max="23" :input-props="{ 'aria-label': '执行小时' }" style="width: 84px" /> 时
              <NInputNumber v-model:value="form.minute" :min="0" :max="59" :input-props="{ 'aria-label': '执行分钟' }" style="width: 84px" /> 分（本地时区）
            </NSpace>
          </NFormItem>
          <NSpace justify="space-between" align="center" style="width: 100%">
            <span id="bk-schedule-enabled" style="font-size: var(--fs-sm); color: var(--cp-text-dim)">启用定时计划</span>
            <NSwitch v-model:value="form.enabled" size="small" aria-labelledby="bk-schedule-enabled" />
          </NSpace>
          <NFormItem label="保留份数"><NInputNumber v-model:value="form.keep" :min="1" :max="60" :input-props="{ 'aria-label': '保留备份份数' }" style="width: 130px" /></NFormItem>
          <NButton type="primary" block class="cp-press" :icon="ico('AddOutline')" @click="create">创建任务</NButton>
        </NSpace>
      </NForm>
    </NModal>
  </div>
</template>
