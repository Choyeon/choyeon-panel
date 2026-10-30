<script setup lang="ts">
import { onMounted, ref, h, computed } from 'vue';
import { useRouter } from 'vue-router';
import {
  NSpace, NDataTable, NModal, NForm, NFormItem, NInput, NInputNumber, NSelect,
  NRadioGroup, NRadioButton, NIcon, NAlert, NTag, NCollapse, NCollapseItem,
  useMessage,
} from 'naive-ui';
import { NButton, unitTag } from '../ui';
import { api, repoHref, siteHref, getRole } from '../api';
import { icon, icons } from '../icons';
import { useDelayedRefresh } from '../composables/useDelayedRefresh';
import { useVisiblePoll } from '../composables/useVisiblePoll';
// 应用清单还有别的消费者：备份页的「选应用」下拉缓存 key 'apps' 60 秒。本页增删改名后
// 不失效它，那个下拉会在最长一分钟内选不到刚建的应用、或者把已删的应用列着让人去建备份任务。
import { invalidateCache } from '../composables/useCachedFetch';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import CpToolbarCard from '../components/CpToolbarCard.vue';
import CpRowActions from '../components/CpRowActions.vue';
import CpStatusTag from '../components/CpStatusTag.vue';
import ConfirmActionDialog from '../components/ConfirmActionDialog.vue';

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

const ico = (name: string, size = 14) => icon(name, size);

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

function syncBtn(r: any) {
  if (!isAdmin.value || !r.repo_url) return null;
  // 同步只在配了仓库时可用；这里不重装依赖、不改 unit/nginx，几分钟的部署换成几秒的拉取+重启。
  return h(NButton, {
    size: 'tiny',
    tertiary: true,
    title: '一键同步：拉取最新代码并重启（不重装依赖）',
    loading: busyOn(r.id, 'sync'),
    disabled: rowBusy(r.id) || r.deploying,
    icon: ico('CloudDownloadOutline', 14),
    onClick: () => sync(r),
  }, { default: () => '同步' });
}

function appCell(r: any) {
  const kids = childMap.value.get(r.id) || [];
  const parts = [
    h('div', { class: 'cell-main' }, [
      h('span', { class: 'cell-ico' }, [
        h(NIcon, { component: r.type === 'node' ? icons.LogoElectron : icons.LogoPython, size: 18, color: r.type === 'node' ? 'var(--cp-node)' : 'var(--cp-python)' }),
      ]),
      h('div', { class: 'cell-txt' }, [
        // 名称行允许整项换行：200px 的名称列装不下「rosetta-frontend [前端] [同步] ↗」（自然宽 289），
        // nowrap 会让徽章与按钮越过列边界压在类型列上。名字自己 ellipsis，附属标记折到下一行。
        h(NSpace, { size: 6, align: 'center' }, () => [
          h(NButton, { text: true, type: 'primary', class: 'cell-name', title: r.name, onClick: () => router.push(`/apps/${r.id}`) }, () => r.name),
          ...(kids.length ? [h(NTag, { size: 'tiny', bordered: false, type: 'info' }, { default: () => `组合 ${kids.length + 1} 件` })] : []),
          repoLink(r.repo_url),
        ]),
        // 名称与描述都会被裁：描述折行（手机上摸不到 hover），title 只作桌面端补充
        h('div', { class: 'cell-sub cp-prose', title: r.domain || r.unit || r.path || '—' }, r.domain || r.unit || r.path || '—'),
      ]),
    ]),
  ];
  for (const c of kids) {
    const ct = unitTag(c);
    parts.push(
      h('div', { class: 'cell-main cell-child' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: c.type === 'node' ? icons.LogoElectron : icons.LogoPython, size: 14, color: c.type === 'node' ? 'var(--cp-node)' : 'var(--cp-python)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h(NSpace, { size: 6, align: 'center' }, () => [
            h(NButton, { text: true, type: 'primary', class: 'cell-name', style: 'font-size:var(--fs-sm)', title: c.name, onClick: () => router.push(`/apps/${c.id}`) }, () => c.name),
            h(NTag, { size: 'tiny', bordered: false, type: c.component_role === 'frontend' ? 'success' : 'warning' }, { default: () => roleLabel(c) }),
            ...(!c.deploying && ct.status !== 'running'
              ? [h('span', { class: `st ${ct.tone}` }, [h('span', { class: `dot ${ct.tone}` }), ct.text])]
              : []),
            // 组合项目的每个组件是独立仓库/独立服务，同步得分别触发
            syncBtn(c),
            repoLink(c.repo_url, 12),
          ]),
        ]),
      ]),
    );
  }
  return parts;
}

