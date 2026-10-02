<script setup lang="ts">
import { onMounted, ref, h } from 'vue';
import {
  NDataTable, NButton, NSpace, NInput, NText, NBreadcrumb, NBreadcrumbItem, NModal,
  NPopconfirm, useMessage, NIcon, NCard,
} from 'naive-ui';
import { api, getToken } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const path = ref('/root/www');
const entries = ref<any[]>([]);
const editing = ref(false);
const editPath = ref('');
const editContent = ref('');
const savingEdit = ref(false);
const newFolder = ref('');
const uploading = ref(false);
const showRename = ref(false);
const renameFrom = ref<any>(null);
const renameTo = ref('');

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

function fmt(n: number, isDir: boolean) {
  if (isDir) return '—';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < 2) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

const crumbs = ref<string[]>([]);
function splitPath(p: string) {
  return p.split('/').filter(Boolean);
}

async function load(p = path.value) {
  try {
    const r = await api.files(p);
    path.value = r.path;
    entries.value = r.entries;
    crumbs.value = splitPath(r.path);
  } catch (e: any) {
    msg.error(e.message);
  }
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
    load();
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
    load();
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
    load();
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
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function upload(file: File) {
  uploading.value = true;
  try {
    const buf = await file.arrayBuffer();
    await fetch(`/api/files/content?path=${encodeURIComponent(join(file.name))}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/octet-stream', Authorization: `Bearer ${getToken()}` },
      body: buf,
    }).then(async (r) => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || '上传失败');
    });
    msg.success('上传完成');
    load();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    uploading.value = false;
  }
}
function pickFile() {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.onchange = () => inp.files?.[0] && upload(inp.files[0]);
  inp.click();
}

const columns: any[] = [
  {
    title: '名称', key: 'name',
    render: (e: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: e.isDir ? icons.FolderOutline : icons.DocumentOutline, size: 17, color: e.isDir ? '#f5c26f' : '#8b8b96' }),
        ]),
        h(NButton, {
          text: true, type: e.isDir ? 'info' : 'default', class: 'cell-name',
          style: e.isDir ? '' : 'color:#cfcfd8', onClick: () => open(e),
        }, () => e.name),
      ]),
  },
  { title: '大小', key: 'size', width: 90, render: (e: any) => h('span', { class: 'mono-dim' }, fmt(e.size, e.isDir)) },
  { title: '修改时间', key: 'mtime', width: 170, render: (e: any) => h('span', { class: 'mono-dim' }, e.mtime?.replace('T', ' ').slice(0, 19) || '—') },
  {
    title: '操作', key: 'ops', width: 210,
    render: (e: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, tag: 'a', href: `/api/files/download?path=${encodeURIComponent(join(e.name))}&token=${getToken()}`, target: '_blank', icon: ico('DownloadOutline', 12) }, () => '下载'),
        ...(e.isDir ? [] : [h(NButton, { size: 'tiny', tertiary: true, icon: ico('PencilOutline', 12), onClick: () => open(e) }, () => '编辑')]),
        h(NButton, { size: 'tiny', tertiary: true, icon: ico('OptionsOutline', 12), onClick: () => openRename(e) }, () => '改名'),
        h(NPopconfirm, { onPositiveClick: () => del(e) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline', 12) }, { default: () => '' }),
          default: () => `删除 ${e.name}${e.isDir ? '（含全部子文件）' : ''}？`,
        }),
      ]),
  },
];

const quick = ['/root/www', '/etc/nginx', '/root/backups/panel'];
onMounted(() => load());
</script>

<template>
  <div>
    <PageHeader title="文件管理" :sub="path">
      <template #actions>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load()">刷新</NButton>
        <NButton size="small" type="primary" :loading="uploading" :icon="ico('CloudUploadOutline')" @click="pickFile">上传文件</NButton>
      </template>
    </PageHeader>

    <NCard size="small" style="margin-bottom: 14px" :content-style="{ padding: '12px 16px' }">
      <NSpace vertical :size="10">
        <NSpace align="center" justify="space-between" style="width: 100%">
          <NBreadcrumb>
            <NBreadcrumbItem>
              <NButton text type="primary" size="tiny" @click="load('/root/www')">{{ crumbs[0] || 'root' }}</NButton>
            </NBreadcrumbItem>
            <NBreadcrumbItem v-for="(c, i) in crumbs.slice(1)" :key="i">
              <NButton text size="tiny" style="color: #9d9da8" @click="load('/' + crumbs.slice(0, i + 1).join('/'))">{{ c }}</NButton>
            </NBreadcrumbItem>
          </NBreadcrumb>
          <NSpace :size="6">
            <NButton v-for="q in quick" :key="q" size="tiny" :tertiary="path === q" @click="load(q)">{{ q }}</NButton>
          </NSpace>
        </NSpace>
        <NSpace :size="8">
          <NInput v-model:value="newFolder" placeholder="新文件夹名称" style="width: 220px" size="small" @keyup.enter="mkdir">
            <template #prefix><NIcon :component="icons.FolderOutline" /></template>
          </NInput>
          <NButton size="small" :icon="ico('AddOutline')" @click="mkdir">新建目录</NButton>
        </NSpace>
      </NSpace>
    </NCard>

    <NText depth="3" style="font-size: 12px; display: block; margin-bottom: 10px">可访问范围：/root/www（项目）、/etc/nginx（配置）、/root/backups/panel（备份）。点击文件名直接在线编辑。</NText>

    <NDataTable :columns="columns" :data="entries" size="small" :bordered="false" :max-height="'calc(100vh - 360px)'" :row-key="(e: any) => e.name">
      <template #empty><EmptyBox text="空目录" /></template>
    </NDataTable>

    <NModal v-model:show="editing" preset="card" style="width: 900px; max-width: 94vw" :title="`编辑 ${editPath.split('/').pop()}`">
      <template #header-extra><NText depth="3" style="font-size: 12px">{{ editPath }}</NText></template>
      <NSpace vertical :size="12" style="width: 100%">
        <NInput v-model:value="editContent" type="textarea" :autosize="{ minRows: 16, maxRows: 28 }" style="font-family: 'JetBrains Mono', monospace; font-size: 13px" />
        <NSpace justify="end">
          <NButton tertiary @click="editing = false">取消</NButton>
          <NButton type="primary" :loading="savingEdit" :icon="ico('SaveOutline')" @click="saveEdit">保存</NButton>
        </NSpace>
      </NSpace>
    </NModal>

    <NModal v-model:show="showRename" preset="card" title="重命名" style="width: 380px">
      <NSpace vertical :size="12">
        <NInput v-model:value="renameTo" placeholder="新名称" @keyup.enter="confirmRename" />
        <NSpace justify="end">
          <NButton tertiary @click="showRename = false">取消</NButton>
          <NButton type="primary" :icon="ico('SaveOutline')" @click="confirmRename">确定</NButton>
        </NSpace>
      </NSpace>
    </NModal>
  </div>
</template>
