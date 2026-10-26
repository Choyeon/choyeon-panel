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
const showPerm = ref(false);
const permTarget = ref<any>(null);
const permMode = ref('');
const permOwner = ref('');
const permGroup = ref('');
// 搬运剪贴板只活在页面里，存的是勾选那一刻的绝对路径：之后翻目录、点面包屑，
// 都不会把「剪切」的源换成新目录里的同名条目——那会搬错文件，而按钮看起来一模一样。
const clip = ref<{ mode: 'copy' | 'move'; items: string[] } | null>(null);
const clipCount = computed(() => clip.value?.items.length ?? 0);
const clipVerb = computed(() => (clip.value?.mode === 'move' ? '移动' : '复制'));

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
// 单文件的字节额度只有服务端那一份：config.CP_MAX_UPLOAD_MB，列表接口按 maxUploadBytes 回传，
// 在线打开（read_text）与保存（写盘）比的也是同一个常量。这里两处判断都读这一份。
// 以前 open() 里另写死 1MB：8MB 的额度被本地提前拒掉一半，一个 3MB 的日志/SQL 明明能开、
// 能存，却先收到一句「文件过大，请使用下载」——而且那两句话术还不是同一份，
// 用户看到两处限制会以为是两回事。
const capBytes = computed<number>(() => Number(data.value?.maxUploadBytes || 0));
const path = computed(() => String(data.value?.path ?? reqPath.value));
// 条目被服务层截断时「当前 N 项」必须是"目录里真有 N 项"，不是"表格里渲染了几行"；
// 上限也从服务层的回传里取，前端不抄一份，CP_MAX_DIR_ENTRIES 改完文案跟着改。
const truncated = computed(() => !!data.value?.truncated);
const dirTotal = computed(() => Number(data.value?.total ?? entries.value.length));
const entryCount = computed(() =>
  truncated.value ? `${entries.value.length}/${dirTotal.value}` : `${dirTotal.value}`
);
const quick = computed(() => roots.value);
/** 当前路径所属的白名单根：从快捷入口进第二个根之后，面包屑必须以它为起点，
 *  固定取 roots[0] 会让第一段「标签是 A、点下去跳到 B」，祖先段还会拼出白名单外的路径。 */
