<script setup lang="ts">
import { computed, onMounted, ref, h } from 'vue';
import {
  NDataTable, NSpace, NTag, NModal, NForm, NFormItem, NInput, NInputNumber,
  NSelect, NPopconfirm, useMessage, NIcon, NAlert, NSwitch, NText,
} from 'naive-ui';
import { NButton } from '../ui';
import { api, apiUrl, getRole, getToken } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const isAdmin = computed(() => getRole() === 'admin');
const rows = ref<any[]>([]);
const apps = ref<any[]>([]);
const show = ref(false);
const busy = ref<number | null>(null);
const submitting = ref(false);
const editingId = ref<number | null>(null);
const form = ref<any>({ kind: 'pg', target: 'rosetta', schedule: 'daily', hour: 3, minute: 30, keep: 7, enabled: true });

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

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

function fmt(n: number) {
  let i = 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

// 文件名芯片：去掉 bk<id>- 前缀与压缩后缀，只留人看得懂的时间戳/库名
function fileLabel(r: any, f: any) {
  return f.name
    .replace(`bk${r.id}-`, '')
    .replace(/\.(dump|tar|sqlite)(\.gz)?$/i, '')
    + (f.size ? ` · ${fmt(f.size)}` : '');
}

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
          h('span', { class: 'cell-name' }, r.kind === 'sqlite' ? sqliteTargetName(r.target) : r.target),
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
    title: '备份文件', key: 'files',
    render: (r: any) =>
      r.files.length
        ? h(NSpace, { size: 4 }, () =>
            r.files.slice(0, 5).map((f: any) =>
              h(
                NButton,
                // 下载走 /api/files/download，该接口现在只允许管理员：
                // 只读账号点开的是一张 JSON 错误页，不如直接不给这个入口。
                // 没有 dir（还没产出过备份文件）时同样不给，否则链接指向 /<文件名>，必报路径超范围。
                isAdmin.value && r.dir
                  ? { size: 'tiny', tertiary: true, tag: 'a', href: `${apiUrl('/files/download')}?path=${encodeURIComponent(`${r.dir}/${f.name}`)}&token=${encodeURIComponent(getToken())}`, target: '_blank' }
                  : { size: 'tiny', tertiary: true, title: isAdmin.value ? '备份目录未知，无法下载' : '仅管理员可下载备份文件' },
                { icon: ico('DownloadOutline', 12), default: () => fileLabel(r, f) },
              ),
            ))
        : h('span', { class: 'cell-sub' }, '尚无产物'),
  },
  {
    title: '操作', key: 'ops', width: 336,
    render: (r: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, type: 'primary', loading: busy.value === r.id, icon: ico('PlayOutline'), onClick: () => run(r.id) }, () => '立即备份'),
        h(NButton, { size: 'tiny', tertiary: true, disabled: busy.value === r.id, icon: ico('PencilOutline'), title: '编辑计划与保留份数', onClick: () => openEdit(r) }, () => '编辑'),
        h(NButton, {
          size: 'tiny', tertiary: true, type: 'warning',
          disabled: busy.value === r.id || !isAdmin.value || !(r.files || []).some((f: any) => f.restoreable),
          icon: ico('RefreshCircleOutline'), title: isAdmin.value ? '从备份文件恢复' : '仅管理员可恢复',
          onClick: () => openRestore(r),
        }, () => '恢复'),
        h(NPopconfirm, { onPositiveClick: () => del(r.id) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline'), title: '删除备份任务' }, { default: () => '删除' }),
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

const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    rows.value = await api.backups();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    loading.value = false;
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
    load();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    submitting.value = false;
  }
}

// ---------- 恢复 ----------
const showRestore = ref(false);
const restoreJob = ref<any>(null);
const restoreFile = ref('');
const restoring = ref(false);
const restoreLog = ref('');
const restoreErr = ref('');

const restoreFiles = computed(() =>
  (restoreJob.value?.files || [])
    .filter((f: any) => f.restoreable)
    .map((f: any) => ({ label: `${f.name.replace(`bk${restoreJob.value.id}-`, '')} · ${fmt(f.size)} · ${f.mtime?.slice?.(0, 19).replace('T', ' ') || ''}`.trim(), value: f.name })),
);

