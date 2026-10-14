<script setup lang="ts">
import { computed, h, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NAlert, NDataTable, NDrawer, NDrawerContent, NForm, NFormItem, NIcon, NInput, NModal, NPopconfirm, NSpace,
  NSwitch, NSelect, NTag, NText, useMessage,
} from 'naive-ui';
import { NButton, useDrawerWidth } from '../ui';
import { api, apiUrl, getRole, getToken } from '../api';
import { icon, icons } from '../icons';
import EmptyBox from '../components/EmptyBox.vue';
import CpToolbarCard from '../components/CpToolbarCard.vue';
import CpRowActions from '../components/CpRowActions.vue';
import CpStatusTag from '../components/CpStatusTag.vue';
import ConfirmActionDialog from '../components/ConfirmActionDialog.vue';
import { useTableHeight } from '../composables/useTableHeight';
import { fmtBytes, fmtDateTime } from '../composables/useFormat';
import { useVisiblePoll } from '../composables/useVisiblePoll';

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
const checked = ref<string[]>([]);
const deleting = ref(false);

const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

// 恢复是不可逆覆盖，确认弹窗要按类型说清"会丢什么"，一句通用文案不够
const RESTORE_WARN: Record<string, string> = {
  sqlite: '会先自动留存当前库，然后停止对应服务、用快照覆盖数据库文件、再启动服务。当前库里备份之后写入的数据会全部丢失。',
  app: '会停止对应服务，并用归档覆盖应用目录下的同名文件（当前 .panel.env 环境变量会被保留）。归档是增量覆盖：目录里备份之后新增的文件不会被删除。',
  pg: '会先 pg_dump 留存当前库，然后 DROP 并重建该库、用 pg_restore 导入。库里现有的表与数据全部替换。',
};
// "能不能退回本次覆盖之前的版本"按类型差别很大：pg 与 sqlite 会自动留 pre-restore 快照，
// 应用目录恢复不会（整份目录快照在几百 MB 的 node_modules 上等于再吃一份磁盘，
// 而且可能当场把盘写满）。文案不能笼统承诺"可退回"，那是给了一次不存在的安全网。
const RESTORE_ROLLBACK: Record<string, string> = {
  sqlite: '覆盖前会自动留存当前库为 pre-restore 快照，可再从本页退回。确认执行？',
  pg: '覆盖前会自动留存当前库为 pre-restore 快照，可再从本页退回。确认执行？',
  app: '应用目录不会整份留存快照（只保留当前 .panel.env），本次覆盖没有自动退路。确认执行？',
};
const KIND_LABEL: Record<string, string> = { pg: 'PostgreSQL', app: '应用目录', sqlite: 'SQLite' };
const KIND_ICON: Record<string, string> = { pg: 'CubeOutline', app: 'FolderOutline', sqlite: 'ServerOutline' };

const ico = (name: string, size = 14) => icon(name, size);

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
function download(f: any) {
  const href = downloadHref(f);
  if (!href) return;
  const a = document.createElement('a');
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.click();
}

