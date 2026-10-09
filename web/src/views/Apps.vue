<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, h, computed } from 'vue';
import { useRouter } from 'vue-router';
import {
  NButton, NSpace, NDataTable, NModal, NForm, NFormItem, NInput, NInputNumber, NSelect,
  NRadioGroup, NRadioButton, NPopconfirm, NIcon, NAlert, NTag, NCollapse, NCollapseItem,
  useMessage,
} from 'naive-ui';
import { api, repoHref, siteHref } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const router = useRouter();
const msg = useMessage();
const rows = ref<any[]>([]);
const kw = ref('');
const showCreate = ref(false);
const creating = ref(false);
const form = ref<any>(singleFields());
const mode = ref<'single' | 'split'>('single');
const be = ref<any>(singleFields({ start_cmd: '', port: 8000 }));
const fe = ref<any>(singleFields({ type: 'node', start_cmd: 'npm run start', port: 3100 }));

function singleFields(over: any = {}) {
  return {
    name: '', type: 'node', repo_url: '', branch: 'main', domain: '', port: null,
    install_cmd: '', start_cmd: 'npm start', unit_override: '', path: '', unit_template: '', template: '',
    ...over,
  };
}

// 前后端分离：共享名称/仓库/目录，组件条目派生 `<name>-backend` / `<name>-frontend`
function applyShared() {
  const sh = {
    name: form.value.name,
    repo_url: form.value.repo_url,
    branch: form.value.branch || 'main',
    path: form.value.path,
  };
  be.value = { ...be.value, ...sh, type: form.value.type, template: form.value.template || undefined };
  fe.value = { ...fe.value, ...sh };
}

function buildApp(f: any, role: string): any {
  return {
    name: f.name ? `${f.name}-${role}` : '',
    type: f.type,
    repo_url: f.repo_url,
    branch: f.branch || 'main',
    domain: role === 'frontend' ? (f.domain || null) : null,
    port: f.port || null,
    install_cmd: f.install_cmd || null,
    start_cmd: f.start_cmd,
    path: f.path ? `${f.path.replace(/\/+$/, '')}/${role}` : null,
    component_role: role,
  };
}

const runningCount = computed(() => rows.value.filter((r) => r.running).length);
// 组合项目合并展示：子组件从主列表移除，改在父行的名称单元里缩进渲染
const childMap = computed(() => {
  const m = new Map<number, any[]>();
  for (const r of rows.value) {
    if (r.parent_id) {
      const list = m.get(r.parent_id) || [];
      list.push(r);
      m.set(r.parent_id, list);
    }
  }
  for (const [, list] of m) {
    list.sort((a, b) => (a.component_role === 'backend' ? -1 : b.component_role === 'backend' ? 1 : a.name.localeCompare(b.name)));
  }
  return m;
});
const topLevel = computed(() => rows.value.filter((r) => !r.parent_id));
const filtered = computed(() => {
  if (!kw.value) return topLevel.value;
  const k = kw.value.toLowerCase();
  return topLevel.value.filter((r) => {
    const kids = childMap.value.get(r.id) || [];
    return [` ${r.name} ${r.domain || ''} ${kids.map((x) => x.name).join(' ')} ${(r.domains || []).join(' ')} ${r.port || ''} ${r.type}`.toLowerCase().includes(k)];
  });
});

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

function roleLabel(c: any) {
  return c.component_role === 'frontend' ? '前端' : c.component_role === 'backend' ? '后端' : '组件';
}

// 仓库外链：只认 https，并把 URL 里的 user:token@ 凭据段剥掉（见 api.ts 的 repoHref）
function repoLink(url: any, size = 13) {
  const href = repoHref(url);
  if (!href) return null;
  return h('a', {
    class: 'cell-repo',
    href,
    target: '_blank',
    rel: 'noopener noreferrer',
    title: '在代码托管站点打开仓库',
    'aria-label': '在代码托管站点打开仓库',
  }, [h(NIcon, { component: icons.OpenOutline, size })]);
}

