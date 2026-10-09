<script setup lang="ts">
import { computed, ref, watch, h } from 'vue';
import {
  NDataTable, NSpace, NInput, NText, NBreadcrumb, NBreadcrumbItem, NModal,
  NPopconfirm, useMessage, useDialog, NIcon, NCard,
} from 'naive-ui';
import { NButton } from '../ui';
import { api, getToken, apiUrl } from '../api';
import { icon, icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { useCachedFetch, invalidateCache } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';
import { fmtDateTime, fmtSize } from '../composables/useFormat';
import { beginRequest, endRequest } from '../composables/useAppProgress';

const msg = useMessage();
const dlg = useDialog();
// path 初始为空：起始目录由服务端按 CP_FILE_ROOTS 决定。
// 前端写死 /root/www 时，改过白名单的部署一进这一页就请求一个不在范围内的目录。
const reqPath = ref('');
const editing = ref(false);
const editPath = ref('');
const editContent = ref('');
const savingEdit = ref(false);
const newFolder = ref('');
const uploading = ref(false);
const uploadHint = ref('');
const busyOp = ref('');
const checkedKeys = ref<string[]>([]);
const showRename = ref(false);
const renameFrom = ref<any>(null);
const renameTo = ref('');

// 每个目录一条缓存：来回浏览已看过的目录立刻出列表，超过 15s 才后台再验证。
// node_modules 这类目录动辄几千项，缓存条数由 LRU 上限兜住，不会一直涨。
const { data, error, loading: listing, refresh } = useCachedFetch<any>({
  key: () => `files:${reqPath.value}`,
  loader: () => api.files(reqPath.value),
  maxAge: 15000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });

const entries = computed<any[]>(() => data.value?.entries ?? []);
const roots = computed<string[]>(() => data.value?.roots ?? []);
const path = computed(() => String(data.value?.path ?? reqPath.value));
const quick = computed(() => roots.value);
const homeRoot = computed(() => roots.value[0] || '/');

function splitPath(p: string) {
  return p.split('/').filter(Boolean);
}
const crumbs = computed(() => splitPath(path.value));

const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

function load(p = path.value) {
  reqPath.value = p;
  // 已经在飞的同一个目录复用那次请求（useCachedFetch 内部去重），这里只需要兜住错误提示
  return refresh().catch(() => undefined);
}
/** 写操作之后：本目录的缓存作废，强制回源 */
function reload() {
  invalidateCache(`files:${reqPath.value}`);
  // 删除/改名/新建/上传都会写审计；父目录的其它缓存也一并作废
  invalidateCache('files:');
  invalidateCache('audit:');
  checkedKeys.value = [];
  return refresh().catch(() => undefined);
}

function join(name: string) {
  return path.value === '/' ? `/${name}` : `${path.value}/${name}`;
}
async function open(e: any) {
  const p = join(e.name);
  if (e.isDir) return load(p);
  if (e.size > 1024 * 1024) return msg.warning('文件过大，请使用下载');
  try {
    const r = await api.fileContent(p);
    editPath.value = p;
    editContent.value = r.content;
    editing.value = true;
  } catch (err: any) {
    msg.error(err.message);
  }
}
async function saveEdit() {
  savingEdit.value = true;
  try {
    await api.saveFile(editPath.value, editContent.value);
    msg.success('已保存');
    editing.value = false;
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    savingEdit.value = false;
  }
}
async function del(e: any) {
  try {
    await api.fileAction('delete', join(e.name));
    msg.success('已删除');
    reload();
  } catch (err: any) {
    msg.error(err.message);
  }
}
function openRename(e: any) {
  renameFrom.value = e;
  renameTo.value = e.name;
  showRename.value = true;
}
async function confirmRename() {
  const to = renameTo.value.trim();
  if (!to || !renameFrom.value || to === renameFrom.value.name) return (showRename.value = false);
  try {
    await api.fileAction('rename', join(renameFrom.value.name), join(to));
    msg.success('已改名');
    showRename.value = false;
    reload();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function mkdir() {
  const n = newFolder.value.trim();
  if (!n) return;
  try {
    await api.fileAction('mkdir', join(n));
    newFolder.value = '';
    msg.success('已创建目录');
    reload();
  } catch (e: any) {
    msg.error(e.message);
  }
}
/**
 * multipart/JSON 直发：上传必须让浏览器自己带 FormData 的 boundary，
 * 而 api.ts 的 req() 固定塞 JSON Content-Type、saveFile() 固定 octet-stream，两者都套不上。
 * 包在页面里而不是往 api.ts 加方法，跟 Sites.vue 是同一个取舍：api.ts 是多条改动流
 * 共同的落点，页面自用的一层不往外放。
 */
async function postJson<T = any>(path: string, body: unknown, timeout = 120000): Promise<T> {
  return send<T>(path, { body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } }, timeout);
}
async function postForm<T = any>(path: string, form: FormData, timeout = 300000): Promise<T> {
  return send<T>(path, { body: form }, timeout);
}
async function send<T>(path: string, init: RequestInit, timeout: number): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  beginRequest();
  let ok = true;
  try {
    const res = await fetch(apiUrl(path), {
      method: 'POST',
      ...init,
      headers: { Authorization: `Bearer ${getToken()}`, ...(init.headers || {}) },
      signal: ctrl.signal,
    });
    const data = await res.json().catch(() => ({}) as any);
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data as T;
  } catch (e: any) {
    ok = false;
    if (e?.name === 'AbortError') throw new Error('请求超时，请稍后重试');
    throw e;
  } finally {
    clearTimeout(timer);
    endRequest(ok);
  }
}

async function uploadFiles(files: File[], overwrite = false) {
  if (!files.length) return;
  const dup = files.filter((f) => entries.value.some((e) => !e.isDir && e.name === f.name));
  if (!overwrite && dup.length) {
    // 覆盖是不可逆的，先给出名单再动手；后端在这种情况下会逐个拒绝，不会写一半。
    dlg.warning({
      title: '当前目录已有同名文件',
      content: `${dup.map((d) => d.name).join('、')} 已存在。继续会覆盖它们（原内容不可恢复）。`,
      positiveText: '覆盖上传',
      negativeText: '取消',
      onPositiveClick: () => uploadFiles(files, true),
    });
    return;
  }
  const form = new FormData();
  form.append('path', path.value);
  if (overwrite) form.append('overwrite', '1');
  for (const f of files) form.append('files', f, f.name);
  uploading.value = true;
  uploadHint.value = `上传 ${files.length} 个文件`;
  try {
    const r = await postForm<{ saved: any[]; failed: string[]; bytes: number }>('/files/upload', form);
    if (r.failed?.length) msg.warning(`已保存 ${r.saved.length} 个，${r.failed.length} 个失败：${r.failed.join('、')}`);
    else msg.success(`已上传 ${r.saved.length} 个文件（${fmtSize(r.bytes)}）`);
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    uploading.value = false;
    uploadHint.value = '';
  }
}
function pickFiles() {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.multiple = true;
  inp.onchange = () => uploadFiles(Array.from(inp.files || []));
  inp.click();
}

const ARCHIVE_RE = /\.(zip|tar|tar\.gz|tgz|tar\.bz2|tbz2|tar\.xz|txz)$/i;
async function extract(e: any) {
  busyOp.value = 'extract';
  try {
    const r = await postJson<{ files: number; dest: string }>('/files/extract', { path: join(e.name) });
    msg.success(`已解压 ${r.files} 项到 ${r.dest}`);
    reload();
  } catch (err: any) {
    msg.error(err.message);
  } finally {
    busyOp.value = '';
  }
}
async function pack() {
  const names = checkedKeys.value;
  if (!names.length) return;
  busyOp.value = 'archive';
  try {
    const r = await postJson<{ path: string; size: number }>('/files/archive', {
      paths: names.map(join),
      name: `archive-${new Date().toISOString().slice(0, 10)}.tar.gz`,
    });
    checkedKeys.value = [];
    msg.success(`已打包 ${names.length} 项（${fmtSize(r.size)}），开始下载`);
    reload();
    const a = document.createElement('a');
    a.href = `${apiUrl('/files/download')}?path=${encodeURIComponent(r.path)}&token=${encodeURIComponent(getToken())}`;
    a.rel = 'noopener';
    a.click();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busyOp.value = '';
  }
}

const columns: any[] = [
  {
    title: '名称', key: 'name',
    render: (e: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: e.isDir ? icons.FolderOutline : icons.DocumentOutline, size: 17, color: e.isDir ? 'var(--cp-warn)' : 'var(--cp-text-mute)' }),
        ]),
        h(NButton, {
          text: true, type: e.isDir ? 'info' : 'default', class: 'cell-name',
          // 文件名会被 ellipsis 裁掉（窄列/长名），title 保留全文
          title: e.name,
          style: e.isDir ? '' : 'color:var(--cp-text)', onClick: () => open(e),
          // 目录不只靠颜色区分：可访问名本身就是入口，aria 里补一句类型
          'aria-label': e.isDir ? `打开目录 ${e.name}` : `编辑文件 ${e.name}`,
        }, () => e.name),
      ]),
  },
  { title: '大小', key: 'size', width: 90, render: (e: any) => h('span', { class: 'mono-dim' }, fmtSize(e.size, e.isDir)) },
  { title: '修改时间 (UTC)', key: 'mtime', width: 170, render: (e: any) => h('span', { class: 'mono-dim' }, fmtDateTime(e.mtime)) },
  {
    title: '操作', key: 'ops', width: 330,
    render: (e: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, tag: 'a', href: `${apiUrl('/files/download')}?path=${encodeURIComponent(join(e.name))}&token=${encodeURIComponent(getToken())}`, target: '_blank', rel: 'noopener', icon: icon('DownloadOutline', 12) }, () => '下载'),
        ...(e.isDir ? [] : [h(NButton, { size: 'tiny', tertiary: true, icon: icon('PencilOutline', 12), onClick: () => open(e) }, () => '编辑')]),
        ...(e.isDir || !ARCHIVE_RE.test(String(e.name)) ? [] : [
          h(NPopconfirm, { onPositiveClick: () => extract(e) }, {
            trigger: () => h(NButton, { size: 'tiny', tertiary: true, loading: busyOp.value === 'extract', icon: icon('FolderOpenOutline', 12), title: `解压 ${e.name}` }, { default: () => '解压' }),
            default: () => `把 ${e.name} 解压到当前目录？同名文件会被覆盖。`,
          }),
        ]),
        h(NButton, { size: 'tiny', tertiary: true, icon: icon('OptionsOutline', 12), onClick: () => openRename(e) }, () => '改名'),
        h(NPopconfirm, { onPositiveClick: () => del(e) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: icon('TrashOutline', 12), title: `删除 ${e.name}` }, { default: () => '删除' }),
          default: () => `删除 ${e.name}${e.isDir ? '（含全部子文件）' : ''}？`,
        }),
      ]),
  },
];

