<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NAlert, NDataTable, NForm, NFormItem, NIcon, NInput, NModal, NPopconfirm, NSpace,
  NSwitch, NSelect, NTag, NText, useMessage,
} from 'naive-ui';
import { NButton } from '../ui';
import { api, apiUrl, getRole, getToken } from '../api';
import { icon, icons } from '../icons';
import EmptyBox from '../components/EmptyBox.vue';
import PageHeader from '../components/PageHeader.vue';

const msg = useMessage();
const route = useRoute();
const router = useRouter();
const isAdmin = computed(() => getRole() === 'admin');

const dir = ref('');
const files = ref<any[]>([]);
const loading = ref(false);
const jobFilter = ref('all');
const kindFilter = ref('all');
const pattern = ref('');
const onlyRestoreable = ref(false);
const checked = ref<string[]>([]);
const deleting = ref(false);

// 恢复是不可逆覆盖，确认弹窗要按类型说清"会丢什么"，一句通用文案不够
const RESTORE_WARN: Record<string, string> = {
  sqlite: '会先自动留存当前库，然后停止对应服务、用快照覆盖数据库文件、再启动服务。当前库里备份之后写入的数据会全部丢失。',
  app: '会停止对应服务，并用归档覆盖应用目录下的文件（当前 .panel.env 环境变量会被保留）。',
  pg: '会先 pg_dump 留存当前库，然后 DROP 并重建该库、用 pg_restore 导入。库里现有的表与数据全部替换。',
};
const KIND_LABEL: Record<string, string> = { pg: 'PostgreSQL', app: '应用目录', sqlite: 'SQLite' };
const KIND_ICON: Record<string, string> = { pg: 'CubeOutline', app: 'FolderOutline', sqlite: 'ServerOutline' };

const ico = (name: string, size = 14) => icon(name, size);