function syncBtn(r: any, iconOnly = false) {
  if (!r.repo_url) return null;
  // 同步只在配了仓库时可用；这里不重装依赖、不改 unit/nginx，几分钟的部署换成几秒的拉取+重启。
  return h(NButton, {
    size: 'tiny',
    tertiary: true,
    title: '一键同步：拉取最新代码并重启（不重装依赖）',
    'aria-label': `同步 ${r.name}`,
    loading: busyOn(r.id, 'sync'),
    disabled: rowBusy(r.id) || r.deploying,
    icon: ico('CloudDownloadOutline', iconOnly ? 13 : 14),
    onClick: () => sync(r),
  }, { default: () => (iconOnly ? '' : '同步') });
}

function appCell(r: any) {
  const kids = childMap.value.get(r.id) || [];
  const parts = [
    h('div', { class: 'cell-main' }, [
      h('span', { class: 'cell-ico' }, [
        h(NIcon, { component: r.type === 'node' ? icons.LogoElectron : icons.LogoPython, size: 18, color: r.type === 'node' ? 'var(--cp-node)' : 'var(--cp-python)' }),
      ]),
      h('div', { class: 'cell-txt' }, [
        h(NSpace, { size: 6, align: 'center', wrap: false }, () => [
          h(NButton, { text: true, type: 'primary', class: 'cell-name', onClick: () => router.push(`/apps/${r.id}`) }, () => r.name),
          ...(kids.length ? [h(NTag, { size: 'tiny', bordered: false, type: 'info' }, { default: () => `组合 ${kids.length + 1} 件` })] : []),
          repoLink(r.repo_url),
        ]),
        h('div', { class: 'cell-sub' }, r.domain || r.unit || r.path || '—'),
      ]),
    ]),
  ];
  for (const c of kids) {
    parts.push(
      h('div', { class: 'cell-main cell-child' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: c.type === 'node' ? icons.LogoElectron : icons.LogoPython, size: 14, color: c.type === 'node' ? 'var(--cp-node)' : 'var(--cp-python)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h(NSpace, { size: 6, align: 'center', wrap: false }, () => [
            h(NButton, { text: true, type: 'primary', class: 'cell-name', style: 'font-size:var(--fs-sm)', onClick: () => router.push(`/apps/${c.id}`) }, () => c.name),
            h(NTag, { size: 'tiny', bordered: false, type: c.component_role === 'frontend' ? 'success' : 'warning' }, { default: () => roleLabel(c) }),
            ...(!c.running && !c.deploying ? [h('span', { class: 'st idle' }, [h('span', { class: 'dot idle' }), '已停止'])] : []),
            // 组合项目的每个组件是独立仓库/独立服务，同步得分别触发
            syncBtn(c, true),
            repoLink(c.repo_url, 12),
          ]),
        ]),
      ]),
    );
  }
  return parts;
}

