<script setup lang="ts">
import { onMounted, ref, watch, h, computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NCard, NSpace, NText, NTag, NInput, NDynamicInput,
  useMessage, NPopconfirm, NIcon, NForm, NFormItem, NSkeleton, NGrid, NGridItem, NSelect, NAlert,
} from 'naive-ui';
import { NButton, unitTag } from '../ui';
import { api, repoHref, siteHref, getRole } from '../api';
import { icon, icons } from '../icons';
import { useDelayedRefresh } from '../composables/useDelayedRefresh';
import { fmtBytes } from '../composables/useFormat';
import LogStream from '../components/LogStream.vue';
import EmptyBox from '../components/EmptyBox.vue';
import PageTabBar from '../components/PageTabBar.vue';
import DescriptionsCard from '../components/DescriptionsCard.vue';
import CpStatusTag from '../components/CpStatusTag.vue';
import ConfirmActionDialog from '../components/ConfirmActionDialog.vue';

const route = useRoute();
const router = useRouter();
const msg = useMessage();
let id = Number(route.params.id);
const app = ref<any>(null);
const env = ref<{ k: string; v: string }[]>([]);
const edit = ref<any>({});
const deps = ref<any[]>([]);
const depLog = ref<any>(null);
// 延迟刷新走共享实现：本页多处写成 `await api.xxx(); later(load, 600)`，
// 自建 timers 数组在 dispose 时定时器往往还没挂上，接口回来照样刷新，
// 于是在别的页面上多发请求、把不相干的错误 toast 打过去。
const { later, cancel } = useDelayedRefresh();
const isAdmin = computed(() => getRole() === 'admin');
const saving = ref(false);
const acting = ref('');
// 页签受控 + 内容用 v-if 挂载：运行日志那条 SSE 只有在当前页签时才存在，
// 切走就随组件销毁，不会在背后白占一个 journalctl -f。
const tab = ref('config');
const envView = ref<'detail' | 'edit'>('detail');
const opRef = ref<any>(null);
const loadErr = ref('');
const sslEmail = ref('');

const ngx = ref<any>(null);
const ngxFile = ref<any>(null);
const ngxContent = ref('');
const ngxSaving = ref(false);
const bodySizeVal = ref('50m');
const pgOptions = ref<any[]>([]);
// 清单没取到时下拉是空的，而空的下拉和"这台机器没有数据库"长得一模一样
const pgErr = ref('');
const dbSel = ref<string[]>([]);

const unitInfo = ref<any>(null);
const unitContent = ref('');
const unitSaving = ref(false);
const proc = ref<any>(null);
// 同上：读不到进程读数时，卡片要说的是"这一次没取到"，不是"systemd 没给读数"
const procErr = ref('');

// 仓库外链：只认 https，且 URL 里的 user:token@ 凭据段已在 repoHref 中剥掉，
// 密钥既不会显示在页面上，也不会进地址栏和新标签页。
const repoUrl = computed(() => repoHref(app.value?.repo_url));

// 域名同样只走白名单化的外链构造（siteHref），nginx 检测出的怪值不会变成可点链接
const siteUrl = computed(() => siteHref(app.value?.domain || ngx.value?.configs?.[0]?.analysis?.serverNames?.[0]));

const panes = computed(() => [
  { key: 'config', label: '基本配置' },
  { key: 'env', label: '环境变量' },
  // badge 只表示「有非成功记录」，不假设部署记录的排序方向
  { key: 'deploy', label: '部署记录', badge: deps.value.some((d: any) => d.status !== 'success' && d.status !== 'running') },
  { key: 'logs', label: '运行日志' },
  { key: 'nginx', label: '域名 / Nginx' },
  { key: 'unit', label: 'systemd 单元' },
  { key: 'ssl', label: 'SSL 证书' },
]);

const domainText = computed(() => app.value?.domain || ngx.value?.configs?.[0]?.analysis?.serverNames?.[0] || '');

// 1Panel 的详情表就是「一列一条事实」：后端没有的字段（如运行时长）直接不出现，不补占位值。
const infoItems = computed(() => {
  const a = app.value || {};
  const port = a.port ? String(a.port) : '';
  const repo = a.repo_url ? `${a.repo_url} @ ${a.branch || 'main'}` : '';
  return [
    { label: '运行时', value: a.type === 'node' ? 'Node.js' : a.type === 'python' ? 'Python' : a.type || '' },
    { label: '监听端口', value: port ? `${port}${a.portAuto ? '（自动检测）' : ''}` : '', mono: true, copy: port || undefined },
    { label: '域名', value: domainText.value, slot: siteUrl.value ? 'site' : undefined },
    { label: '仓库 / 分支', value: repo || '本地目录', mono: true, copy: a.repo_url || undefined, slot: repoUrl.value ? 'repo' : undefined },
    { label: '应用路径', value: a.path || '', mono: true, copy: a.path || undefined },
  ];
});

const resItems = computed(() => {
  const p = proc.value;
  if (!p) return [];
  return [
    { label: '进程 PID', value: p.running && p.pid ? String(p.pid) : '未运行', mono: true },
    { label: '内存', value: fmtBytes(p.memoryBytes), mono: true },
    { label: 'CPU 累计', value: p.cpuSeconds != null ? `${p.cpuSeconds}s` : '', mono: true },
    { label: '重启次数', value: p.restarts != null ? String(p.restarts) : '' },
    { label: '自启动时间', value: p.execStartAt || p.activeSince || '', mono: true },
    { label: '子状态', value: p.subState || '' },
  ];
});