const shown = computed(() =>
  files.value.filter((f) => {
    if (jobFilter.value !== 'all' && String(f.job_id) !== jobFilter.value) return false;
    if (kindFilter.value === 'orphan') { if (!f.job_missing) return false; }
    else if (kindFilter.value === 'restoreable') { if (!f.restoreable) return false; }
    else if (kindFilter.value !== 'all' && f.kind !== kindFilter.value) return false;
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

// 「仅可恢复」并入类型筛选：1Panel 的习惯是筛选都在下拉+搜索里
const kindOpts = [
  { label: '全部类型', value: 'all' },
  { label: 'PostgreSQL', value: 'pg' },
  { label: '应用目录', value: 'app' },
  { label: 'SQLite', value: 'sqlite' },
  { label: '仅可恢复', value: 'restoreable' },
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

// 1Panel TableSetting 自动刷新：0/5/10/30/60/120/300s，localStorage 记忆
const AUTO_OPTS = [0, 5, 10, 30, 60, 120, 300].map((s) => ({ label: s ? `${s} 秒` : '关闭自动刷新', value: s }));
const autoSec = ref(Math.max(0, Number(localStorage.getItem('backup-files-auto-refresh')) || 0));
// 用增到 5s 档时，后台标签页每分钟白打一次 backupFiles（列目录 + 算大小）。
const autoPoll = useVisiblePoll(() => void load(), autoSec.value * 1000);
watch(autoSec, (v) => {
  localStorage.setItem('backup-files-auto-refresh', String(v));
  autoPoll.every(v * 1000);
});

const delDialog = ref<InstanceType<typeof ConfirmActionDialog> | null>(null);
function confirmDelOne(f: any) {
  delDialog.value?.acceptParams({
    title: '确认删除备份文件',
    names: [f.name],
    msg: '删除后不可找回，且会永久占用一个保留名额直到下次备份。',
    api: async () => { await api.deleteBackupFile(f.job_id, f.name); await load(); },
    successMsg: `已删除 ${f.name}`,
  });
}

// 批量删除走串行：一次请求打满磁盘目录反而看不清是哪一步失败。
function confirmDelChecked() {
  const targets = files.value.filter((f) => checked.value.includes(f.name));
  if (!targets.length) return;
  delDialog.value?.acceptParams({
    title: `确认删除 ${targets.length} 个备份文件`,
    names: targets.map((f) => f.name),
    msg: '逐个删除选中的备份文件，删除后不可找回。',
    api: async () => {
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
      if (failed.length) throw new Error(`删除 ${ok} 个，失败 ${failed.length} 个：${failed[0]}`);
      await load();
    },
    successMsg: `已删除 ${targets.length} 个备份文件`,
  });
}

// ---------- 上传产物 ----------
const showUpload = ref(false);
const drawerW = useDrawerWidth(0.4);
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
    msg.success(`已登记 ${data.name}（${fmtBytes(data.size)}）`);
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
const restoreWarn = ref(false);
const restoreErr = ref('');

function openRestore(f: any) {
  restoreTarget.value = f;
  restoreLog.value = '';
  restoreWarn.value = false;
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
    // ok:true 只说明数据装上了。日志里带 `!!` 的行是"有一件事必须人来接手"
    // （服务没起来、按最大库定位、归档环境变量生效），旧写法一律报绿色"恢复完成"，
    // 成功提示正好把这些行盖住。
    restoreWarn.value = Array.isArray(r.warn) && r.warn.length > 0;
    if (restoreWarn.value) msg.warning(`恢复已执行，但有 ${r.warn.length} 项需要人工确认（见执行记录）`);
    else msg.success('恢复完成');
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
          h('div', { class: 'cell-sub cp-prose', title: jobLabel(f) }, jobLabel(f)),
        ]),
      ]),
  },
  {
    title: '类型', key: 'kind', width: 110,
    render: (f: any) => h(NTag, { size: 'small', bordered: false, type: f.job_missing ? 'warning' : 'default' }, () => KIND_LABEL[f.kind] || '未知'),
  },
  { title: '生成时间 (UTC)', key: 'mtime', width: 180, sorter: 'default', render: (f: any) => h('span', { class: 'mono-dim cp-prose', title: f.mtime }, fmtDateTime(f.mtime)) },
  { title: '大小', key: 'size', width: 100, sorter: 'default', render: (f: any) => h('span', { class: 'mono-dim' }, fmtBytes(f.size)) },
  {
    title: '状态', key: 'state', width: 130,
    render: (f: any) =>
      f.job_missing
        ? h(CpStatusTag, { status: 'deleted', text: '任务已删除' })
        : f.restoreable
          ? h(CpStatusTag, { status: 'success', text: '可一键恢复' })
          : h(CpStatusTag, { status: 'inactive', text: '仅可下载' }),
  },
  {
    title: '操作', key: 'ops', width: 150, align: 'center', fixed: 'right' as const,
    render: (f: any) =>
      h(CpRowActions, {
        buttons: [
          { label: '下载', run: () => download(f), disabled: !downloadHref(f) },
          { label: '恢复', run: () => openRestore(f), disabled: !isAdmin.value || !f.restoreable },
          { label: '删除', danger: true, run: () => confirmDelOne(f), disabled: !isAdmin.value },
        ],
      }),
  },
];