const columns: any[] = [
  {
    title: '应用', key: 'name',
    render: (r: any) => appCell(r),
  },
  {
    title: '类型', key: 'type', width: 96,
    render: (r: any) => h('span', { class: r.type === 'node' ? 'vchip node' : 'vchip python' }, r.type === 'node' ? 'Node.js' : 'Python'),
  },
  {
    title: '端口', key: 'port', width: 100,
    render: (r: any) =>
      r.port
        ? h('span', { class: 'mono-dim' }, [
            String(r.port),
            ...(r.portAuto ? [h('span', { style: 'color:var(--cp-text-mute);font-size:var(--fs-2xs);margin-left:var(--space-1)' }, '自动')] : []),
          ])
        : h('span', { class: 'cell-sub' }, '—'),
  },
  {
    title: '域名', key: 'domains', width: 210,
    render: (r: any) => {
      const ds = (r.domains || []) as string[];
      if (!ds.length) return h('span', { class: 'cell-sub' }, '—');
      return h(NSpace, { size: 4, wrap: false }, () => [
        ...ds.slice(0, 2).map((d) => {
          const href = siteHref(d);
          return h(
            NTag,
            href
              ? { size: 'small', bordered: false, type: r.domain ? 'default' : 'info', tag: 'a', href, target: '_blank', rel: 'noopener noreferrer', class: 'dom-tag', title: `在新标签页打开 ${href}` }
              : { size: 'small', bordered: false, type: r.domain ? 'default' : 'info' },
            { default: () => d },
          );
        }),
        ...(ds.length > 2 ? [h('span', { class: 'cell-sub' }, `+${ds.length - 2}`)] : []),
        ...(!r.domain ? [h('span', { style: 'color:var(--cp-text-mute);font-size:var(--fs-2xs)' }, 'nginx 检测')] : []),
      ]);
    },
  },
  {
    title: '状态', key: 'status', width: 110,
    render: (r: any) =>
      r.deploying
        ? h('span', { class: 'st warn' }, [h('span', { class: 'dot warn' }), '部署中'])
        : r.running
          ? h('span', { class: 'st ok' }, [h('span', { class: 'dot ok' }), '运行中'])
          : h('span', { class: 'st idle' }, [h('span', { class: 'dot idle' }), '已停止']),
  },
  {
    title: '操作', key: 'ops', width: 360,
    render: (r: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, type: 'primary', loading: busyOn(r.id, 'deploy'), disabled: rowBusy(r.id), icon: ico('CloudUploadOutline'), onClick: () => act(r.id, 'deploy') }, () => '部署'),
        syncBtn(r),
        h(NButton, {
          size: 'tiny', tertiary: true, type: r.running ? 'warning' : 'success',
          loading: busyOn(r.id, r.running ? 'stop' : 'start'), disabled: rowBusy(r.id),
          icon: ico(r.running ? 'StopOutline' : 'PlayOutline'), onClick: () => act(r.id, r.running ? 'stop' : 'start'),
        }, () => (r.running ? '停止' : '启动')),
        h(NButton, { size: 'tiny', tertiary: true, disabled: !r.running || rowBusy(r.id), loading: busyOn(r.id, 'restart'), icon: ico('SyncOutline'), onClick: () => act(r.id, 'restart') }, () => '重启'),
        h(NButton, { size: 'tiny', quaternary: true, title: '回滚到上一次成功部署', 'aria-label': '回滚', loading: busyOn(r.id, 'rollback'), disabled: rowBusy(r.id), icon: ico('ArrowBackOutline'), onClick: () => rollback(r) }),
        h(NPopconfirm, { onPositiveClick: () => del(r) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline'), title: `删除应用 ${r.name}`, 'aria-label': `删除应用 ${r.name}` }, { default: () => '' }),
          default: () => '仅删除面板配置，服务器文件保留。彻底清理请到应用详情页操作。',
        }),
      ]),
  },
];

const loading = ref(false);
// 行内按钮要知道"是哪个动作在跑"：只记 id 会让部署按钮替启停转圈，
// 同一行连点两次也拦不住。
const busy = ref<{ id: number; verb: string } | null>(null);
const busyOn = (id: number, verb: string) => busy.value?.id === id && busy.value?.verb === verb;
const rowBusy = (id: number) => busy.value?.id === id;
// 延迟刷新不能拖过页面卸载：那是一次没人看的请求，失败还会把错误 toast 打到新页面上。
const timers: number[] = [];
function later(fn: () => void, ms: number) {
  timers.push(window.setTimeout(fn, ms));
}
onBeforeUnmount(() => timers.forEach(clearTimeout));