const rowSelection = computed(() => ({
  type: 'checkbox' as const,
  checkedRowKeys: checkedKeys.value,
  onUpdateCheckedRowKeys: (keys: Array<string | number>) => { checkedKeys.value = keys.map(String); },
}));

// 快捷入口与"可访问范围"都来自服务端白名单：前端写死一份的话，
// 改过 CP_FILE_ROOTS 的部署点一下就报"路径超出允许范围"。
</script>

<template>
  <div>
    <PageHeader title="文件管理" :sub="path">
      <template #actions>
        <NButton size="small" tertiary :icon="icon('RefreshOutline', 14)" @click="load()">刷新</NButton>
        <NButton
          size="small" tertiary :disabled="!checkedKeys.length" :loading="busyOp === 'archive'"
          :icon="icon('CubeOutline', 14)" @click="pack"
        >
          打包选中{{ checkedKeys.length ? `（${checkedKeys.length}）` : '' }}
        </NButton>
        <NButton size="small" type="primary" :loading="uploading" :icon="icon('CloudUploadOutline', 14)" @click="pickFiles">
          {{ uploadHint || '上传文件' }}
        </NButton>
      </template>
    </PageHeader>

    <NCard size="small" style="margin-bottom: var(--space-4)" :content-style="{ padding: 'var(--space-3) var(--space-4)' }">
      <NSpace vertical :size="12">
        <NSpace align="center" justify="space-between" style="width: 100%">
          <NBreadcrumb>
            <NBreadcrumbItem>
              <NButton text type="primary" size="tiny" @click="load(homeRoot)">{{ crumbs[0] || 'root' }}</NButton>
            </NBreadcrumbItem>
            <NBreadcrumbItem v-for="(c, i) in crumbs.slice(1)" :key="i">
              <NButton text size="tiny" style="color: var(--cp-text-dim)" @click="load('/' + crumbs.slice(0, i + 1).join('/'))">{{ c }}</NButton>
            </NBreadcrumbItem>
          </NBreadcrumb>
          <NSpace :size="6">
            <NButton v-for="q in quick" :key="q" size="tiny" :tertiary="path === q" @click="load(q)">{{ q }}</NButton>
          </NSpace>
        </NSpace>
        <NSpace :size="8">
          <NInput v-model:value="newFolder" placeholder="新文件夹名称" style="width: 220px; max-width: 100%" size="small" @keyup.enter="mkdir">
            <template #prefix><NIcon :component="icons.FolderOutline" /></template>
          </NInput>
          <NButton size="small" :icon="icon('AddOutline', 14)" @click="mkdir">新建目录</NButton>
        </NSpace>
      </NSpace>
    </NCard>

    <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-3)">
      可访问范围：{{ quick.join('、') || '（未配置 CP_FILE_ROOTS）' }}。点击文件名直接在线编辑；勾选多项可打包成 tar.gz。
      当前 {{ entries.length }} 项{{ entries.length > 200 ? '（虚拟滚动，只渲染可见行）' : '' }}。
    </NText>

    <NDataTable
      ref="tableAnchor"
      :columns="columns" :data="entries" size="small" :bordered="false" :loading="listing"
      :row-selection="rowSelection"
      :scroll-x="900" :max-height="tableH" :min-row-height="40" virtual-scroll :pagination="false"
      :row-key="(e: any) => e.name"
    >
      <template #empty><EmptyBox text="空目录" /></template>
    </NDataTable>

    <NModal v-model:show="editing" preset="card" style="width: 900px; max-width: 94vw" :title="`编辑 ${editPath.split('/').pop()}`">
      <template #header-extra><NText depth="3" style="font-size: var(--fs-xs)">{{ editPath }}</NText></template>
      <NSpace vertical :size="12" style="width: 100%">
        <NInput v-model:value="editContent" type="textarea" :autosize="{ minRows: 16, maxRows: 28 }" :input-props="{ class: 'mono', 'aria-label': '文件内容', style: 'font-size: var(--fs-sm)' }" />
        <NSpace justify="end">
          <NButton tertiary @click="editing = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="savingEdit" :icon="icon('SaveOutline', 14)" @click="saveEdit">保存</NButton>
        </NSpace>
      </NSpace>
    </NModal>

    <NModal v-model:show="showRename" preset="card" title="重命名" style="width: 400px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NInput v-model:value="renameTo" placeholder="新名称" :input-props="{ 'aria-label': '新文件名称' }" @keyup.enter="confirmRename" />
        <NSpace justify="end">
          <NButton tertiary @click="showRename = false">取消</NButton>
          <NButton type="primary" class="cp-press" :icon="icon('SaveOutline', 14)" @click="confirmRename">确定</NButton>
        </NSpace>
      </NSpace>
    </NModal>
  </div>
</template>
