<script setup lang="ts">
import { computed, ref, watch, h } from 'vue';
import { useRouter } from 'vue-router';
import {
  NDataTable, NSpace, NTag, NModal, NForm, NFormItem, NInput, NInputNumber,
  NSelect, NPopconfirm, useMessage, NIcon, NAlert, NSwitch, NText,
} from 'naive-ui';
import { NButton } from '../ui';
import { api } from '../api';
import { icon, icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { useCachedFetch, invalidateCache } from '../composables/useCachedFetch';
import { fmtBytes, fmtDateTime } from '../composables/useFormat';

const msg = useMessage();
const router = useRouter();
const show = ref(false);
const busy = ref<number | null>(null);
const submitting = ref(false);
const editingId = ref<number | null>(null);
const form = ref<any>({ kind: 'pg', target: 'rosetta', schedule: 'daily', hour: 3, minute: 30, keep: 7, enabled: true });

// 备份任务表几乎不变（只有改计划/删除会变），30s 缓存让这一页秒开；
// 应用列表只用于下拉建议与目标名展示，缓存 60s。
const { data, error, loading, refreshing, refresh } = useCachedFetch<any[]>({
  key: () => 'backups',
  loader: () => api.backups(),
  maxAge: 30000,
});
const { data: appsData, load: loadApps } = useCachedFetch<any[]>({
  key: () => 'apps',
  loader: () => api.apps(),
  maxAge: 60000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });
// 原实现在 onMounted 里吞掉应用列表的错误（列表只影响下拉建议），保持一致
void loadApps().catch(() => undefined);

const rows = computed<any[]>(() => data.value ?? []);
const apps = computed<any[]>(() => appsData.value ?? []);

function reload() {
  invalidateCache('backups');
  // 立即备份/删除任务都会写审计，审计页的缓存一起作废
  invalidateCache('audit:');
  return refresh().catch(() => undefined);
}

const kindOpts = [
  { label: 'PostgreSQL 数据库', value: 'pg' },
  { label: '应用目录 (tar.gz)', value: 'app' },
  { label: '应用 SQLite 库（在线一致性快照）', value: 'sqlite' },
];
const schedOpts = [
  { label: '手动执行', value: 'manual' },
  { label: '每日', value: 'daily' },
  { label: '每周一', value: 'weekly' },
];
const KIND_ICON: Record<string, string> = { pg: 'CubeOutline', app: 'FolderOutline', sqlite: 'ServerOutline' };
const KIND_DESC: Record<string, string> = {
  pg: 'pg_dump 自定义格式（pg_restore 可恢复）',
  app: '应用目录打包（排除依赖目录）',
  sqlite: 'SQLite 在线备份 API 快照（自动发现目录内 *.db）',
};

const appOpts = computed(() => apps.value.map((a: any) => ({ label: a.name, value: a.name })));
// sqlite 的 target 存 app_id：应用改名后按名的 target 会指向不存在的目录
const sqliteOpts = computed(() => apps.value.map((a: any) => ({ label: a.name, value: String(a.id) })));
const targetOptions = computed(() => (form.value.kind === 'sqlite' ? sqliteOpts.value : appOpts.value));

const columns: any[] = [
  {
    title: '任务', key: 'target',
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: icons[KIND_ICON[r.kind] || 'FolderOutline'], size: 18, color: r.kind === 'pg' ? 'var(--cp-brand-soft)' : 'var(--cp-node)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name', title: r.kind === 'sqlite' ? sqliteTargetName(r.target) : r.target }, r.kind === 'sqlite' ? sqliteTargetName(r.target) : r.target),
          r.target_missing
            ? h(NTag, { size: 'tiny', type: 'warning', bordered: false, class: 'cell-sub' }, () => '目标应用已删除，备份将失败')
            : h('div', { class: 'cell-sub' }, KIND_DESC[r.kind] || ''),
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
    // 文件明细挪到二级页：这里堆芯片既看不清总量，也没法按时间挑一份出来恢复
    title: '产物', key: 'files', width: 250,
    render: (r: any) =>
      r.files.length
        ? h('div', { class: 'cell-txt' }, [
            h('span', { style: 'font-size:var(--fs-sm); color:var(--cp-text)' }, `${r.files.length} 份 · 合计 ${fmtBytes(r.files.reduce((n: number, f: any) => n + (f.size || 0), 0))}`),
            h('div', { class: 'cell-sub' }, `最近 ${fmtDateTime(r.files[0].mtime)}`),
          ])
        : h('span', { class: 'cell-sub' }, '尚无产物'),
  },
  {
    title: '操作', key: 'ops', width: 336,
    render: (r: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, type: 'primary', loading: busy.value === r.id, icon: icon('PlayOutline', 14), onClick: () => run(r.id) }, () => '立即备份'),
        h(NButton, { size: 'tiny', tertiary: true, disabled: busy.value === r.id, icon: icon('PencilOutline', 14), title: '编辑计划与保留份数', onClick: () => openEdit(r) }, () => '编辑'),
        h(NButton, {
          size: 'tiny', tertiary: true, type: 'warning',
          disabled: !r.files.length,
          title: r.files.length ? '查看/下载/恢复/清理该任务的备份文件' : '还没有备份产物可管理',
          icon: icon('FolderOpenOutline', 14), onClick: () => router.push({ path: '/backups/files', query: { job: String(r.id) } }),
        }, () => '备份文件'),
        h(NPopconfirm, { onPositiveClick: () => del(r.id) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: icon('TrashOutline', 14), title: '删除备份任务' }, { default: () => '删除' }),
          default: () => '删除任务及其 systemd timer（已生成的备份文件保留）',
        }),
      ]),
  },
];