async function load() {
  loading.value = true;
  try {
    rows.value = await api.apps();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    loading.value = false;
  }
}
async function rollback(r: any) {
  busy.value = { id: r.id, verb: 'rollback' };
  try {
    const res = await api.appAction(r.id, 'rollback');
    msg.success(`已回滚到 ${res.commit || '上一版本'}`);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}
async function sync(r: any) {
  busy.value = { id: r.id, verb: 'sync' };
  try {
    const res = await api.appSync(r.id);
    msg.success(res.changed ? `已同步到 ${res.commit || '最新版本'} 并重启` : `远端无新提交（${res.commit || '当前版本'}）`);
    later(load, 900);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}
async function act(id: number, verb: string) {
  busy.value = { id, verb };
  try {
    if (verb === 'deploy') await api.deploy(id);
    else await api.appAction(id, verb);
    msg.success(verb === 'deploy' ? '部署任务已启动' : '操作已下发');
    later(load, 800);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}
const templates = ref<any[]>([]);
const templateOptions = computed(() => [
  { label: '自定义（手动填写启动命令）', value: '' },
  ...templates.value.map((t: any) => ({ label: `${t.name} — ${t.desc}`, value: t.key })),
]);

async function loadTemplates() {
  try {
    templates.value = (await api.templates()) as any[];
  } catch {
    templates.value = []; // 模板加载失败不阻塞建应用，用户可手动填
  }
}

// 选模板即填默认值，但用户已手改的字段不覆盖
function pickTemplate(key: string) {
  if (!key) return;
  const t = templates.value.find((x: any) => x.key === key);
  if (!t) return;
  form.value.type = t.type;
  form.value.install_cmd = t.install_cmd || '';
  form.value.start_cmd = t.start_cmd || '';
  form.value.port = t.port || null;
}

async function del(r: any) {
  try {
    await api.deleteApp(r.id, false);
    msg.success('已删除配置');
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function create() {
  if (!form.value.name) return msg.error('请填写应用名称');
  if (mode.value === 'split') {
    if (!be.value.start_cmd || !fe.value.start_cmd) return msg.error('请填写前后端的启动命令');
  } else if (!form.value.start_cmd) {
    return msg.error('请填写启动命令');
  }
  creating.value = true;
  try {
    if (mode.value === 'split') {
      applyShared();
      const res: any = await api.createApp({ components: [buildApp(be.value, 'backend'), buildApp(fe.value, 'frontend')] });
      msg.success('前后端项目已创建');
      showCreate.value = false;
      router.push(`/apps/${res.group}`);
      return;
    }
    const a = await api.createApp({
      name: form.value.name,
      type: form.value.type,
      repo_url: form.value.repo_url,
      branch: form.value.branch || 'main',
      domain: form.value.domain || null,
      port: form.value.port || null,
      install_cmd: form.value.install_cmd || null,
      start_cmd: form.value.start_cmd,
      unit_override: form.value.unit_override || null,
      path: form.value.path || null,
      unit_template: form.value.unit_template || null,
      template: form.value.template || undefined,
    });
    msg.success('创建成功');
    showCreate.value = false;
    router.push(`/apps/${a.id}`);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    creating.value = false;
  }
}

onMounted(() => {
  loadTemplates();
  load();
});
</script>

<template>
  <div>
    <PageHeader title="应用" :sub="`共 ${topLevel.length} 个项目 · ${runningCount} 个服务运行中。部署、启停、日志与域名反代集中在此管理；前后端分离项目合并为一个条目`">
      <template #actions>
        <NInput :value="kw" @update:value="(v: string) => (kw = v)" placeholder="搜索名称/域名/端口" size="small" clearable style="width: 200px; max-width: 100%">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
        <NButton size="small" type="primary" :icon="ico('AddOutline')" @click="showCreate = true">新建应用</NButton>
      </template>
    </PageHeader>

    <NDataTable :columns="columns" :data="filtered" :bordered="false" size="small" :row-key="(r: any) => r.id" :loading="loading" :scroll-x="1180" :max-height="'calc(100vh - 268px)'">
      <template #empty>
        <EmptyBox :text="rows.length ? '没有匹配的应用' : '还没有应用 —— 点击「新建应用」接入你的第一个 Node / Python 项目'" />
      </template>
    </NDataTable>

    <NModal v-model:show="showCreate" preset="card" title="新建应用" style="width: 640px; max-width: 94vw">
      <NSpace vertical :size="4">
        <NAlert type="info" :bordered="false" size="small" style="margin-bottom: var(--space-2)">
          {{ mode === 'single'
            ? 'Git 仓库与「已有 unit」都可留空：仓库留空时纳管服务器上已有目录，unit 留空时由面板生成 systemd 服务并接管。'
            : '前后端分离项目会创建两个独立应用（<名称>-backend / <名称>-frontend），在列表中合并为一个项目展示，各自可独立部署、启停与回滚。域名与对外端口通常配在前端。' }}
        </NAlert>
        <NFormItem label="项目形态" style="margin-bottom: var(--space-2)">
          <NRadioGroup v-model:value="mode" size="small" aria-label="项目形态">
            <NRadioButton value="single">单应用</NRadioButton>
            <NRadioButton value="split">前后端分离</NRadioButton>
          </NRadioGroup>
        </NFormItem>
        <NForm label-placement="left" label-width="96">
          <NSpace vertical :size="12">
            <NFormItem label="名称" required>
              <NInput v-model:value="form.name" :placeholder="mode === 'single' ? '小写字母/数字/连字符，如 my-site' : '项目名，自动生成 <名称>-backend / <名称>-frontend'" :input-props="{ 'aria-label': '应用名称' }" />
            </NFormItem>
            <NFormItem v-if="mode === 'single'" label="一键模板">
              <NSelect
                v-model:value="form.template"
                :options="templateOptions"
                size="small"
                placeholder="选一个预设，自动填入运行时/命令/端口（可再手动改）"
                aria-label="一键模板"
                @update:value="pickTemplate"
              />
            </NFormItem>
            <NFormItem v-if="mode === 'single'" label="运行时">
              <NRadioGroup v-model:value="form.type" size="small" aria-label="运行时类型">
                <NRadioButton value="node">Node.js</NRadioButton>
                <NRadioButton value="python">Python</NRadioButton>
              </NRadioGroup>
            </NFormItem>
            <NFormItem label="Git 仓库">
              <NInput v-model:value="form.repo_url" placeholder="https://… 可留空：纳管服务器上已有目录" :input-props="{ 'aria-label': 'Git 仓库地址' }" />
            </NFormItem>
            <NFormItem label="分支">
              <NInput v-model:value="form.branch" placeholder="main" :input-props="{ 'aria-label': '分支' }" />
            </NFormItem>
            <NFormItem label="安装目录">
              <NInput v-model:value="form.path" :placeholder="mode === 'single' ? '留空默认 /root/www/〈名称〉；可指定如 /root/www/my-site' : '留空默认 /root/www/〈名称〉，前后端自动放在 backend / frontend 子目录'" :input-props="{ 'aria-label': '安装目录' }" />
            </NFormItem>
            <NFormItem v-if="mode === 'single'" label="安装命令">
              <NInput
                v-model:value="form.install_cmd"
                :placeholder="form.type === 'node' ? '默认 npm install' : '默认 python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'"
                :input-props="{ 'aria-label': '安装命令', class: 'mono' }"
              />
            </NFormItem>
            <NFormItem v-if="mode === 'single'" label="启动命令" required>
              <NInput v-model:value="form.start_cmd" :placeholder="form.type === 'node' ? '如 npm start 或 node server.js' : '如 .venv/bin/uvicorn main:app --port 8000'" :input-props="{ 'aria-label': '启动命令', class: 'mono' }" />
            </NFormItem>
            <NFormItem v-if="mode === 'single'" label="端口">
              <NInputNumber v-model:value="form.port" placeholder="监听端口，配域名时必填" :input-props="{ 'aria-label': '监听端口' }" style="width: 100%" />
            </NFormItem>
            <NFormItem v-if="mode === 'single'" label="域名">
              <NInput v-model:value="form.domain" placeholder="如 app.choyeon.cc，留空不创建反代" :input-props="{ 'aria-label': '域名' }" />
            </NFormItem>
            <NCollapse v-if="mode === 'split'" style="margin-top: var(--space-1)">
              <NCollapseItem name="be">
                <template #header>
                  <NSpace align="center" :size="8">
                    <NIcon :component="icons.ServerOutline" size="15" color="var(--cp-warning)" />
                    <span style="font-size: var(--fs-sm)">后端（入口应用）</span>
                    <NTag size="tiny" :bordered="false" type="warning">{{ form.name ? `${form.name}-backend` : '—' }}</NTag>
                  </NSpace>
                </template>
                <NSpace vertical :size="10">
                  <NFormItem label="运行时" :show-feedback="false">
                    <NRadioGroup v-model:value="be.type" size="small" aria-label="后端运行时">
                      <NRadioButton value="node">Node.js</NRadioButton>
                      <NRadioButton value="python">Python</NRadioButton>
                    </NRadioGroup>
                  </NFormItem>
                  <NFormItem label="安装命令" :show-feedback="false">
                    <NInput v-model:value="be.install_cmd" :placeholder="be.type === 'node' ? '默认 npm install' : '默认 python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'" :input-props="{ 'aria-label': '后端安装命令', class: 'mono' }" />
                  </NFormItem>
                  <NFormItem label="启动命令" required :show-feedback="false">
                    <NInput v-model:value="be.start_cmd" :placeholder="be.type === 'node' ? '如 npm run start' : '如 .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000'" :input-props="{ 'aria-label': '后端启动命令', class: 'mono' }" />
                  </NFormItem>
                  <NFormItem label="端口" :show-feedback="false">
                    <NInputNumber v-model:value="be.port" placeholder="后端监听端口" :input-props="{ 'aria-label': '后端端口' }" style="width: 100%" />
                  </NFormItem>
                </NSpace>
              </NCollapseItem>
              <NCollapseItem name="fe">
                <template #header>
                  <NSpace align="center" :size="8">
                    <NIcon :component="icons.GlobeOutline" size="15" color="var(--cp-success)" />
                    <span style="font-size: var(--fs-sm)">前端（对外站点）</span>
                    <NTag size="tiny" :bordered="false" type="success">{{ form.name ? `${form.name}-frontend` : '—' }}</NTag>
                  </NSpace>
                </template>
                <NSpace vertical :size="10">
                  <NFormItem label="运行时" :show-feedback="false">
                    <NRadioGroup v-model:value="fe.type" size="small" aria-label="前端运行时">
                      <NRadioButton value="node">Node.js</NRadioButton>
                      <NRadioButton value="python">Python</NRadioButton>
                    </NRadioGroup>
                  </NFormItem>
                  <NFormItem label="安装命令" :show-feedback="false">
                    <NInput v-model:value="fe.install_cmd" placeholder="默认 npm install" :input-props="{ 'aria-label': '前端安装命令', class: 'mono' }" />
                  </NFormItem>
                  <NFormItem label="启动命令" required :show-feedback="false">
                    <NInput v-model:value="fe.start_cmd" placeholder="如 npm run start 或 npx serve dist" :input-props="{ 'aria-label': '前端启动命令', class: 'mono' }" />
                  </NFormItem>
                  <NFormItem label="端口" :show-feedback="false">
                    <NInputNumber v-model:value="fe.port" placeholder="前端监听端口（对外）" :input-props="{ 'aria-label': '前端端口' }" style="width: 100%" />
                  </NFormItem>
                  <NFormItem label="域名" :show-feedback="false">
                    <NInput v-model:value="fe.domain" placeholder="如 app.choyeon.cc，通常配在前端；留空不创建反代" :input-props="{ 'aria-label': '前端域名' }" />
                  </NFormItem>
                </NSpace>
              </NCollapseItem>
            </NCollapse>
            <NFormItem v-if="mode === 'single'" label="已有 unit">
              <NInput v-model:value="form.unit_override" placeholder="纳管用：如 rosetta-backend.service，填了则不覆写 unit" :input-props="{ 'aria-label': '已有 systemd unit' }" />
            </NFormItem>
            <NCollapse v-if="mode === 'single'">
              <NCollapseItem title="高级：自定义 systemd unit 模板" name="tpl">
                <NInput
                  v-model:value="form.unit_template"
                  type="textarea"
                  :autosize="{ minRows: 6, maxRows: 14 }"
                  :input-props="{ class: 'mono', 'aria-label': '自定义 systemd unit 模板', style: 'font-size: var(--fs-xs)' }"
                  placeholder="留空则使用面板默认模板。支持占位符：{{name}} {{path}} {{start_cmd}} {{port}}，部署时自动渲染写入 /etc/systemd/system/panel-<名称>.service"
                />
                <div style="color:var(--cp-text-mute);font-size:var(--fs-xs);margin-top:var(--space-2)">
                  创建后也可在应用详情「systemd 单元」页签随时修改，保存前自动 systemd-analyze verify 校验，失败即回滚。
                </div>
              </NCollapseItem>
            </NCollapse>
            <NButton type="primary" block :loading="creating" @click="create">创建应用</NButton>
          </NSpace>
        </NForm>
      </NSpace>
    </NModal>
  </div>
</template>

<style scoped>
:deep(.cell-child) { margin-top: var(--space-2); padding-left: var(--space-4); border-left: 1px dashed var(--cp-border, rgba(128,128,128,.25)); }
/* 外链小图标：默认是链接蓝， hover 才提亮，避免名称列出现一排抢视线的图标 */
:deep(.cell-repo) { display: inline-flex; align-items: center; color: var(--cp-text-mute); cursor: pointer; transition: color var(--dur) var(--ease); }
:deep(.cell-repo:hover) { color: var(--cp-brand); }
/* 域标签即站点入口：可点击时用品牌色给出反馈，不可点击时保持原样 */
:deep(.dom-tag) { cursor: pointer; transition: color var(--dur) var(--ease), background var(--dur) var(--ease); }
:deep(.dom-tag:hover) { color: var(--cp-brand-soft); background: color-mix(in srgb, var(--cp-brand) 14%, transparent); }
</style>