onMounted(() => {
  const q = route.query.job;
  if (typeof q === 'string' && /^\d+$/.test(q)) jobFilter.value = q;
  load();
  autoPoll.start();
});
</script>

<template>
  <div>
    <CpToolbarCard title="备份文件">
      <template #left>
        <NButton size="small" tertiary :icon="ico('ArrowBackOutline')" @click="router.push('/backups')">返回备份任务</NButton>
        <NButton v-if="isAdmin" size="small" tertiary :icon="ico('CloudUploadOutline')" @click="openUpload">上传产物</NButton>
        <NButton v-if="isAdmin && checked.length" size="small" tertiary type="error" :loading="deleting" :icon="ico('TrashOutline')" @click="confirmDelChecked">
          删除所选 {{ checked.length }} 个
        </NButton>
      </template>
      <template #right>
        <NSelect v-model:value="jobFilter" :options="jobOpts" size="small" style="width: 200px" placeholder="全部任务" aria-label="按备份任务筛选" />
        <NSelect v-model:value="kindFilter" :options="kindOpts" size="small" style="width: 200px" placeholder="全部类型" aria-label="按备份类型筛选" />
        <NInput v-model:value="pattern" size="small" clearable placeholder="搜索文件名…" style="width: 200px" :input-props="{ 'aria-label': '备份文件名' }">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NSelect v-model:value="autoSec" :options="AUTO_OPTS" size="small" style="width: 130px" aria-label="自动刷新间隔" />
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>

      <NAlert v-if="totals.count" type="info" :bordered="false" size="small" style="margin-bottom: var(--space-3)">
        共 {{ totals.count }} 个 · 合计 {{ fmtBytes(totals.size) }}；当前筛选 {{ shown.length }} 个 · {{ fmtBytes(totals.shownSize) }}。产物目录 {{ dir || '未设置' }}。
        <template v-if="isAdmin">
          保留份数由任务的 keep 按写入顺序自动裁剪最旧的产物；这里手动删除是腾空间的补充手段，删掉的文件不会由 timer 重建。
        </template>
      </NAlert>

      <NDataTable
        ref="tableAnchor"
        v-model:checked-row-keys="checked"
        :columns="columns"
        :data="shown"
        size="small"
        :bordered="false"
        :loading="loading"
        :scroll-x="1150"
        :max-height="tableH"
        :row-key="(f: any) => f.name"
      >
        <template #empty><EmptyBox :text="files.length ? '没有匹配当前筛选的备份文件' : '还没有任何备份产物，先到备份任务页立即备份一次'" /></template>
      </NDataTable>
    </CpToolbarCard>

    <NModal v-model:show="showRestore" preset="card" title="从该备份文件恢复" style="width: max(40vw, 420px); max-width: 94vw">
      <NSpace vertical :size="12">
        <NAlert type="warning" :bordered="false" size="small" role="alert">
          {{ RESTORE_WARN[restoreTarget?.kind] || '恢复会用备份内容覆盖当前数据，操作不可撤销。' }}
        </NAlert>
        <NForm label-placement="top" :show-feedback="false">
          <NFormItem label="文件"><NText class="mono cp-break" style="font-size: var(--fs-sm); min-width: 0">{{ restoreTarget?.name }}</NText></NFormItem>
          <NFormItem label="所属任务"><NText style="font-size: var(--fs-sm)">{{ restoreTarget ? jobLabel(restoreTarget) : '' }}</NText></NFormItem>
          <NFormItem label="生成时间 (UTC)"><NText style="font-size: var(--fs-sm)">{{ fmtDateTime(restoreTarget?.mtime) }} · {{ fmtBytes(restoreTarget?.size || 0) }}</NText></NFormItem>
        </NForm>
        <NAlert v-if="restoreErr" type="error" :bordered="false" size="small" role="alert">{{ restoreErr }}</NAlert>
        <!-- 结果色由 restoreWarn 决定；NAlert 自己会把 role 钉成 alert（$attrs 被 mergeProps
             覆盖），所以这里不再写 role，避免"以为自己是柔和提示"。 -->
        <NAlert v-if="restoreLog" :type="restoreWarn ? 'warning' : 'success'" :bordered="false" size="small">
          <pre class="mono-pre">{{ restoreLog }}</pre>
        </NAlert>
        <NSpace justify="end" :size="8">
          <NButton tertiary :disabled="restoring" @click="showRestore = false">关闭</NButton>
          <NPopconfirm @positive-click="doRestore">
            <template #trigger>
              <NButton type="warning" class="cp-press" :loading="restoring" :disabled="restoring" :icon="ico('RefreshCircleOutline')">恢复覆盖</NButton>
            </template>
            {{ RESTORE_ROLLBACK[restoreTarget?.kind] || '覆盖后当前数据不可找回，确认执行？' }}
          </NPopconfirm>
        </NSpace>
      </NSpace>
    </NModal>

    <NDrawer v-model:show="showUpload" :width="drawerW">
      <NDrawerContent title="上传备份产物" closable>
        <NForm label-placement="top">
          <NSpace vertical :size="12">
            <NFormItem label="目标任务">
              <NSpace vertical :size="2" style="width: 100%">
                <NSelect v-model:value="uploadJob" :options="uploadJobs" size="small" placeholder="选择备份任务" aria-label="上传到哪个备份任务" />
                <span class="cp-help">
                  把在别处生成的产物登记进某个任务名下：服务端会补 bk&lt;任务号&gt;- 前缀，恢复与按名清理才认得它。pg 要 .dump.gz、SQLite 要 .sqlite.gz、应用目录要 .tar.gz/.tgz（其它格式恢复逻辑读不了，传进来也只能干放着，所以服务端会直接拒绝）。
                </span>
              </NSpace>
            </NFormItem>
            <NFormItem label="文件">
              <NSpace :size="8" align="center" style="min-width: 0">
                <NButton size="small" tertiary :icon="ico('CloudUploadOutline')" @click="pickUploadFile">选择文件</NButton>
                <NText v-if="uploadFile" class="mono cp-break" style="font-size: var(--fs-xs); min-width: 0">
                  {{ uploadFile.name }} · {{ fmtBytes(uploadFile.size) }}
                </NText>
                <NText v-else depth="3" style="font-size: var(--fs-xs)">尚未选择</NText>
              </NSpace>
            </NFormItem>
            <NFormItem label="同名时">
              <NSpace vertical :size="4">
                <NSpace align="center" :size="6">
                  <NSwitch v-model:value="uploadOverwrite" size="small" aria-label="覆盖同名产物" />
                  <span class="cp-help">覆盖任务目录里的同名产物（原文件不可找回）</span>
                </NSpace>
              </NSpace>
            </NFormItem>
          </NSpace>
        </NForm>
        <template #footer>
          <NSpace justify="end" :size="12">
            <NButton size="small" :disabled="uploading" @click="showUpload = false">取消</NButton>
            <NButton size="small" type="primary" class="cp-press" :loading="uploading" :disabled="!uploadFile || !uploadJob" @click="doUpload">上传</NButton>
          </NSpace>
        </template>
      </NDrawerContent>
    </NDrawer>

    <ConfirmActionDialog ref="delDialog" />
  </div>
</template>

<style scoped>
.mono-pre { margin: 0; font-family: var(--font-mono); font-size: var(--fs-xs); white-space: pre-wrap; word-break: break-all; }
</style>