// 环境变量里名字像凭据的一律遮罩（1Panel 的 password 型参数处理方式）
const SECRET_KEY_RE = /(pass(word)?|pwd|secret|token|api[_-]?key|access[_-]?key|credential)/i;
const envItems = computed(() =>
  env.value
    .filter((x) => x && typeof x.k === 'string' && x.k)
    .map((x) => ({ label: x.k, value: x.v, mono: true, secret: SECRET_KEY_RE.test(x.k) })),
);

function onDescSlot(name: string) {
  const url = name === 'site' ? siteUrl.value : name === 'repo' ? repoUrl.value : '';
  if (url) window.open(url, '_blank', 'noopener,noreferrer');
}

const appStatus = computed(() => {
  if (app.value?.deploying) return { status: 'installing', text: '部署中' };
  // 与列表页同一份说法（ui.ts 的 unitTag）：详情页以前也是 running 二选一，
  // 于是"正在启动"的项目在这里同样被写成"已停止"。
  const t = unitTag(app.value || {});
  return { status: t.status, text: t.text };
});

async function loadUnit() {
  const requested = id;
  try {
    const r = await api.appUnit(requested);
    // 组合项目条在 /apps/1 ↔ /apps/2 之间复用本组件，切页只换 id、不取消 in-flight 请求：
    // 上一个应用的回包晚一步回来，就会盖到当前这一页的 unit 全文上——那是别的应用的
    // Environment= 凭据与路径，而且「保存」会把这份内容写回现在这个应用。
    if (requested !== id) return;
    unitInfo.value = r;
    unitContent.value = r.content || r.defaultTemplate;
  } catch (e: any) {
    if (requested !== id) return;
    msg.error(e.message);
  }
}
async function saveUnitFile() {
  // 保存 unit 是「写文件 + systemd-analyze verify」两段：连点的第二次会在第一次还没校验完时
  // 把文件换掉，报出来的语法错误对应的是另一份内容。
  if (unitSaving.value) return;
  unitSaving.value = true;
  try {
    await api.saveUnit(id, unitContent.value);
    msg.success('已保存并通过 systemd-analyze verify，重启服务后生效');
    await loadUnit();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    unitSaving.value = false;
  }
}
function restoreDefaultTpl() {
  unitContent.value = unitInfo.value?.defaultTemplate || '';
}
async function loadProc() {
  const requested = id;
  try {
    const r = await api.appProc(requested);
    if (requested !== id) return;
    proc.value = r;
    procErr.value = '';
  } catch (e: any) {
    // 不弹 toast：卡片就在这一页上，就地写清"这一次没取到"比刷屏强
    if (requested !== id) return;
    procErr.value = e?.message || '进程读数获取失败';
  }
}
async function loadNginx() {
  const requested = id;
  try {
    const r = await api.appNginx(requested);
    if (requested !== id) return;
    ngx.value = r;
    if (r.configs?.length) selConfig(r.configs[0]);
  } catch (e: any) {
    if (requested !== id) return;
    msg.error(e.message);
  }
}
function selConfig(c: any) {
  ngxFile.value = c;
  ngxContent.value = c.content;
}
async function saveNginx() {
  if (ngxSaving.value) return;
  ngxSaving.value = true;
  try {
    await api.saveNginx(id, ngxFile.value.file, ngxContent.value);
    msg.success('已保存并通过 nginx -t 校验');
    await loadNginx();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    ngxSaving.value = false;
  }
}
async function quick(kind: string) {
  // 快捷项与「保存配置」都是读-改-写同一个文件：并发跑会互相覆盖，
  // 后端还会同时写同一个临时文件，所以共用 ngxSaving 这把锁。
  if (ngxSaving.value) return;
  ngxSaving.value = true;
  try {
    await api.quickNginx(id, ngxFile.value.file, kind, kind === 'body' ? bodySizeVal.value : undefined);
    msg.success('快捷配置已生效');
    await loadNginx();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    ngxSaving.value = false;
  }
}
async function loadPg() {
  try {
    const dbs = await api.pgDbs();
    if (Array.isArray(dbs)) {
      pgOptions.value = dbs.map((d: any) => ({ label: d.name, value: d.name }));
      pgErr.value = '';
    }
  } catch (e: any) {
    // 这里也不弹 toast：没装 PostgreSQL 的机器上，每一次进详情页都会失败一次。
    pgErr.value = e?.message || '数据库清单获取失败';
  }
}

const ico = (name: string, size = 14) => icon(name, size);

async function load() {
  const requested = id;
  loadErr.value = '';
  try {
    const data = await api.app(requested);
    if (requested !== id) return;
    if (!data) {
      loadErr.value = '未找到该应用，可能已被删除';
      return;
    }
    app.value = data;
    edit.value = { ...app.value, port: app.value.port ? String(app.value.port) : '', unit_override: app.value.unit_override || '' };
    env.value = JSON.parse(app.value.env || '[]');
    dbSel.value = (app.value.db_names || '').split(',').filter(Boolean);
    const list = await api.deployments(requested);
    if (requested !== id) return;
    deps.value = list;
  } catch (e: any) {
    // 切走之后这一轮的失败已经不属于当前页了：把 loadErr 设上会把 B 的详情页
    // 整页换成「未找到该应用，可能已被删除」这种针对 A 的错误。
    if (requested !== id) return;
    loadErr.value = e.message || '加载应用详情失败';
    return;
  }
  loadNginx();
  loadPg();
  // unit 全文里有 Environment= 凭据与本机路径，后端只认管理员；只读账号不发这个请求，
  // 否则每次进详情页都弹一条 403 红条。
  if (isAdmin.value) loadUnit();
  loadProc();
  // 带着 running 记录进入/刷新页面时接续轮询，否则部署记录停在打开那一刻
  if (deps.value.some((d: any) => d.status === 'running')) later(pollDeps, 3000);
}