const curRoot = computed(() => {
  const p = path.value;
  const hit = roots.value.filter((r) => {
    const base = r.replace(/\/+$/, '') || '/';
    return p === base || p.startsWith(base === '/' ? '/' : base + '/');
  });
  return hit.sort((a, b) => b.length - a.length)[0] || roots.value[0] || '/';
});
/** 相对 curRoot 的路径段：第一段单列成根，其余才是要拼接的层级 */
const crumbs = computed(() => {
  const base = curRoot.value.replace(/\/+$/, '') || '/';
  const rest = base === '/' ? path.value : path.value.slice(base.length);
  return rest.split('/').filter(Boolean);
});
function crumbPath(i: number) {
  const base = curRoot.value.replace(/\/+$/, '') || '/';
  const seg = crumbs.value.slice(0, i + 1).join('/');
  return base === '/' ? `/${seg}` : `${base}/${seg}`;
}

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
let openSeq = 0;
async function open(e: any) {
  const p = join(e.name);
  if (e.isDir) return load(p);
  // 额度读服务端那一份（见 capBytes）；读不到额度时不在本地猜一个上限，
  // 让服务端的 read_text 自己答复——它拒的那句带的是它自己的数。
  if (capBytes.value && e.size > capBytes.value) {
    return msg.warning(`文件超过 ${fmtSize(capBytes.value)}，请使用下载`);
  }
  const seq = ++openSeq;
  try {
    const r = await api.fileContent(p);
    // 连开两个文件（或大文件慢响应）时，先发的慢响应后到会把编辑器整段换成前一个文件，
    // 用户接着点保存就写坏了当前这个文件。响应只认领最后那一次 open。
    if (seq !== openSeq) return;
    // 后端只在严格 UTF-8 时下发内容：二进制没有 content，直接引导到下载。
    // 以前这里会把图片/zip 解成 U+FFFD 塞进编辑器，点保存就把原文件写坏了。
    if (r.binary) return msg.warning('二进制文件，无法在线编辑，请使用下载');
    editPath.value = p;
    editContent.value = r.content;
    editing.value = true;
  } catch (err: any) {
    if (seq !== openSeq) return;
    msg.error(err.message);
  }
}
async function saveEdit() {
  if (savingEdit.value) return;
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
  if (busyOp.value) return;
  busyOp.value = 'delete';
  try {
    await api.fileAction('delete', join(e.name));
    msg.success('已删除');
    reload();
  } catch (err: any) {
    msg.error(err.message);
  } finally {
    busyOp.value = '';
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
  // 改名是真实文件系统操作，连点（尤其是输入框回车 + 确定各一次）会让第二次拿到
  // 已经不存在的源路径，报 "no such file"；弹窗于是既不关也说不清到底改成了什么。
  if (busyOp.value) return;
  busyOp.value = 'rename';
  try {
    await api.fileAction('rename', join(renameFrom.value.name), join(to));
    msg.success('已改名');
    showRename.value = false;
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busyOp.value = '';
  }
}
function openPerm(e: any) {
  permTarget.value = e;
  // 预填当前值：用户改哪一项自己会看出来，留空即"这一项不动"。
  permMode.value = e.octal || '';
  permOwner.value = e.owner || '';
  permGroup.value = e.group || '';
  showPerm.value = true;
}
async function confirmPerm() {
  const t = permTarget.value;
  // 与改名同一口径：确定按钮连点、或回车 + 点击各一次，第二次会拿同一份旧值再写一遍。
  if (!t || busyOp.value) return;
  const m = permMode.value.trim();
  const o = permOwner.value.trim();
  const g = permGroup.value.trim();
  const body: Record<string, string> = { path: join(t.name) };
  if (m && m !== (t.octal || '')) body.mode = m;
  if (o && o !== (t.owner || '')) body.owner = o;
  if (g && g !== (t.group || '')) body.group = g;
  // 一个字段都没变就别发：后端的"没有要修改的内容"是拦口的，不该变成弹窗的报错。
  if (Object.keys(body).length === 1) return (showPerm.value = false);
  busyOp.value = 'perm';
  try {
    const r = await postJson<any>('/files/perm', body);
    msg.success(`已改为 ${r.perm}（${r.octal}）${r.owner}:${r.group}`);
    showPerm.value = false;
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busyOp.value = '';
  }
}

async function mkdir() {
  const n = newFolder.value.trim();
  if (!n || busyOp.value) return;  busyOp.value = 'mkdir';
  try {
    await api.fileAction('mkdir', join(n));
    newFolder.value = '';
    msg.success('已创建目录');
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busyOp.value = '';
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
  // 上传是整段 POST，一次没落定就再来一次会把同名文件写到同一目录的两次临时文件上，
  // 后一次的成功提示会盖住前一次的失败。
  if (uploading.value) return;
  // 服务端会拒收超限文件，但那是在浏览器把整个文件传完之后：几百 MB 的误选
  // 白占一轮带宽和磁盘缓存，最后只换来一句"超过 8MB"。额度由列表接口带回来。
  const cap = capBytes.value;
  if (cap) {
    const over = files.filter((f) => f.size > cap).map((f) => f.name);
    if (over.length) {
      msg.warning(`超过 ${fmtSize(cap)} 上限，已跳过：${over.join('、')}`);
      files = files.filter((f) => f.size <= cap);
      if (!files.length) return;
    }
  }
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
  // 解压/打包都是分钟级的服务端写操作，且共用 busyOp 这把锁：和 del/rename/perm/mkdir/paste
  // 一样要重入守卫，否则删除在飞时点解压会让两个写操作并发，先回来的那个把 busyOp 清空、
  // 又放行第三个并发写。
  if (busyOp.value) return;
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
  if (busyOp.value) return;
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

function setClip(mode: 'copy' | 'move') {
  if (busyOp.value) return;
  const names = checkedKeys.value;
  if (!names.length) return;
  clip.value = { mode, items: names.map(join) };
  msg.info(`已${mode === 'move' ? '剪切' : '复制'} ${names.length} 项，进目标目录后点「粘贴到当前目录」`);
}
async function pasteClip() {
  const c = clip.value;
  if (!c || busyOp.value) return;
  // 目的地在动手前定死：粘贴途中点了面包屑，剩下几项跟着落进新目录，
  // 一次操作两个去处比整批留在原处难解释得多。
  const dest = path.value;
  busyOp.value = 'paste';
  const failed: string[] = [];
  let done = 0;
  try {
    // 逐项串行。同名冲突、搬进自己的子目录、跨分区、落盘余量都由后端判（唯一出处），
    // 页面只转述每一条的结果，不预先猜哪几项「应该能成」。
    for (const src of c.items) {
      try {
        await postJson('/files/action', { action: c.mode, path: src, path2: dest });
        done += 1;
      } catch (e: any) {
        failed.push(`${src.split('/').pop() || src}：${e.message}`);
      }
    }
  } finally {
    // 一次粘贴就清空：留着再点一次会把成功过的项再搬一遍（复制撞同名、移动报原路径不存在），
    // 没成的项在提示里已经点了名，重新勾选比让页面猜后端状态可靠。
    busyOp.value = '';
    clip.value = null;
  }
  const verb = c.mode === 'move' ? '移动' : '复制';
  if (failed.length) msg.warning(`已${verb} ${done} 项到 ${dest}，${failed.length} 项没成：${failed.join('；')}`);
  else msg.success(`已${verb} ${done} 项到 ${dest}`);
  reload();
}

const columns: any[] = [
  // 勾选在 naive-ui 里是一列（type: 'selection'），不是表格属性：
  // 原来绑的 :row-selection 这个 prop 在 NDataTable 上根本不存在，会被整个忽略，
  // 于是「打包选中」从上线起就只有按钮、没有可勾的框（375px 与 1440px 实测 0 个 .n-checkbox）。
  { type: 'selection' },
  {
    // 弹性列必须给 minWidth：scroll-x auto 下没有它，固定列一超过容器就被挤成 0 宽（375px 实测名字看不见）
    title: '名称', key: 'name', minWidth: 220,
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
    // 权限与属主共用一列两行，而不是拆成两列：这一列固定占 130px，拆两列要 210px 起，
    // 375 档名称列（minWidth 220）就只剩横滚。两行都省略号，全文在 title 里。
    title: '权限/属主', key: 'perm', width: 130,
    render: (e: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'mono-dim', title: e.perm ? `${e.perm}（${e.octal}）` : '读不到' }, e.perm || '—'),
      h('span', { class: 'cell-sub', title: e.owner ? `${e.owner}:${e.group}` : '读不到' }, e.owner ? `${e.owner}:${e.group}` : '—'),
    ]),
  },
  {
    title: '操作', key: 'ops', width: 270, align: 'center', fixed: 'right' as const,
    // 下载必须是 <a>（走浏览器下载管理器）、解压/删除要 Popconfirm，所以套不上
    // CpRowActions 组件，但 .cp-ops + text/small 就是它那一套版式。
    render: (e: any) => {
      const txt = { text: true, size: 'small', class: 'cp-op' } as const;
      return h('div', { class: 'cp-ops' }, [
        h(NButton, { ...txt, type: 'primary', tag: 'a', href: `${apiUrl('/files/download')}?path=${encodeURIComponent(join(e.name))}&token=${encodeURIComponent(getToken())}`, target: '_blank', rel: 'noopener' }, () => '下载'),
        ...(e.isDir ? [] : [h(NButton, { ...txt, type: 'primary', onClick: () => open(e) }, () => '编辑')]),
        ...(e.isDir || !ARCHIVE_RE.test(String(e.name)) ? [] : [
          h(NPopconfirm, { onPositiveClick: () => extract(e) }, {
            trigger: () => h(NButton, { ...txt, type: 'primary', loading: busyOp.value === 'extract', disabled: !!busyOp.value, title: `解压 ${e.name}` }, { default: () => '解压' }),
            default: () => `把 ${e.name} 解压到当前目录？同名文件会被覆盖。`,
          }),
        ]),
        h(NButton, { ...txt, type: 'primary', onClick: () => openRename(e) }, () => '改名'),
        h(NButton, { ...txt, type: 'primary', title: `修改 ${e.name} 的权限与属主`, onClick: () => openPerm(e) }, { default: () => '权限' }),
        h(NPopconfirm, { onPositiveClick: () => del(e) }, {
          trigger: () => h(NButton, { ...txt, type: 'error', title: `删除 ${e.name}` }, { default: () => '删除' }),
          default: () => `删除 ${e.name}${e.isDir ? '（含全部子文件）' : ''}？`,
        }),
      ]);
    },
  },
];