function fmt(n: number) {
  let i = 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

function when(iso: string) {
  return (iso || '').replace('T', ' ').replace(/\.\d+Z$/, ' UTC');
}

function jobLabel(f: any) {
  if (f.job_missing) return `任务 #${f.job_id}（已删除）`;
  return f.kind === 'sqlite' ? `#${f.job_id} ${f.target} · SQLite` : `#${f.job_id} ${f.target}`;
}

// 下载复用 /api/files/download：它只允许管理员，所以只读账号看到的是禁用+说明，
// 而不是点开一张 JSON 错误页。
function downloadHref(f: any) {
  if (!isAdmin.value || !dir.value) return '';
  return `${apiUrl('/files/download')}?path=${encodeURIComponent(`${dir.value}/${f.name}`)}&token=${encodeURIComponent(getToken())}`;
}

const shown = computed(() =>
  files.value.filter((f) => {
    if (jobFilter.value !== 'all' && String(f.job_id) !== jobFilter.value) return false;
    if (kindFilter.value === 'orphan' ? !f.job_missing : kindFilter.value !== 'all' && f.kind !== kindFilter.value) return false;
    if (onlyRestoreable.value && !f.restoreable) return false;
    if (pattern.value && !f.name.toLowerCase().includes(pattern.value.toLowerCase())) return false;
    return true;
  }),
);

const totals = computed(() => ({
  count: files.value.length,
  size: files.value.reduce((n, f) => n + (f.size || 0), 0),
  shownSize: shown.value.reduce((n, f) => n + (f.size || 0), 0),
}));

const jobOpts = computed(() => {
  const seen = new Map<string, string>();
  for (const f of files.value) seen.set(String(f.job_id), jobLabel(f));
  return [{ label: '全部任务', value: 'all' }, ...[...seen.entries()].sort().map(([v, l]) => ({ label: l, value: v }))];
});

const kindOpts = [
  { label: '全部类型', value: 'all' },
  { label: 'PostgreSQL', value: 'pg' },
  { label: '应用目录', value: 'app' },
  { label: 'SQLite', value: 'sqlite' },
  { label: '任务已删除的孤儿文件', value: 'orphan' },
];

async function load() {
  loading.value = true;
  try {
    const r = await api.backupFiles();
    dir.value = r.dir || '';
    files.value = r.files || [];
    checked.value = checked.value.filter((n) => files.value.some((f) => f.name === n));
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    loading.value = false;
  }
}

async function delOne(f: any) {
  try {
    await api.deleteBackupFile(f.job_id, f.name);
    msg.success(`已删除 ${f.name}`);
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}

// 批量删除走串行：一次请求打满磁盘目录反而看不清是哪一步失败，
// 而且 prune 本身也是按名字顺序删的。
async function delChecked() {
  const targets = files.value.filter((f) => checked.value.includes(f.name));
  if (!targets.length) return;
  deleting.value = true;
  let ok = 0;
  const failed: string[] = [];
  for (const f of targets) {
    try {
      await api.deleteBackupFile(f.job_id, f.name);
      ok += 1;
    } catch (e: any) {
      failed.push(`${f.name}：${e.message}`);
    }
  }
  deleting.value = false;
  checked.value = [];
  if (failed.length) msg.error(`删除 ${ok} 个，失败 ${failed.length} 个：${failed[0]}`);
  else msg.success(`已删除 ${ok} 个备份文件`);
  load();
}

// ---------- 上传产物 ----------
const showUpload = ref(false);
const uploadJob = ref<number | null>(null);
const uploadFile = ref<File | null>(null);
const uploadOverwrite = ref(false);
const uploading = ref(false);
const uploadJobs = ref<{ label: string; value: number }[]>([]);

async function openUpload() {
  uploadJob.value = null;
  uploadFile.value = null;
  uploadOverwrite.value = false;
  showUpload.value = true;
  try {
    const jobs = await api.backups();
    // 没有产物的任务在 jobOpts（由文件列表推导）里根本不出现，所以上传要用完整任务表
    uploadJobs.value = (jobs || []).map((j: any) => ({
      label: `#${j.id} ${j.target} · ${KIND_LABEL[j.kind] || j.kind}`,
      value: j.id,
    }));
  } catch (e: any) {
    msg.error(e.message);
  }
}
function pickUploadFile() {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.onchange = () => { uploadFile.value = inp.files?.[0] || null; };
  inp.click();
}
async function doUpload() {
  const f = uploadFile.value;
  const job = uploadJob.value;
  if (!f || !job) return;
  uploading.value = true;
  try {
    const form = new FormData();
    form.append('file', f, f.name);
    if (uploadOverwrite.value) form.append('overwrite', '1');
    // 走原始 fetch：api.ts 的 req() 固定带 JSON Content-Type，会让服务端解析不到 multipart 边界
    const res = await fetch(apiUrl(`/backups/${job}/upload`), {
      method: 'POST',
      headers: { Authorization: `Bearer ${getToken()}` },
      body: form,
    });
    const data = await res.json().catch(() => ({}) as any);
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    msg.success(`已登记 ${data.name}（${fmt(data.size)}）`);
    showUpload.value = false;
    load();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    uploading.value = false;
  }
}

// ---------- 恢复 ----------
const showRestore = ref(false);
const restoreTarget = ref<any>(null);
const restoring = ref(false);
const restoreLog = ref('');
const restoreErr = ref('');

function openRestore(f: any) {
  restoreTarget.value = f;
  restoreLog.value = '';
  restoreErr.value = '';
  showRestore.value = true;
}

async function doRestore() {
  const f = restoreTarget.value;
  if (!f) return;
  restoring.value = true;
  restoreErr.value = '';
  try {
    const r = await api.restoreBackup(f.job_id, f.name);
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

const columns: any[] = [
  { type: 'selection', disabled: () => !isAdmin.value },
  {
    title: '备份文件', key: 'name', minWidth: 300,
    render: (f: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: icons[KIND_ICON[f.kind] || 'DocumentOutline'], size: 17, color: f.job_missing ? 'var(--cp-warn)' : f.kind === 'pg' ? 'var(--cp-brand-soft)' : 'var(--cp-node)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          // 备份文件名是长等宽串（库名+时间戳），必须走 .cell-name 的 ellipsis，
          // 只用 .mono 的话在窄列里被 td 裁成半截、还看不到全名
          h('span', { class: 'cell-name mono', title: f.name }, f.name),
          h('div', { class: 'cell-sub', title: jobLabel(f) }, jobLabel(f)),
        ]),
      ]),
  },
  {
    title: '类型', key: 'kind', width: 110,
    render: (f: any) => h(NTag, { size: 'small', bordered: false, type: f.job_missing ? 'warning' : 'default' }, () => KIND_LABEL[f.kind] || '未知'),
  },
  { title: '生成时间', key: 'mtime', width: 210, sorter: 'default', render: (f: any) => h('span', { class: 'mono-dim', title: when(f.mtime) }, when(f.mtime)) },
  { title: '大小', key: 'size', width: 100, sorter: 'default', render: (f: any) => h('span', { class: 'mono-dim' }, fmt(f.size)) },
  {
    title: '状态', key: 'state', width: 130,
    render: (f: any) =>
      f.job_missing
        ? h(NTag, { size: 'tiny', bordered: false, type: 'warning' }, () => '任务已删除')
        : f.restoreable
          ? h('span', { class: 'st ok' }, [h('span', { class: 'dot ok' }), '可一键恢复'])
          : h('span', { class: 'st idle' }, [h('span', { class: 'dot idle' }), '仅可下载']),
  },
  {
    title: '操作', key: 'ops', width: 240,
    render: (f: any) => {
      const href = downloadHref(f);
      return h(NSpace, { size: 6 }, () => [
        h(
          NButton,
          href
            ? { size: 'tiny', tertiary: true, tag: 'a', href, target: '_blank', rel: 'noopener noreferrer', title: `下载 ${f.name}`, icon: ico('DownloadOutline', 12) }
            : { size: 'tiny', tertiary: true, disabled: true, icon: ico('DownloadOutline', 12), title: isAdmin.value ? '备份目录未知，无法下载' : '仅管理员可下载备份文件' },
          { default: () => '下载' },
        ),
        h(
          NButton,
          {
            size: 'tiny', tertiary: true, type: 'warning',
            disabled: !isAdmin.value || !f.restoreable,
            title: !isAdmin.value ? '仅管理员可恢复' : f.restoreable ? `用 ${f.name} 覆盖当前数据` : '该类型不支持自动恢复',
            icon: ico('RefreshCircleOutline', 12),
            onClick: () => openRestore(f),
          },
          { default: () => '恢复' },
        ),
        h(NPopconfirm, { onPositiveClick: () => delOne(f) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', disabled: !isAdmin.value, icon: ico('TrashOutline', 12), title: `删除 ${f.name}` }, { default: () => '删除' }),
          default: () => `删除后不可找回，且会永久占用一个保留名额直到下次备份。确认删除 ${f.name}？`,
        }),
      ]);
    },
  },
];

onMounted(() => {
  const q = route.query.job;
  if (typeof q === 'string' && /^\d+$/.test(q)) jobFilter.value = q;
  load();
});
</script>

<template>
  <div>
    <PageHeader
      title="备份文件"
      :sub="`共 ${totals.count} 个 · 合计 ${fmt(totals.size)}；当前筛选 ${shown.length} 个 · ${fmt(totals.shownSize)}。产物目录 ${dir || '—'}`"
    >
      <template #actions>
        <NPopconfirm v-if="isAdmin && checked.length" @positive-click="delChecked">
          <template #trigger>
            <NButton size="small" tertiary type="error" :loading="deleting" :icon="ico('TrashOutline')">
              删除所选 {{ checked.length }} 个
            </NButton>
          </template>
          逐个删除选中的 {{ checked.length }} 个备份文件？删除后不可找回。
        </NPopconfirm>
        <NButton size="small" tertiary :icon="ico('ArrowBackOutline')" @click="router.push('/backups')">返回备份任务</NButton>
        <NButton v-if="isAdmin" size="small" tertiary :icon="ico('CloudUploadOutline')" @click="openUpload">上传产物</NButton>
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <NSpace align="center" :size="10" wrap style="margin-bottom: var(--space-3)">
      <NSelect v-model:value="jobFilter" :options="jobOpts" size="small" style="width: 230px" placeholder="全部任务" aria-label="按备份任务筛选" />
      <NSelect v-model:value="kindFilter" :options="kindOpts" size="small" style="width: 190px" placeholder="全部类型" aria-label="按备份类型筛选" />
      <NInput v-model:value="pattern" size="small" clearable placeholder="搜索文件名…" style="width: 240px" :input-props="{ 'aria-label': '备份文件名' }">
        <template #prefix><NIcon :component="icons.SearchOutline" /></template>
      </NInput>
      <NSpace align="center" :size="6">
        <NSwitch v-model:value="onlyRestoreable" size="small" aria-label="只看可恢复" />
        <NText depth="3" style="font-size: var(--fs-xs)">只看可恢复</NText>
      </NSpace>
    </NSpace>

    <NAlert v-if="isAdmin && totals.count" type="info" :bordered="false" size="small" style="margin-bottom: var(--space-3)">
      保留份数由任务的 keep 自动裁剪（按文件名排序删最旧的）；这里手动删除是腾空间的补充手段，删掉的文件不会由 timer 重建。
    </NAlert>

    <NDataTable
      v-model:checked-row-keys="checked"
      :columns="columns"
      :data="shown"
      size="small"
      :bordered="false"
      :loading="loading"
      :scroll-x="1240"
      class="cp-tbl tbl-backup-files"
      :max-height="'var(--table-h)'"
      :row-key="(f: any) => f.name"
    >
      <template #empty><EmptyBox :text="files.length ? '没有匹配当前筛选的备份文件' : '还没有任何备份产物，先到备份任务页立即备份一次'" /></template>
    </NDataTable>

    <NModal v-model:show="showRestore" preset="card" title="从该备份文件恢复" style="width: 520px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NAlert type="warning" :bordered="false" size="small" role="alert">
          {{ RESTORE_WARN[restoreTarget?.kind] || '恢复会用备份内容覆盖当前数据，操作不可撤销。' }}
        </NAlert>
        <NForm label-placement="left" label-width="76" :show-feedback="false">
          <NFormItem label="文件"><NText class="mono cp-break" style="font-size: var(--fs-sm); min-width: 0">{{ restoreTarget?.name }}</NText></NFormItem>
          <NFormItem label="所属任务"><NText style="font-size: var(--fs-sm)">{{ restoreTarget ? jobLabel(restoreTarget) : '' }}</NText></NFormItem>
          <NFormItem label="生成时间"><NText style="font-size: var(--fs-sm)">{{ when(restoreTarget?.mtime) }} · {{ fmt(restoreTarget?.size || 0) }}</NText></NFormItem>
        </NForm>
        <NAlert v-if="restoreErr" type="error" :bordered="false" size="small" role="alert">{{ restoreErr }}</NAlert>
        <NAlert v-if="restoreLog" type="success" :bordered="false" size="small" role="status">
          <pre class="mono-pre">{{ restoreLog }}</pre>
        </NAlert>
        <NSpace justify="end" :size="8">
          <NButton tertiary :disabled="restoring" @click="showRestore = false">关闭</NButton>
          <NPopconfirm @positive-click="doRestore">
            <template #trigger>
              <NButton type="warning" class="cp-press" :loading="restoring" :disabled="restoring" :icon="ico('RefreshCircleOutline')">恢复覆盖</NButton>
            </template>
            覆盖后当前数据不可找回（仅能再退回本次自动留存的 pre-restore 快照），确认执行？
          </NPopconfirm>
        </NSpace>
      </NSpace>
    </NModal>

    <NModal v-model:show="showUpload" preset="card" title="上传备份产物" style="width: 520px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NText depth="3" style="font-size: var(--fs-xs)">
          把在别处生成的产物登记进某个任务名下：服务端会补 bk&lt;任务号&gt;- 前缀，恢复与按名清理才认得它。pg 要 .dump.gz、SQLite 要 .sqlite.gz、应用目录要 .tar.gz/.tgz（其它格式恢复逻辑读不了，传进来也只能干放着，所以服务端会直接拒绝）。
        </NText>
        <NForm label-placement="left" label-width="88" :show-feedback="false">
          <NFormItem label="目标任务">
            <NSelect v-model:value="uploadJob" :options="uploadJobs" size="small" placeholder="选择备份任务" aria-label="上传到哪个备份任务" />
          </NFormItem>
          <NFormItem label="文件">
            <NSpace :size="8" align="center" style="min-width: 0">
              <NButton size="small" tertiary :icon="ico('CloudUploadOutline')" @click="pickUploadFile">选择文件</NButton>
              <NText v-if="uploadFile" class="mono cp-break" style="font-size: var(--fs-xs); min-width: 0">
                {{ uploadFile.name }} · {{ fmt(uploadFile.size) }}
              </NText>
              <NText v-else depth="3" style="font-size: var(--fs-xs)">尚未选择</NText>
            </NSpace>
          </NFormItem>
          <NFormItem label="同名时">
            <NSpace align="center" :size="6">
              <NSwitch v-model:value="uploadOverwrite" size="small" aria-label="覆盖同名产物" />
              <NText depth="3" style="font-size: var(--fs-xs)">覆盖任务目录里的同名产物（原文件不可找回）</NText>
            </NSpace>
          </NFormItem>
        </NForm>
        <NSpace justify="end" :size="8">
          <NButton tertiary :disabled="uploading" @click="showUpload = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="uploading" :disabled="!uploadFile || !uploadJob" @click="doUpload">上传</NButton>
        </NSpace>
      </NSpace>
    </NModal>
  </div>
</template>

<style scoped>
.mono-pre { margin: 0; font-family: var(--font-mono); font-size: var(--fs-xs); white-space: pre-wrap; word-break: break-all; }
</style>