async function save() {
  // 后端会把这份配置渲染进 unit / .panel.env / nginx 并顺带重启：第二次 PUT 带的是第一次
  // 还没应用完的旧字段，等于把刚保存的改动盖回去。
  if (saving.value) return;
  saving.value = true;
  try {
    const r: any = await api.updateApp(id, {
      name: edit.value.name,
      type: edit.value.type,
      repo_url: edit.value.repo_url || '',
      branch: edit.value.branch || 'main',
      domain: edit.value.domain || null,
      port: edit.value.port ? Number(edit.value.port) : null,
      install_cmd: edit.value.install_cmd || null,
      start_cmd: edit.value.start_cmd,
      unit_override: edit.value.unit_override || null,
      path: edit.value.path || null,
      db_names: dbSel.value.join(',') || null,
      env: env.value.filter((x) => x && typeof x.k === 'string' && x.k),
    });
    // 「已保存」和「已生效」是两件事：后端会把改动重新渲染进 unit / .panel.env / nginx，
    // 服务在跑就顺带重启。这一步失败或压根不该做（外部 unit）时只报"已保存"，
    // 用户会以为新配置已经在跑，于是把"改了没反应"当成玄学问题去排查。
    const ap = r?.applied || {};
    if (ap.note) msg.warning(ap.note);
    else if (ap.restarted) msg.success('已保存，配置已同步并重启服务');
    else msg.success('已保存');
    await load();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    saving.value = false;
  }
}

async function deploy() {
  // 部署没有服务端互斥（它自己就是那条 running 记录的写入者）：连点会同时跑两轮
  // git/依赖安装，两个进程在同一份 working tree 上互相踩。
  if (acting.value) return;
  acting.value = 'deploy';
  try {
    await api.deploy(id);
    msg.success('部署已开始');
    later(pollDeps, 1000);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    acting.value = '';
  }
}
async function sync() {
  // 同步在服务端有 _ensure_no_deployment 拦着，第二次请求只会撞回那句「部署进行中，不能同步」，
  // 把第一次的结果提示盖掉。
  if (acting.value) return;
  acting.value = 'sync';
  try {
    const r = await api.appSync(id);
    msg.success(r.changed ? `已同步到 ${r.commit || '最新版本'} 并重启` : `远端无新提交（${r.commit || '当前版本'}）`);
    // load() 里已经带上了 deployments，不必再单独轮询一次
    later(load, 1200);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    acting.value = '';
  }
}
async function pollDeps() {
  const requested = id;
  // 轮询是 setTimeout 里跑的，失败若不在这里吞掉就变成未处理的 promise rejection：
  // 界面上部署记录停在旧数据、也没有任何提示。
  let list: any;
  try {
    list = await api.deployments(requested);
  } catch (e: any) {
    if (requested !== id) return;
    msg.error(`部署记录获取失败：${e.message}`);
    return;
  }
  if (requested !== id) return;
  deps.value = list;
  // 头部徽标看的是 app.deploying：只换记录列表的话「部署中」要等手动刷新才出现
  let fresh: any;
  try {
    fresh = await api.app(requested);
  } catch {
    /* 保留上一帧，下个周期自动重试 */
  }
  if (requested !== id) return;
  if (fresh) app.value = fresh;
  // 还有 running 记录就继续轮：部署是后台任务，不轮询用户只能反复点「刷新」才知道跑完了
  if (deps.value.some((d: any) => d.status === 'running')) later(pollDeps, 5000);
}
async function viewDep(depId: number) {
  const requested = id;
  try {
    const r = await api.deployment(requested, depId);
    // 浮层是按 depLog 有没有值弹出的：切到别的应用后旧应用的日志回包会把浮层再顶出来，
    // 标题写的是现在这个应用，内容却是上一个的部署日志。
    if (requested !== id) return;
    depLog.value = r;
  } catch (e: any) {
    if (requested !== id) return;
    msg.error(`部署日志获取失败：${e.message}`);
  }
}
async function act(verb: string) {
  // 早退兜底：unit 页签里「保存后重启服务」这颗按钮离顶部动作组很远，
  // 只靠 :disabled 传播到它，一旦以后再有别的入口调 act 就没人拦着了。
  if (acting.value) return;
  acting.value = verb;
  try {
    await api.appAction(id, verb);
    // 启停/重启原来只有出错才吭声，成功只靠 600ms 后状态列自己变；
    // 补一句按动词命名的回执，点了「重启」就知道重启这一下送出去了。
    msg.success({ start: '已启动服务', stop: '已停止服务', restart: '已重启服务' }[verb] || `已执行 ${verb}`);
    later(load, 600);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    acting.value = '';
  }
}
async function ssl() {
  // certbot 是分钟级外部命令，连点会并发跑两个签发进程互相抢同一张证书锁
  if (acting.value) return;
  acting.value = 'ssl';
  try {
    await api.appSsl(id, sslEmail.value || undefined);
    msg.success('证书已配置');
    // 申请成功后后端会把 vhost 改成 HTTPS，nginx 页签的分析结果必须跟着刷新
    later(loadNginx, 600);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    acting.value = '';
  }
}
function purge() {
  // 1Panel 的删除走 OpDialog：标题 + 被操作对象名单 + 一句后果，页面里不再现写弹窗
  const target = id;
  opRef.value?.acceptParams({
    title: '彻底删除应用',
    names: [app.value?.name, app.value?.unit, app.value?.path].filter(Boolean),
    msg: '此操作将删除面板配置、systemd unit 与目录文件，不可恢复，是否继续？',
    api: async () => {
      // 弹窗挂在页面外面：开着它经组合项目条跳到 /apps/2，确认按钮读到的 id 已经是另一个应用，
      // 而标题里点名的还是 A 的名字与目录——那是照着 A 的名单删 B。跳页就作废这一次确认。
      if (target !== id) throw new Error('已经切换到别的应用，这次删除作废；要删请回到那个应用再点一次');
      await api.deleteApp(target, true);
      router.push('/apps');
    },
    successMsg: '已删除并清理文件',
  });
}