function sqliteTargetName(target: any) {
  const t = String(target || '');
  const hit = apps.value.find((a: any) => String(a.id) === t || a.name === t);
  return hit ? `${hit.name} · SQLite` : `应用 #${t} · SQLite`;
}

async function run(id: number) {
  busy.value = id;
  try {
    const r = await api.runBackup(id);
    if (r.code === 0) msg.success('备份完成');
    else msg.error(`备份失败 (exit ${r.code})`);
    reload();
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
    reload();
  } catch (e: any) {
    msg.error(e.message);
  }
}

function blankForm() {
  return { kind: 'pg', target: 'rosetta', schedule: 'daily', hour: 3, minute: 30, keep: 7, enabled: true };
}
function openCreate() {
  editingId.value = null;
  form.value = blankForm();
  show.value = true;
}
function openEdit(r: any) {
  editingId.value = r.id;
  form.value = {
    kind: r.kind,
    target: r.kind === 'sqlite' ? String(r.target) : r.target,
    schedule: r.schedule,
    hour: r.hour,
    minute: r.minute,
    keep: r.keep,
    enabled: !!r.enabled,
  };
  show.value = true;
}
async function submit() {
  submitting.value = true;
  try {
    if (editingId.value == null) {
      await api.createBackup(form.value);
      msg.success('已创建');
    } else {
      await api.updateBackup(editingId.value, form.value);
      msg.success('已保存');
    }
    show.value = false;
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div>
    <PageHeader title="备份" sub="systemd timer 驱动的定时备份；产物在「备份文件」页统一查看、下载、恢复与清理">
      <template #actions>
        <NButton size="small" tertiary :icon="icon('FolderOpenOutline', 14)" @click="router.push('/backups/files')">备份文件</NButton>
        <NButton size="small" tertiary :loading="loading || refreshing" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
        <NButton size="small" type="primary" :icon="icon('AddOutline', 14)" @click="openCreate">新建备份任务</NButton>
      </template>
    </PageHeader>

    <NAlert type="info" :bordered="false" size="small" style="margin-bottom: var(--space-4)">
      调度由 systemd timer 独立完成，面板进程停止不影响备份执行。PG 用 pg_dump 自定义格式；应用目录打包自动排除 node_modules / .venv / .next / .output；SQLite 走在线备份 API，热备份不会写出页不一致的库。
    </NAlert>

    <NDataTable :columns="columns" :data="rows" size="small" :bordered="false" :loading="loading" :scroll-x="1210" :row-key="(r: any) => r.id">
      <template #empty><EmptyBox text="还没有备份任务，点击右上角新建" /></template>
    </NDataTable>

    <NModal v-model:show="show" preset="card" :title="editingId == null ? '新建备份任务' : '编辑备份任务'" style="width: 420px; max-width: 94vw">
      <NForm label-placement="left" label-width="86">
        <NSpace vertical :size="12">
          <NFormItem label="类型"><NSelect v-model:value="form.kind" :options="kindOpts" aria-label="备份类型" @update:value="form.target = form.kind === 'pg' ? 'rosetta' : null" /></NFormItem>
          <NFormItem v-if="form.kind === 'pg'" label="数据库">
            <NSpace :size="8">
              <NInput v-model:value="form.target" placeholder="如 rosetta，或 all 全库" style="width: 210px" :input-props="{ 'aria-label': '数据库名称' }" />
              <NButton size="small" tertiary @click="form.target = 'all'">全部</NButton>
            </NSpace>
          </NFormItem>
          <NFormItem v-else :label="form.kind === 'sqlite' ? 'SQLite 应用' : '应用'">
            <NSelect v-model:value="form.target" :options="targetOptions" aria-label="备份应用" placeholder="选择应用" />
          </NFormItem>
          <NFormItem v-if="form.kind === 'sqlite'" label="源库文件">
            <NText depth="3" style="font-size: var(--fs-xs); line-height: 1.6">
              自动选取应用目录下体积最大的 *.db / *.sqlite*（忽略 -wal / -shm），备份文件名会记下源库名，恢复时按它精确写回。
            </NText>
          </NFormItem>
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
          <NButton type="primary" block class="cp-press" :loading="submitting" :icon="icon(editingId == null ? 'AddOutline' : 'SaveOutline', 14)" @click="submit">
            {{ editingId == null ? '创建任务' : '保存修改' }}
          </NButton>
        </NSpace>
      </NForm>
    </NModal>
  </div>
</template>