// 1Panel 的操作列是文字按钮 + 5px gap，超过 ellipsis 个收进「更多」；
// 列宽按它的估算取 35 + 2*58 + 58 = 209。
function rowActions(r: any) {
  if (!isAdmin.value) return [];
  const stop = !!r.running;
  // 单元在本机 systemd 里不存在时，"启动/重启"只会换来一句 not loaded；部署与删除仍然留着，
  // 否则这条记录在界面上就再也处理不掉了（部署会把 unit 写回去）。
  const gone = !!r.unitMissing;
  const list: { label: string; run: () => void; danger?: boolean; disabled?: boolean }[] = [
    { label: '部署', run: () => act(r.id, 'deploy'), disabled: rowBusy(r.id) },
  ];
  if (r.repo_url) list.push({ label: '同步', run: () => sync(r), disabled: rowBusy(r.id) || !!r.deploying });
  list.push({ label: stop ? '停止' : '启动', run: () => act(r.id, stop ? 'stop' : 'start'), disabled: rowBusy(r.id) || (!stop && gone) });
  list.push({ label: '重启', run: () => act(r.id, 'restart'), disabled: !stop || rowBusy(r.id) || gone });
  list.push({ label: '回滚', run: () => rollback(r), disabled: rowBusy(r.id) });
  list.push({ label: '删除', run: () => askDel(r), danger: true, disabled: rowBusy(r.id) });
  return list;
}

const columns: any[] = [
  {
    // 弹性列必须给 minWidth：scroll-x auto 下没有它，固定列一超过容器就被挤成 0 宽（375px 实测名字看不见）
    title: '应用', key: 'name', minWidth: 200,
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
    // 域名不能全塞进一行：1440 档这一列的内容宽是 186px，而三个域名的自然宽是 226px，
    // 原先 nowrap 会把尾部条目挤到 1ch 再逐字换行（「nginx 检测」竖成一排压到状态列上）。
    // 现在整项换行：宁可「+1」落到第二行，也不把域名本身截掉尾缀；只有单个域名长过整列时才 ellipsis。
    // 240 是两个域名（191）+ 计数（15）+ 间距与内边距的下限；余量留给名称列，它才是需要读完整的那一列。
    title: '域名', key: 'domains', width: 240,
    render: (r: any) => {
      const ds = (r.domains || []) as string[];
      if (!ds.length) return h('span', { class: 'cell-sub' }, '—');
      return h('div', { class: 'dom-cell' }, [
        h('div', { class: 'dom-line' }, [
          ...ds.slice(0, 2).map((d) => {
            const href = siteHref(d);
            return h(
              NTag,
              href
                ? { size: 'small', bordered: false, type: r.domain ? 'default' : 'info', tag: 'a', href, target: '_blank', rel: 'noopener noreferrer', class: 'dom-tag', title: `在新标签页打开 ${href}` }
                : { size: 'small', bordered: false, type: r.domain ? 'default' : 'info', class: 'dom-tag', title: d },
              { default: () => d },
            );
          }),
          ...(ds.length > 2 ? [h('span', { class: 'dom-more', title: ds.slice(2).join('、') }, `+${ds.length - 2}`)] : []),
        ]),
        ...(!r.domain ? [h('div', { class: 'dom-hint' }, 'nginx 检测')] : []),
      ]);
    },
  },
  {
    title: '状态', key: 'status', width: 110,
    render: (r: any) => {
      if (r.deploying) return h(CpStatusTag, { status: 'deploying', text: '部署中' });
      const t = unitTag(r);
      // 「未纳管」要说清未纳管的是哪个名字，否则用户只能猜；进详情页有完整判词与「已有 unit」输入框。
      if (r.suggestUnit)
        return h('span', { title: `本机有同名 unit ${r.suggestUnit}，在详情页「已有 unit」里登记纳管` }, [
          h(CpStatusTag, { status: t.status, text: t.text }),
        ]);
      return h(CpStatusTag, { status: t.status, text: t.text });
    },
  },
  {
    title: '操作', key: 'ops', width: 209, align: 'center', fixed: 'right' as const,
    render: (r: any) => h(CpRowActions, { buttons: rowActions(r), ellipsis: 2 }),
  },
];

const loading = ref(false);
// 首帧读取失败的第三态：读不到时 rows 是空数组，空态本来会写「还没有应用」并引导新建——
// 那是把一次接口失败粉饰成"这台机器没应用"，用户还可能照着提示建一个和真实应用重名的。
const loadFailed = ref('');
/**
 * 行内动作按 id 各占一格，verb 决定名称列的「同步」该不该转圈，
 * 操作列的文字按钮没有加载态，靠 rowBusy 整行置灰挡住连点。
 * 以前是一个单槽 `{id, verb}`：B 行点一下就把 A 行在飞的那份顶掉，A 的 finally 又清掉 B 的，
 * 于是两行的按钮都在自己的请求还没回来时就恢复，连点会并发同一份部署/回滚。
 */