onMounted(load);
// 组合项目组件条在 /apps/1 ↔ /apps/2 之间跳转时路由复用本组件：
// 不重新取 id、不重置状态的话，页面会停在旧应用上，看起来就像"点了没反应"。
watch(
  () => Number(route.params.id),
  (n) => {
    id = n;
    app.value = null;
    ngx.value = null;
    ngxFile.value = null;
    ngxContent.value = '';
    unitInfo.value = null;
    unitContent.value = '';
    proc.value = null;
    depLog.value = null;
    deps.value = [];
    cancel();   // 切应用时掐掉上一个应用挂着的部署轮询
    load();
  },
);
</script>

<template>
  <div v-if="loadErr" class="page-loading">
    <NAlert type="error" :bordered="false" role="alert">{{ loadErr }}</NAlert>
    <NSpace justify="center">
      <NButton size="small" class="cp-press" @click="load">重新加载</NButton>
      <NButton size="small" quaternary @click="router.push('/apps')">返回列表</NButton>
    </NSpace>
  </div>
  <div v-else-if="!app" class="page-loading">
    <NSkeleton height="52px" class="cp-shimmer" style="border-radius: var(--radius-lg)" />
    <NSkeleton v-for="i in 3" :key="i" height="88px" class="cp-shimmer" style="border-radius: var(--radius-lg)" />
  </div>
  <div v-else>
    <div class="ad-head">
      <div class="ad-head-left">
        <NButton size="small" quaternary :icon="ico('ArrowBackOutline', 16)" @click="router.push('/apps')">返回列表</NButton>
        <h2 class="ad-title cp-prose" :title="app.name">{{ app.name }}</h2>
        <CpStatusTag :status="appStatus.status" :text="appStatus.text" class="ad-status" />
      </div>
      <div v-if="isAdmin" class="ad-head-right">
        <NButton size="small" type="primary" round class="cp-press" :loading="acting === 'deploy'" :disabled="!!acting" :icon="ico('CloudUploadOutline')" @click="deploy">立即部署</NButton>
        <NButton v-if="app.repo_url" size="small" tertiary round :loading="acting === 'sync'" :disabled="!!acting || app.deploying" :icon="ico('CloudDownloadOutline')" title="拉取远端最新代码并重启，不重装依赖、不改 unit/反代" @click="sync">一键同步</NButton>
        <NPopconfirm v-if="app.running" @positive-click="act('stop')">
          <template #trigger>
            <NButton size="small" type="warning" round :loading="acting === 'stop'" :disabled="!!acting" :icon="ico('StopOutline')">停止</NButton>
          </template>
          此操作将停止，是否继续？
        </NPopconfirm>
        <NButton v-else size="small" type="success" round :disabled="!!acting || app.unitMissing" :loading="acting === 'start'" :title="app.unitMissing ? '本机没有这个 systemd 单元，启动不会成功，请先立即部署' : ''" :icon="ico('PlayOutline')" @click="act('start')">启动</NButton>
        <NPopconfirm @positive-click="act('restart')">
          <template #trigger>
            <NButton size="small" tertiary round :disabled="!app.running || !!acting" :loading="acting === 'restart'" :icon="ico('SyncOutline')">重启</NButton>
          </template>
          此操作将重启，是否继续？
        </NPopconfirm>
        <NButton size="small" tertiary round type="error" :icon="ico('TrashOutline')" title="彻底删除应用与文件" @click="purge">彻底删除</NButton>
      </div>
    </div>
    <p class="ad-sub">
      {{ app.unit }}
      <span v-if="app.suggestUnit" class="ad-missing">
        本机没有 {{ app.unit }}，但有一个同名 unit {{ app.suggestUnit }} 在 systemd 里——这个应用大概率本来就是外部 unit 在服务，
        只是没登记纳管。请在下方「配置」页的「已有 unit」里填 {{ app.suggestUnit }} 并保存；
        不要点「立即部署」：那会新建 {{ app.unit }} 去抢同一个端口，把正在服务的进程挤掉。
      </span>
      <span v-else-if="app.unitMissing" class="ad-missing">本机 systemd 里没有这个单元，服务从未部署成功或已被改名；「启动/重启」都无效，请重新立即部署</span>
    </p>

    <NGrid cols="1 m:2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive style="margin-bottom: var(--space-4)">
      <NGridItem span="1">
        <NCard size="small" class="cp-rise" style="--i: 0">
          <template #header><span class="cp-chart-title">基本信息</span></template>
          <DescriptionsCard :items="infoItems" @open-slot="onDescSlot" />
        </NCard>
      </NGridItem>
      <NGridItem span="1">
        <NCard size="small" class="cp-rise" style="--i: 1">
          <template #header>
            <div class="res-head">
              <span class="cp-chart-title">运行状态</span>
              <NButton size="small" tertiary round :icon="ico('RefreshOutline')" @click="loadProc">刷新</NButton>
            </div>
          </template>
          <DescriptionsCard v-if="resItems.length" :items="resItems" />
          <EmptyBox v-else :text="procErr || '未获取到进程信息（systemd 未返回该 unit 的读数）'" />
          <div v-if="procErr && resItems.length" class="cp-help">最近一次获取失败：{{ procErr }}——上面是上一轮取到的读数</div>
        </NCard>
      </NGridItem>
    </NGrid>

    <NCard v-if="app.components?.length" size="small" class="comp-bar" style="margin-bottom: var(--space-4)">
      <NSpace align="center" :size="10" wrap>
        <NIcon :component="icons.LayersOutline" :size="15" color="var(--cp-brand-soft)" />
        <NText depth="3" style="font-size: var(--fs-xs)">组合项目组件</NText>
        <NButton
          v-for="c in app.components" :key="c.id" size="tiny"
          :type="c.id === id ? 'primary' : 'default'" :tertiary="c.id !== id"
          @click="c.id === id ? null : router.push(`/apps/${c.id}`)"
        >
          {{ c.component_role === 'frontend' ? '前端' : c.component_role === 'backend' ? '后端' : c.component_role || '组件' }} · {{ c.name }}
        </NButton>
        <NText depth="3" style="font-size: var(--fs-2xs)">启停与部署各自独立，nginx/域名按组件分别配置</NText>
      </NSpace>
    </NCard>

    <PageTabBar :tabs="panes" :value="tab" @update="(k: string) => (tab = k)" />

    <NCard size="small" class="cp-rise" style="--i: 2; margin-top: 10px">
      <div v-if="tab === 'config'">
        <NForm label-placement="left" label-width="100" style="max-width: 660px">
          <NSpace vertical :size="12">
            <!-- 只读账号：字段保留可读（read-only 不减对比度），写入口不放进来 -->
            <NFormItem label="名称"><NInput v-model:value="edit.name" :readonly="!isAdmin" :input-props="{ 'aria-label': '应用名称' }" /></NFormItem>
            <NFormItem label="安装目录"><NInput v-model:value="edit.path" :readonly="!isAdmin" placeholder="/root/www/<名称>" :input-props="{ 'aria-label': '安装目录' }" /></NFormItem>
            <NFormItem label="Git 仓库"><NInput v-model:value="edit.repo_url" :readonly="!isAdmin" :input-props="{ 'aria-label': 'Git 仓库地址' }" /></NFormItem>
            <NFormItem label="分支"><NInput v-model:value="edit.branch" :readonly="!isAdmin" :input-props="{ 'aria-label': '分支' }" /></NFormItem>
            <NFormItem label="安装命令"><NInput v-model:value="edit.install_cmd" :readonly="!isAdmin" :input-props="{ 'aria-label': '安装命令' }" /></NFormItem>
            <NFormItem label="启动命令"><NInput v-model:value="edit.start_cmd" :readonly="!isAdmin" :input-props="{ 'aria-label': '启动命令', class: 'mono' }" /></NFormItem>
            <NFormItem label="端口"><NInput v-model:value="edit.port" :readonly="!isAdmin" placeholder="留空表示无端口" :input-props="{ 'aria-label': '监听端口' }" /></NFormItem>
            <NFormItem label="域名"><NInput v-model:value="edit.domain" :readonly="!isAdmin" :input-props="{ 'aria-label': '域名' }" /></NFormItem>
            <!-- 新建表单里有这一栏，详情页原来却没有：unit_override 只在新建时能填，
                 之后再也改不动（保存载荷带着它，输入框却不存在）。于是一个把外部服务登记成
                 面板托管的应用，列表会永远红着「单元不存在」，而唯一能改的那个入口不在界面上。 -->
            <NFormItem label="已有 unit">
              <NInput v-model:value="edit.unit_override" :readonly="!isAdmin" placeholder="纳管用：如 rosetta-backend.service，填了则不覆写 unit" :input-props="{ 'aria-label': '已有 systemd unit', class: 'mono' }" />
              <template #feedback>
                <span v-if="app.suggestUnit" class="ad-missing">
                  面板找的是 {{ app.unit }}，本机没有；但有一个同名 unit {{ app.suggestUnit }}。
                  <NButton v-if="isAdmin" text type="primary" size="tiny" @click="edit.unit_override = app.suggestUnit">填入 {{ app.suggestUnit }}</NButton>
                  填这里就是告诉面板「这个应用由那个外部 unit 服务」，状态、日志与启停会跟着它走；
                  留空则表示交给面板托管 panel-{{ app.name }}.service。服务正在运行时保存会重启一次生效。
                </span>
                <span v-else>外部 unit 纳管用：填了面板就不再生成 panel-{{ app.name }}.service，只写环境变量与 drop-in。</span>
              </template>
            </NFormItem>
            <NFormItem label="关联数据库">
              <NSelect v-model:value="dbSel" :disabled="!isAdmin" multiple filterable tag :options="pgOptions" placeholder="选择该应用使用的 PostgreSQL 数据库，便于项目管理与备份联动" aria-label="关联数据库" style="width: 100%" />
              <template #feedback>
                <span v-if="pgErr">数据库清单没取到（{{ pgErr }}）——下拉可能是空的，仍可直接输入库名后回车</span>
              </template>
            </NFormItem>
            <NButton v-if="isAdmin" type="primary" size="small" round :loading="saving" :disabled="saving" :icon="ico('SaveOutline')" @click="save">保存配置</NButton>
          </NSpace>
        </NForm>
      </div>

      <div v-else-if="tab === 'env'">
        <NSpace vertical :size="12" style="width: 100%">
          <div class="cp-help">
            <template v-if="app.unit_override">
              该应用由外部 unit 纳管：面板不改写 unit 本体，环境变量写入安装目录的
              .panel.env，并由面板在 systemd 的 {{ app.unit_override }}.d/ 下补一份 drop-in 引用它（删掉那个文件就恢复原样）。
              服务正在运行时保存会重启一次生效；启动命令、端口、域名仍要在外部 unit 里自己维护。
            </template>
            <template v-else>
              写入应用目录的 .panel.env 并由 systemd EnvironmentFile 加载。服务正在运行时，保存会自动同步 unit 并重启生效；停止状态则下次启动时生效。
            </template>
          </div>
          <!-- 1Panel 的参数抽屉是「只读详情 / 编辑」互斥切换：默认只读，敏感值先遮罩 -->
          <NButton v-if="isAdmin" size="small" tertiary round :icon="ico(envView === 'edit' ? 'EyeOutline' : 'PencilOutline')" style="align-self: flex-start" @click="envView = envView === 'edit' ? 'detail' : 'edit'">
            {{ envView === 'edit' ? '详情' : '编辑' }}
          </NButton>
          <template v-if="envView === 'detail'">
            <DescriptionsCard v-if="envItems.length" :items="envItems" label-width="34%" />
            <EmptyBox v-else text="该应用还没有环境变量" />
          </template>
          <template v-else>
            <NDynamicInput v-model:value="env" :on-create="() => ({ k: '', v: '' })">
              <template #default="{ value }">
                <!-- KEY 写死 220px：375px 屏上 220+8+两枚按钮(~64) 之后，
                     flex:1 的值栏只剩 ~30px，环境变量根本没法编辑。 -->
                <NInput v-model:value="value.k" placeholder="KEY" style="width: clamp(110px, 38%, 220px); margin-right: var(--space-2)" :input-props="{ class: 'mono', 'aria-label': '环境变量名' }" />
                <NInput v-model:value="value.v" placeholder="value" style="flex: 1" :input-props="{ class: 'mono', 'aria-label': '环境变量值' }" />
              </template>
              <!-- 默认的行内按钮是两块无名的圆形图标按钮：读屏只念得出「按钮」，
                   键盘用户更分不清哪个是删掉这一行、哪个是再加一行。自己接管这一格。 -->
              <template #action="{ index, create, remove }">
                <NButton size="tiny" circle :icon="ico('TrashOutline', 12)" aria-label="删除这一项环境变量" @click="remove(index)" />
                <NButton size="tiny" circle :icon="ico('AddOutline', 12)" aria-label="新增一项环境变量" style="margin-left: var(--space-1)" @click="create()" />
              </template>
            </NDynamicInput>
            <NButton size="small" type="primary" round :loading="saving" :disabled="saving" :icon="ico('SaveOutline')" style="align-self: flex-start" @click="save">保存环境变量</NButton>
          </template>
        </NSpace>
      </div>

      <div v-else-if="tab === 'deploy'">
        <div class="split-view">
          <div class="dep-list">
            <div v-for="d in deps" :key="d.id" class="dep-item" :class="{ active: depLog?.id === d.id }"
                 role="button" tabindex="0" :aria-label="`查看部署记录 #${d.id}`"
                 @click="viewDep(d.id)" @keyup.enter="viewDep(d.id)" @keyup.space.prevent="viewDep(d.id)">
              <NSpace align="center" justify="space-between" style="width: 100%">
                <NText depth="3" class="cp-break" style="font-size: var(--fs-xs)">#{{ d.id }} · {{ d.started_at }}</NText>
                <CpStatusTag :status="d.status === 'running' ? 'installing' : d.status" :text="d.status" />
              </NSpace>
            </div>
            <EmptyBox v-if="!deps.length" text="暂无部署记录" />
            <NButton size="small" tertiary round :icon="ico('RefreshOutline')" style="margin-top: var(--space-2)" @click="pollDeps">刷新</NButton>
          </div>
          <div class="log-view dep-log" :class="{ 'dep-log-empty': !depLog }">{{ depLog?.log || '点击左侧记录查看部署日志' }}</div>
        </div>
      </div>

      <div v-else-if="tab === 'logs'">
        <LogStream :key="app.id" :url-path="`/apps/${app.id}/logs`" />
      </div>

      <div v-else-if="tab === 'nginx'">
        <div v-if="!ngx?.configs?.length" style="padding: var(--space-5) 0">
          <EmptyBox text="未找到与该应用关联的 nginx 配置（按域名或反代端口匹配）。配置域名并创建反代后，这里可直接快捷编辑" />
        </div>
        <div v-else class="split-view">
          <div class="ngx-list">
            <div
              v-for="c in ngx.configs" :key="c.file" class="ngx-item" :class="{ active: ngxFile?.file === c.file }"
              role="button" tabindex="0" :aria-label="`查看 nginx 配置 ${c.name}`"
              @click="selConfig(c)" @keyup.enter="selConfig(c)" @keyup.space.prevent="selConfig(c)"
            >
              <NIcon :component="icons.GlobeOutline" :size="15" :color="c.analysis.ssl ? 'var(--cp-ok)' : 'var(--cp-text-mute)'" />
              <div style="min-width: 0">
                <div class="ngx-name cp-prose" :title="c.name">{{ c.name }}</div>
                <!-- 域名多于两个时原先一刀砍掉，看起来就像「这份配置只管这两个域名」；
                     计数补齐 +N，完整清单挂在 title 上，点进来右侧就是这份配置的全文。 -->
                <div class="ngx-dom cp-prose" :title="c.analysis.serverNames.join(', ') || '（无 server_name）'">{{ c.analysis.serverNames.slice(0, 2).join(', ') || '（无 server_name）' }}<template v-if="c.analysis.serverNames.length > 2"> +{{ c.analysis.serverNames.length - 2 }}</template></div>
              </div>
            </div>
            <div v-if="ngxFile" style="margin-top: var(--space-3); display: flex; flex-wrap: wrap; gap: var(--space-2)">
              <NTag size="tiny" :bordered="false" :type="ngxFile.analysis.ssl ? 'success' : 'default'">{{ ngxFile.analysis.ssl ? 'HTTPS' : '仅 HTTP' }}</NTag>
              <NTag size="tiny" :bordered="false" :type="ngxFile.analysis.httpsRedirect ? 'success' : 'default'">301 跳转 {{ ngxFile.analysis.httpsRedirect ? '✓' : '✗' }}</NTag>
              <NTag size="tiny" :bordered="false" :type="ngxFile.analysis.websocket ? 'success' : 'default'">WebSocket {{ ngxFile.analysis.websocket ? '✓' : '✗' }}</NTag>
              <NTag size="tiny" :bordered="false">body {{ ngxFile.analysis.bodySize || '默认 1m' }}</NTag>
            </div>
            <NText v-if="ngxFile" depth="3" style="font-size: var(--fs-2xs); display: block; margin-top: var(--space-2); word-break: break-all">{{ ngxFile.file }}</NText>
          </div>
          <div style="flex: 1; min-width: 0">
            <NSpace v-if="isAdmin" size="small" style="margin-bottom: var(--space-3)" wrap>
              <NButton size="small" tertiary round :disabled="!ngxFile || ngxFile.analysis.websocket || ngxSaving" :loading="ngxSaving" :icon="ico('PulseOutline')" @click="quick('ws')">添加 WebSocket 支持</NButton>
              <NInput v-model:value="bodySizeVal" size="small" style="width: 80px; max-width: 100%" placeholder="50m" :input-props="{ 'aria-label': '上传大小限制' }" />
              <NButton size="small" tertiary round :disabled="!ngxFile || ngxSaving" :loading="ngxSaving" :icon="ico('DownloadOutline')" @click="quick('body')">设置上传限制</NButton>
              <NButton size="small" tertiary round :disabled="!ngxFile || !ngxFile.analysis.ssl || ngxFile.analysis.httpsRedirect || ngxSaving" :loading="ngxSaving" :icon="ico('ShieldCheckmarkOutline')" @click="quick('redirect')">强制 HTTPS 跳转</NButton>
            </NSpace>
            <EmptyBox v-if="!ngxFile.content" text="完整配置文本仅管理员可见：这里保留域名、HTTPS、WebSocket 与上传限制的分析结论" />
            <NInput v-else v-model:value="ngxContent" type="textarea" :readonly="!isAdmin || ngxSaving" :autosize="{ minRows: 14, maxRows: 26 }" :input-props="{ class: 'mono', 'aria-label': 'nginx 配置内容', style: 'font-size: var(--fs-xs)' }" />
            <NSpace justify="space-between" align="center" style="margin-top: var(--space-3)">
              <div class="cp-help">{{ isAdmin ? '保存将执行 nginx -t 校验，失败自动回滚并 reload 生效' : '只读账号可查看分析结论，配置全文、保存与快捷项由管理员操作' }}</div>
              <NButton v-if="isAdmin" type="primary" size="small" round class="cp-press" :loading="ngxSaving" :disabled="ngxSaving" :icon="ico('SaveOutline')" @click="saveNginx">保存配置</NButton>
            </NSpace>
          </div>
        </div>
      </div>

      <div v-else-if="tab === 'unit'">
        <NSpace vertical :size="12" style="width: 100%">
          <NAlert v-if="!isAdmin" type="info" :bordered="false" size="small">
            unit 文件里带着 Environment= 环境变量与本机路径，全文仅管理员可查看。只读账号仍可在「基本配置」看运行状态、在「运行日志」看服务输出。
          </NAlert>
          <template v-else>
            <NAlert v-if="unitInfo && !unitInfo.managed" type="warning" :bordered="false" size="small">
              该应用为纳管模式（unit {{ app.unit }} 由外部管理），面板只读展示，不会写入该文件。
            </NAlert>
            <div class="cp-help">
              文件：<span class="mono-dim cp-break">{{ unitInfo?.path }}</span>。保存流程：写入 → systemd-analyze verify 校验 → daemon-reload，任一步失败自动回滚。修改后需重启服务才生效；面板生成模式下保存会同步更新部署模板，后续部署按此模板渲染。占位符 <code v-pre>{{name}} {{path}} {{start_cmd}} {{port}}</code> 可在模板中使用。
            </div>
            <NInput
              v-model:value="unitContent"
              type="textarea"
              :autosize="{ minRows: 14, maxRows: 26 }"
              :readonly="!!unitInfo && !unitInfo.managed"
              :input-props="{ class: 'mono', 'aria-label': 'systemd unit 内容', style: 'font-size: var(--fs-xs)' }"
            />
            <NSpace justify="space-between" align="center">
              <NSpace size="small">
                <NButton size="small" tertiary round :icon="ico('RefreshOutline')" @click="loadUnit">重新读取</NButton>
                <NButton v-if="unitInfo?.managed" size="small" tertiary round @click="restoreDefaultTpl">恢复面板默认模板</NButton>
                <NButton v-if="unitInfo?.managed" size="small" tertiary round type="warning" :loading="acting === 'restart'" :disabled="!!acting" :icon="ico('SyncOutline')" @click="act('restart')">保存后重启服务</NButton>
              </NSpace>
              <NButton type="primary" size="small" round :loading="unitSaving" :disabled="!unitInfo?.managed || unitSaving" :icon="ico('SaveOutline')" @click="saveUnitFile">保存 Unit</NButton>
            </NSpace>
          </template>
        </NSpace>
      </div>

      <div v-else-if="tab === 'ssl'">
        <NSpace vertical :size="16">
          <NSpace align="center" :size="8">
            <NIcon :component="icons.ShieldCheckmarkOutline" :size="20" color="var(--cp-brand-soft)" />
            <NText style="font-size: var(--fs-sm)">{{ app.domain || '（未配置域名）' }}</NText>
            <NTag v-if="app.domain" size="tiny" :bordered="false" type="info">Let's Encrypt</NTag>
          </NSpace>
          <div class="cp-help">要求该域名 DNS A 记录已指向本机 IP。申请成功后自动切换为 HTTPS 反代并开启 80 → 443 跳转。</div>
          <NSpace>
            <NInput v-model:value="sslEmail" :disabled="!isAdmin" placeholder="邮箱（可选，用于到期通知）" style="width: 300px; max-width: 100%" :input-props="{ 'aria-label': 'SSL 到期通知邮箱' }" />
            <NButton v-if="isAdmin" type="primary" round :disabled="!app.domain || acting === 'ssl'" :loading="acting === 'ssl'" :icon="ico('KeyOutline')" @click="ssl">申请 / 配置证书</NButton>
          </NSpace>
        </NSpace>
      </div>
    </NCard>

    <ConfirmActionDialog ref="opRef" />
  </div>