const RESTORE_WARN: Record<string, string> = {
  sqlite: '会先自动留存当前库，然后停止对应服务、用快照覆盖数据库文件、再启动服务。当前库里备份之后写入的数据会全部丢失。',
  app: '会停止对应服务，并用归档覆盖应用目录下的文件（当前 .panel.env 环境变量会被保留）。',
  pg: '会先 pg_dump 留存当前库，然后 DROP 并重建该库、用 pg_restore 导入。库里现有的表与数据全部替换。',
};

function openRestore(r: any) {
  restoreJob.value = r;
  restoreLog.value = '';
  restoreErr.value = '';
  const first = (r.files || []).find((f: any) => f.restoreable);
  restoreFile.value = first ? first.name : '';
  showRestore.value = true;
}

async function doRestore() {
  if (!restoreFile.value) return;
  restoring.value = true;
  restoreErr.value = '';
  try {
    const r = await api.restoreBackup(restoreJob.value.id, restoreFile.value);
    restoreLog.value = r.log || '恢复完成';
    msg.success('恢复完成');
    load();
  } catch (e: any) {
    restoreErr.value = e.message;
    msg.error(e.message);
  } finally {
    restoring.value = false;
  }
}

onMounted(async () => {
  await load();
  try {
    apps.value = await api.apps();
  } catch { /* 应用列表仅用于下拉建议与目标名展示 */ }
});
</script>

<template>
  <div>
    <PageHeader title="备份" sub="systemd timer 驱动的定时备份，产物存放于备份目录，点击文件名即可下载，选中版本可一键恢复">
      <template #actions>
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
        <NButton size="small" type="primary" :icon="ico('AddOutline')" @click="openCreate">新建备份任务</NButton>
      </template>
    </PageHeader>

    <NAlert type="info" :bordered="false" size="small" style="margin-bottom: var(--space-4)">
      调度由 systemd timer 独立完成，面板进程停止不影响备份执行。PG 用 pg_dump 自定义格式；应用目录打包自动排除 node_modules / .venv / .next / .output；SQLite 走在线备份 API，热备份不会写出页不一致的库。
    </NAlert>

    <NDataTable :columns="columns" :data="rows" size="small" :bordered="false" :loading="loading" :scroll-x="1160" :row-key="(r: any) => r.id">
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
          <NButton type="primary" block class="cp-press" :loading="submitting" :icon="ico(editingId == null ? 'AddOutline' : 'SaveOutline')" @click="submit">
            {{ editingId == null ? '创建任务' : '保存修改' }}
          </NButton>
        </NSpace>
      </NForm>
    </NModal>

    <NModal v-model:show="showRestore" preset="card" title="从备份恢复" style="width: 480px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NAlert type="warning" :bordered="false" size="small" role="alert">
          {{ RESTORE_WARN[restoreJob?.kind] || '恢复会用备份内容覆盖当前数据，操作不可撤销。' }}
        </NAlert>
        <NFormItem label="任务" :show-feedback="false">
          <NText style="font-size: var(--fs-sm)">
            {{ restoreJob ? `${restoreJob.kind} · ${restoreJob.kind === 'sqlite' ? sqliteTargetName(restoreJob.target) : restoreJob.target}` : '' }}
          </NText>
        </NFormItem>
        <NFormItem label="选择版本" :show-feedback="false">
          <NSelect v-model:value="restoreFile" :options="restoreFiles" placeholder="选择要恢复的备份文件" aria-label="备份文件" />
        </NFormItem>
        <NAlert v-if="restoreErr" type="error" :bordered="false" size="small" role="alert">{{ restoreErr }}</NAlert>
        <NAlert v-if="restoreLog" type="success" :bordered="false" size="small" role="status">
          <pre class="mono-pre">{{ restoreLog }}</pre>
        </NAlert>
        <NSpace justify="end" :size="8">
          <NButton tertiary @click="showRestore = false">关闭</NButton>
          <NPopconfirm @positive-click="doRestore">
            <template #trigger>
              <NButton type="warning" class="cp-press" :loading="restoring" :disabled="!restoreFile || restoring" :icon="ico('RefreshCircleOutline')">恢复覆盖</NButton>
            </template>
            覆盖后当前数据不可找回（仅能再退回本次自动留存的 pre-restore 快照），确认执行？
          </NPopconfirm>
        </NSpace>
      </NSpace>
    </NModal>
  </div>
</template>

<style scoped>
.mono-pre { margin: 0; font-family: var(--font-mono, ui-monospace, monospace); font-size: var(--fs-xs); white-space: pre-wrap; word-break: break-all; }
</style>