const busy = ref<Record<number, string>>({});
const busyOn = (id: number, verb: string) => busy.value[id] === verb;
const rowBusy = (id: number) => busy.value[id] !== undefined;
function startBusy(id: number, verb: string) { busy.value = { ...busy.value, [id]: verb }; }
function endBusy(id: number) {
  const next = { ...busy.value };
  delete next[id];
  busy.value = next;
}
// 后端 deps.py 对所有 MUTATION 全局拦 403，只读账号点这些按钮必然失败：
// 按钮直接不进 DOM，而不是留一个按不动的入口；整列都没有可用动作时连列头一起收掉，
// 免得留一条 209px 的空白列，看着像表格没加载出来。
const isAdmin = computed(() => getRole() === 'admin');
const visColumns = computed(() => columns.filter((c) => isAdmin.value || c.key !== 'ops'));
// 延迟刷新走共享的 useDelayedRefresh，本页不再自己抄一份 timers 数组
const { later } = useDelayedRefresh();

// 部署是后台任务，deploying 会自己落定：列表里只要还有部署中的行就每 10s 静默续拉，
// 全部落定就不再挂定时器（不轮询的话用户只能手动点刷新才知道部署完了）。
// 部署动辄几分钟，期间标签页常常不在前台，所以走 useVisiblePoll。
const deployPoll = useVisiblePoll(() => {
  void loadQuiet().then(() => { if (!rows.value.some((r) => r.deploying)) deployPoll.stop(); });
}, 10000);
function schedulePoll() {
  if (rows.value.some((r) => r.deploying)) deployPoll.start();
  else deployPoll.stop();
}