</template>

<style scoped>
.page-loading { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-5) 0; }
/* 1Panel 带返回的标题行：左组（返回 + 名称 + 状态）gap 8，右组（操作）gap 12 */
.ad-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); flex-wrap: wrap; min-width: 0; }
.ad-head-left { display: flex; align-items: center; gap: var(--space-2); min-width: 0; }
.ad-head-right { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; justify-content: flex-end; }
/* 长应用名交给全局 .cp-prose（三行封顶）：原先 nowrap + ellipsis，而应用名常带模板后缀与
   环境名（"ocr-api-staging"），手机上没有 hover，title 那条退路等于不存在，半个名字无法对应
   到具体应用。这里只保留 max-width —— 它防的是右侧操作按钮被标题挤出屏幕，窄屏下靠 .ad-head
   的 flex-wrap 换行；折行方式不能再在这里写一遍，组件 scoped 样式排在 style.css 之后、
   同特异度时会把 .cp-prose 的声明按回去。 */
.ad-title {
  margin: 0; font-size: var(--fs-title); font-weight: 500; color: var(--cp-text-strong);
  min-width: 0; max-width: clamp(140px, 22vw, 320px);
}
.ad-status { margin-left: var(--space-1); flex-shrink: 0; }
.ad-sub { margin: var(--space-1) 0 var(--space-4); font-size: var(--fs-xs); color: var(--cp-text-mute); overflow-wrap: anywhere; }
.ad-missing { color: var(--cp-warn); }
.res-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); min-width: 0; }
.split-view { display: flex; align-items: flex-start; gap: var(--space-3); }
.dep-list { width: 300px; flex-shrink: 0; border-right: 1px solid var(--cp-border); padding-right: var(--space-3); }
.dep-item { padding: var(--space-3); border-radius: var(--radius); cursor: pointer; transition: background var(--dur-fast) var(--ease); }
/* 部署记录行是 space-between 的两段文字：时间戳比列宽长时先收缩自己，
   不然右侧状态标签会被挤出 300px 的列表外（裁半截的老症状）。
   收缩保留、折行交给文本上的 .cp-break：这条 :deep 的权重压过全局类，
   所以 nowrap 必须从这里删干净，否则「#7 · 2026-10-11T09:」这种半截时间戳
   既读不全也没有 title 可看。 */