/** 受控写法：naive 回传的是 RowKey[]（string|number），本表 row-key 取自条目名，统一收成 string。 */
function onChecked(keys: Array<string | number>) {
  checkedKeys.value = keys.map(String);
}

// 快捷入口与"可访问范围"都来自服务端白名单：前端写死一份的话，
// 改过 CP_FILE_ROOTS 的部署点一下就报"路径超出允许范围"。
</script>

<template>
  <div>
    <PageHeader title="文件管理" :sub="path">
      <template #actions>
        <NButton size="small" tertiary :icon="icon('RefreshOutline', 14)" @click="load()">刷新</NButton>
        <NButton
          size="small" tertiary :disabled="!!busyOp || !checkedKeys.length" :loading="busyOp === 'archive'"
          :icon="icon('CubeOutline', 14)" @click="pack"
        >
          打包选中{{ checkedKeys.length ? `（${checkedKeys.length}）` : '' }}
        </NButton>
        <NButton
          size="small" tertiary :disabled="!checkedKeys.length || !!busyOp"
          :icon="icon('CopyOutline', 14)" @click="setClip('copy')"
        >
          复制选中{{ checkedKeys.length ? `（${checkedKeys.length}）` : '' }}
        </NButton>
        <NButton
          size="small" tertiary :disabled="!checkedKeys.length || !!busyOp"
          :icon="icon('CutOutline', 14)" @click="setClip('move')"
        >
          剪切选中{{ checkedKeys.length ? `（${checkedKeys.length}）` : '' }}
        </NButton>
        <NButton
          v-if="clip"
          size="small" type="primary" :loading="busyOp === 'paste'" :disabled="!!busyOp"
          :title="`把 ${clipCount} 项${clipVerb}到 ${path}`"
          :icon="icon('ClipboardOutline', 14)" @click="pasteClip"
        >
          粘贴到当前目录（{{ clipCount }}）
        </NButton>
        <NButton size="small" type="primary" :loading="uploading" :disabled="uploading" :icon="icon('CloudUploadOutline', 14)" @click="pickFiles">
          {{ uploadHint || '上传文件' }}
        </NButton>
      </template>
    </PageHeader>

    <NCard size="small" style="margin-bottom: var(--space-4)" :content-style="{ padding: 'var(--space-3) var(--space-4)' }">
      <NSpace vertical :size="12">
        <NSpace align="center" justify="space-between" style="width: 100%">
          <NBreadcrumb>
            <NBreadcrumbItem>
              <NButton text type="primary" size="tiny" @click="load(curRoot)">{{ curRoot }}</NButton>
            </NBreadcrumbItem>
            <NBreadcrumbItem v-for="(c, i) in crumbs" :key="i">
              <NButton text size="tiny" style="color: var(--cp-text-dim)" @click="load(crumbPath(i))">{{ c }}</NButton>
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
          <NButton size="small" :loading="busyOp === 'mkdir'" :disabled="!!busyOp" :icon="icon('AddOutline', 14)" @click="mkdir">新建目录</NButton>
        </NSpace>
      </NSpace>
    </NCard>

    <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-3)">
      可访问范围：{{ quick.join('、') || '（未配置 CP_FILE_ROOTS）' }}。点击文件名直接在线编辑；勾选多项可打包成 tar.gz，也可复制/剪切后到别的目录粘贴（剪贴板只在本页有效）。
      当前 {{ entryCount }} 项{{ truncated ? `（条目过多，按名称只列出前 ${data?.maxEntries ?? entries.length} 项，进子目录缩小范围）` : '' }}。
    </NText>

    <NDataTable
      ref="tableAnchor"
      :columns="columns" :data="entries" size="small" :bordered="false" :loading="listing"
      :checked-row-keys="checkedKeys" @update:checked-row-keys="onChecked"
      :scroll-x="'auto'" :max-height="tableH" :min-row-height="40" virtual-scroll :pagination="false"
      :row-key="(e: any) => e.name"
    >
      <!-- 拉取失败不能显示成「空目录」：那是"这个目录没有条目"，而实际是"没读到"，
           两者在运维上是完全不同的结论（前者正常，后者要去看权限/服务）。 -->
      <template #empty><EmptyBox :text="error ? '目录读取失败' : '空目录'" /></template>
    </NDataTable>

    <NModal v-model:show="editing" preset="card" style="width: 900px; max-width: 94vw" :title="`编辑 ${editPath.split('/').pop()}`">
      <template #header-extra><NText depth="3" style="font-size: var(--fs-xs)">{{ editPath }}</NText></template>
      <NSpace vertical :size="12" style="width: 100%">
        <NInput v-model:value="editContent" type="textarea" :autosize="{ minRows: 16, maxRows: 28 }" :input-props="{ class: 'mono', 'aria-label': '文件内容', style: 'font-size: var(--fs-sm)' }" />
        <NSpace justify="end">
          <NButton tertiary @click="editing = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="savingEdit" :disabled="savingEdit" :icon="icon('SaveOutline', 14)" @click="saveEdit">保存</NButton>
        </NSpace>
      </NSpace>
    </NModal>

    <NModal v-model:show="showRename" preset="card" title="重命名" style="width: 400px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NInput v-model:value="renameTo" placeholder="新名称" :input-props="{ 'aria-label': '新文件名称' }" @keyup.enter="confirmRename" />
        <NSpace justify="end">
          <NButton tertiary @click="showRename = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="busyOp === 'rename'" :disabled="!!busyOp" :icon="icon('SaveOutline', 14)" @click="confirmRename">确定</NButton>
        </NSpace>
      </NSpace>
    </NModal>

    <NModal v-model:show="showPerm" preset="card" title="权限与属主" style="width: 420px; max-width: 94vw">
      <template #header-extra>
        <NText depth="3" style="font-size: var(--fs-xs)">{{ permTarget?.name }}</NText>
      </template>
      <NSpace vertical :size="12">
        <NText depth="3" style="font-size: var(--fs-xs); line-height: 1.6">
          当前 {{ permTarget?.perm || '—' }}（{{ permTarget?.octal || '—' }}）· {{ permTarget?.owner || '—' }}:{{ permTarget?.group || '—' }}。
          只改这一个条目，不递归到子文件；留空的那一项不动。
        </NText>
        <NInput
          v-model:value="permMode" placeholder="八进制权限，如 644"
          :input-props="{ 'aria-label': '八进制权限', class: 'mono' }" @keyup.enter="confirmPerm"
        >
          <template #prefix><NIcon :component="icons.KeyOutline" /></template>
        </NInput>
        <div style="display: flex; gap: var(--space-2); width: 100%">
          <NInput
            v-model:value="permOwner" placeholder="所有者，如 www-data"
            style="flex: 1; min-width: 0" :input-props="{ 'aria-label': '所有者' }" @keyup.enter="confirmPerm"
          >
            <template #prefix><NIcon :component="icons.PersonCircleOutline" /></template>
          </NInput>
          <NInput
            v-model:value="permGroup" placeholder="属组，如 www-data"
            style="flex: 1; min-width: 0" :input-props="{ 'aria-label': '属组' }" @keyup.enter="confirmPerm"
          >
            <template #prefix><NIcon :component="icons.PeopleOutline" /></template>
          </NInput>
        </div>
        <NSpace justify="end">
          <NButton tertiary @click="showPerm = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="busyOp === 'perm'" :disabled="!!busyOp" :icon="icon('SaveOutline', 14)" @click="confirmPerm">保存</NButton>
        </NSpace>
      </NSpace>
    </NModal>
  </div>
</template>