async function load() {
  loading.value = true;
  try {
    rows.value = await api.apps();
    loadFailed.value = '';
  } catch (e: any) {
    loadFailed.value = e.message || '应用列表读取失败';
    msg.error(e.message);
  } finally {
    loading.value = false;
    schedulePoll();
  }
}
async function loadQuiet() {
  try {
    rows.value = await api.apps();
  } catch {
    /* 轮询失败保留上一帧，下个周期自动重试 */
  }
}
async function rollback(r: any) {
  if (rowBusy(r.id)) return;
  startBusy(r.id, 'rollback');
  try {
    const res = await api.appAction(r.id, 'rollback');
    msg.success(`已回滚到 ${res.commit || '上一版本'}`);
    later(load, 900);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    endBusy(r.id);
  }
}
async function sync(r: any) {
  if (rowBusy(r.id)) return;
  startBusy(r.id, 'sync');
  try {
    const res = await api.appSync(r.id);
    msg.success(res.changed ? `已同步到 ${res.commit || '最新版本'} 并重启` : `远端无新提交（${res.commit || '当前版本'}）`);
    later(load, 900);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    endBusy(r.id);
  }
}
async function act(id: number, verb: string) {
  if (rowBusy(id)) return;
  startBusy(id, verb);
  try {
    if (verb === 'deploy') await api.deploy(id);
    else await api.appAction(id, verb);
    msg.success(verb === 'deploy' ? '部署任务已启动' : '操作已下发');
    later(load, 800);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    endBusy(id);
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

const opRef = ref<any>(null);
function askDel(r: any) {
  // 删除走 1Panel 的 OpDialog：标题 + 名单 + 一句后果，不在行内现写弹窗
  opRef.value?.acceptParams({
    title: '删除应用配置',
    names: [r.name],
    msg: '此操作将仅删除面板配置，服务器文件保留；需要彻底清理请到应用详情页操作。是否继续？',
    api: async () => {
      await api.deleteApp(r.id, false);
      invalidateCache('apps');
      load();
    },
    successMsg: '已删除配置',
  });
}
// 关窗即恢复初值：NModal 里的表单是常驻组件状态，不重置的话，
// 取消后重开还留着上一次的名称/仓库/命令，用户会在旧值上继续点「创建」。
function resetCreate() {
  mode.value = 'single';
  form.value = singleFields();
  be.value = singleFields({ start_cmd: '', port: 8000 });
  fe.value = singleFields({ type: 'node', start_cmd: 'npm run start', port: 3100 });
}
async function create() {
  if (creating.value) return;
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
      invalidateCache('apps');
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
    invalidateCache('apps');
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
    <PageHeader title="应用" :sub="`共 ${topLevel.length} 个项目 · ${runningCount} 个服务运行中`" />

    <!-- 1Panel 把长说明从标题里挪到表头上方的 info alert，并在尾部给一个快捷跳转 -->
    <NAlert type="info" :bordered="false" class="apps-prompt">
      <div class="apps-prompt-row">
        <span>部署、启停、日志与域名反代集中在此管理；前后端分离项目会合并为一个条目展示，各组件仍可独立操作。</span>
        <NButton text type="primary" size="small" :icon="ico('ArrowForwardOutline')" @click="router.push('/sites')">网站与证书</NButton>
      </div>
    </NAlert>

    <CpToolbarCard>
      <template #left>
        <NButton v-if="isAdmin" size="small" type="primary" round :icon="ico('AddOutline')" @click="showCreate = true">新建应用</NButton>
      </template>
      <template #right>
        <NInput :value="kw" @update:value="(v: string) => (kw = v)" placeholder="搜索名称/域名/端口" size="small" clearable style="width: 200px; max-width: 100%">
          <template #prefix><NIcon :component="icons.SearchOutline" /></template>
        </NInput>
        <NButton size="small" tertiary round :loading="loading" :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>

      <NDataTable :columns="visColumns" :data="filtered" :bordered="false" size="small" :row-key="(r: any) => r.id" :loading="loading" :scroll-x="'auto'" class="cp-tbl tbl-apps" :max-height="'var(--table-h)'">
        <template #empty>
          <EmptyBox :text="rows.length ? '没有匹配的应用' : (loadFailed ? '应用列表读取失败，点上方「刷新」重试' : '还没有应用 —— 点击「新建应用」接入你的第一个 Node / Python 项目')" />
        </template>
      </NDataTable>
    </CpToolbarCard>

    <ConfirmActionDialog ref="opRef" />

    <NModal v-model:show="showCreate" preset="card" title="新建应用" style="width: 640px; max-width: 94vw" @after-leave="resetCreate">
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
                    <NIcon :component="icons.ServerOutline" size="15" color="var(--cp-warn)" />
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
                    <NIcon :component="icons.GlobeOutline" size="15" color="var(--cp-ok)" />
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
            <NButton type="primary" block :loading="creating" :disabled="creating" @click="create">创建应用</NButton>
          </NSpace>
        </NForm>
      </NSpace>
    </NModal>
  </div>
</template>

<style scoped>
/* 顶部提示与下方内容卡之间是 1Panel 的 common-prompt 间距 */
.apps-prompt { margin-bottom: var(--space-5); }
.apps-prompt-row { display: flex; align-items: center; gap: 4px 8px; flex-wrap: wrap; }
/* 说明 alert 和工具行都排在表格之外，把它们占掉的像素补进 --table-h 的扣减项（1Panel 的 heightDiff 语义）；
   带 .cp-tbl 前缀是为了盖过 style.css 里 .tbl-apps 的 0px，不改共享文件。 */
.cp-tbl.tbl-apps { --chrome-extra: 140px; }
:deep(.cell-child) { margin-top: var(--space-2); padding-left: var(--space-4); border-left: 1px dashed var(--cp-border, rgba(128,128,128,.25)); }
/* 外链小图标：默认是链接蓝， hover 才提亮，避免名称列出现一排抢视线的图标 */
:deep(.cell-repo) { display: inline-flex; align-items: center; color: var(--cp-text-mute); cursor: pointer; transition: color var(--dur) var(--ease); }
:deep(.cell-repo:hover) { color: var(--cp-brand); }
/* 域标签即站点入口：可点击时用品牌色给出反馈，不可点击时保持原样 */
:deep(.dom-tag) { cursor: pointer; transition: color var(--dur) var(--ease), background var(--dur) var(--ease); }
:deep(.dom-tag:hover) { color: var(--cp-brand-soft); background: color-mix(in srgb, var(--cp-brand) 14%, transparent); }
/* NTag 默认不收缩：域名比列宽长时会被推到列外、末字裁半截。给内容层做 ellipsis，
   整标签作为 flex 项允许收缩（title 里是完整域名）。 */
:deep(.dom-tag) { max-width: 100%; min-width: 0; }
:deep(.dom-tag .n-tag__content) { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 域名格：标签与「+N」占一行（放不下就整项换到下一行），来源说明固定另起一行。
   nowrap 时最先被挤扁的是注脚「nginx 检测」，它会竖成一列字符爬进状态列——注脚不该和域名抢宽度。 */
:deep(.dom-cell) { min-width: 0; }
:deep(.dom-line) { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-width: 0; }
:deep(.dom-more) { flex: none; color: var(--cp-text-mute); font-size: var(--fs-2xs); }
:deep(.dom-hint) { margin-top: 2px; color: var(--cp-text-mute); font-size: var(--fs-2xs); white-space: nowrap; }
</style>