.dep-item :deep(.n-space) { min-width: 0; }
.dep-item :deep(.n-space > *:first-child) { min-width: 0; }
.dep-item :deep(.cp-status) { flex-shrink: 0; }
.dep-item:hover { background: var(--cp-hover); }
.dep-item.active { background: var(--cp-selected); }
.dep-log { flex: 1; min-width: 0; min-height: 420px; max-height: calc(var(--viewport-h) - 320px); box-shadow: var(--shadow-log-inset); }
.dep-log-empty { color: var(--cp-text-mute); }
.ngx-list { width: 260px; flex-shrink: 0; border-right: 1px solid var(--cp-border); padding-right: var(--space-3); }
.ngx-item { display: flex; align-items: center; gap: var(--space-2); padding: var(--space-2) var(--space-3); border-radius: var(--radius); cursor: pointer; transition: background var(--dur-fast) var(--ease); }
.ngx-item:hover { background: var(--cp-hover); }
.ngx-item.active { background: var(--cp-selected); }
.ngx-item > div { min-width: 0; }
/* nginx 列表的两行文字（配置名 + server_name）在 260px 侧栏里，原先各裁一行、且没有 title：
   一个 vhost 挂两个域名时第二个永远看不见，而这一列正是"哪个配置文件管哪个域名"的判据。
   折行方式统一交 .cp-prose（三行封顶），本地只留字号与颜色。 */
.ngx-name { font-size: var(--fs-xs); color: var(--cp-text); font-weight: 550; }
.ngx-dom { font-size: var(--fs-2xs); color: var(--cp-text-mute); }
@media (max-width: 900px) {
  .split-view { flex-direction: column; }
  .dep-list, .ngx-list { width: 100%; border-right: 0; padding-right: 0; margin-bottom: var(--space-2); }
  .ad-head-right { justify-content: flex-start; }
  .dep-log { min-height: 200px; max-height: 50vh; }
}
</style>
