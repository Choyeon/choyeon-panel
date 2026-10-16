<script setup lang="ts">
import { computed, onMounted, ref, watch, h } from 'vue';
import { useRouter } from 'vue-router';
import {
  NDataTable, NSpace, NTag, NDrawer, NDrawerContent, NForm, NFormItem, NInputNumber,
  NSelect, useMessage, NIcon, NAlert, NSwitch, NInput,
} from 'naive-ui';
import { NButton, useDrawerWidth } from '../ui';
import { api, getRole } from '../api';
import { icon, icons } from '../icons';
import CpToolbarCard from '../components/CpToolbarCard.vue';
import CpRowActions from '../components/CpRowActions.vue';
import ConfirmActionDialog from '../components/ConfirmActionDialog.vue';
import EmptyBox from '../components/EmptyBox.vue';
import InfoList from '../components/InfoList.vue';
import { useCachedFetch, invalidateCache } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';
import { fmtBytes, fmtDateTime } from '../composables/useFormat';
import { useVisiblePoll } from '../composables/useVisiblePoll';

const msg = useMessage();
const router = useRouter();
// 备份任务的增删改与立即执行都是 MUTATION，服务端统一拦 403：只读账号只看计数
const isAdmin = getRole() === 'admin';
const show = ref(false);
const drawerW = useDrawerWidth(0.5);
const busy = ref<number | null>(null);
const toggling = ref<number | null>(null);
const submitting = ref(false);
const editingId = ref<number | null>(null);
const form = ref<any>({ kind: 'pg', target: null, schedule: 'daily', hour: 3, minute: 30, keep: 7, enabled: true });
// pg 的 target 是库名，不预填任何值：这里曾经硬编码过一个别的项目的库名，
// 换一台机器建任务就会带着它提交——pg 的库名只校验格式、不校验存在性，
// 于是任务照样创建成功，每晚 pg_dump 在 runner 里失败（与批次⑤O 同一类缺陷）。
const pgDbs = ref<any[]>([]);

// 异地备份目标。`GET /api/backups/targets` 是 need_admin，所以这一整块只在 admin 下
// 取数与渲染；viewer 那侧改用列表接口给的 `cloud_linked` 布尔位回答同一个问题
// （"这条任务有没有异地副本"），不必知道副本落在哪个桶。
const tgShow = ref(false);
const tgDrawerW = useDrawerWidth(0.5);
const tgSubmitting = ref(false);
const tgEditingId = ref<number | null>(null);
const tgToggling = ref<number | null>(null);
const tgTesting = ref<number | null>(null);
const pushing = ref<number | null>(null);
const probeShow = ref(false);
const probeItems = ref<any[]>([]);
const probeTitle = ref('');
function blankTarget() {
  return {
    name: '', endpoint: '', bucket: '', region: 'us-east-1', prefix: '',
    access_key: '', secret_key: '', path_style: true, enabled: true,
  };
}
const tgForm = ref<any>(blankTarget());

// 执行日志抽屉：立即备份的返回体带 log，失败也要能看输出
const logShow = ref(false);
const logText = ref('');
const logTitle = ref('');

// 备份任务表几乎不变（只有改计划/删除会变），30s 缓存让这一页秒开；
// 应用列表只用于下拉建议与目标名展示，缓存 60s。
const { data, error, loading, refreshing, refresh } = useCachedFetch<any[]>({
  key: () => 'backups',
  loader: () => api.backups(),
  maxAge: 30000,
});
const { data: appsData, error: appsError, load: loadApps } = useCachedFetch<any[]>({
  key: () => 'apps',
  loader: () => api.apps(),
  maxAge: 60000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });
// 这一份失败只影响下拉建议与目标名展示，不拦任何操作，所以是 warning 不是 error；
// 但不说就成了另一回事：下拉空着，人只会以为这台机器上没有应用。
watch(appsError, (e) => { if (e) msg.warning(`应用列表没取到，新建备份任务的下拉不会给建议：${(e as Error).message || String(e)}`); });
// 原实现在 onMounted 里吞掉应用列表的错误（列表只影响下拉建议），保持一致
void loadApps().catch(() => undefined);

// 目标列表要 15s 自动刷新：这一页同时是"最后一推成功没有"的看板，
// 而 last_push_at / last_error 是 runner 在备份进程里写的，面板进程不参与，
// 不刷新就只会停在用户上一次点刷新的那一刻。
const {
  data: tgData, error: tgError, loading: tgLoading, refreshing: tgRefreshing,
  refresh: refreshTg, load: loadTg,
} = useCachedFetch<any[]>({
  key: () => 'backups:targets',
  loader: () => api.cloudTargets(),
  maxAge: 15000,
});
watch(tgError, (e) => { if (e) msg.error(`异地目标列表获取失败：${(e as Error).message || String(e)}`); });
// viewer 调这个接口只会拿到 403，所以一次都不发
if (isAdmin) void loadTg().catch(() => undefined);

const rows = computed<any[]>(() => data.value ?? []);
const apps = computed<any[]>(() => appsData.value ?? []);
const tgList = computed<any[]>(() => tgData.value ?? []);
// 任务行要展示"挂在哪个目标、推了几份、最后一次推成功是什么时候"，这三件事的出处
// 都在目标表里；列表接口只给 cloud_target_name，所以这里按 id 关联一次。
// viewer 侧 tgList 恒为空（接口 need_admin），那一格走 cloud_linked 分支，不会走到这里。
const tgById = computed<Record<number, any>>(() => {
  const m: Record<number, any> = {};
  for (const t of tgList.value) m[t.id] = t;
  return m;
});

const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

function reload() {
  invalidateCache('backups');
  // 立即备份/删除任务都会写审计，审计页的缓存一起作废
  invalidateCache('audit:');
  // 手动备份会把异地推送的结果写进 cloud_targets（last_push_at / last_error），
  // 目标表必须同一周期一起取，否则"推送失败"只出现在任务侧、看板还是绿的。
  if (!isAdmin) return refresh().catch(() => undefined);
  invalidateCache('backups:targets');
  return Promise.all([refresh().catch(() => undefined), refreshTg().catch(() => undefined)]);
}

// 1Panel TableSetting 的自动刷新：0/5/10/30/60/120/300s，localStorage 记忆
const AUTO_OPTS = [0, 5, 10, 30, 60, 120, 300].map((s) => ({ label: s ? `${s} 秒` : '关闭自动刷新', value: s }));
const autoSec = ref(Math.max(0, Number(localStorage.getItem('backups-auto-refresh')) || 0));
const autoPoll = useVisiblePoll(() => void reload(), autoSec.value * 1000);
watch(autoSec, (v) => { localStorage.setItem('backups-auto-refresh', String(v)); autoPoll.every(v * 1000); });
onMounted(autoPoll.start);

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
// pg 目标下拉：真实库名 + all。列表拿不到（未装 PG / 连接失败）时留空，
// 选择器仍可键入，不把这条路径堵死。
const pgOpts = computed<any[]>(() => [
  ...pgDbs.value.map((d: any) => ({ label: `${d.name}（${d.size || '大小未知'}）`, value: d.name })),
  { label: 'all（整库 pg_dumpall，只能手工恢复）', value: 'all' },
]);
async function loadPg() {
  try {
    pgDbs.value = await api.pgDbs();
  } catch {
    pgDbs.value = [];
  }
}

const columns: any[] = [
  {
    // 弹性列必须给 minWidth：scroll-x auto 下没有它，固定列一超过容器就被挤成 0 宽（375px 实测名字看不见）
    title: '任务', key: 'target', minWidth: 200,
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: icons[KIND_ICON[r.kind] || 'FolderOutline'], size: 18, color: r.kind === 'pg' ? 'var(--cp-brand-soft)' : 'var(--cp-node)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name', title: r.kind === 'sqlite' ? sqliteTargetName(r.target) : r.target }, r.kind === 'sqlite' ? sqliteTargetName(r.target) : r.target),
          r.target_missing
            ? h(NTag, { size: 'tiny', type: 'warning', bordered: false, class: 'cell-sub' }, () => '目标应用已删除，备份将失败')
            : null,
        ]),
      ]),
  },
  {
    // 1Panel Status operate 模式：启用/停用是可点的小圆按钮，点击直发 updateBackup。
    // 以前这颗开关和"每日 03:00"挤在同一列、列名还和定时任务页的「计划」（cron 表达式）
    // 同名不同义，两页来回切的时候读不出这是哪一类信息。
    title: '启用', key: 'enabled', width: 90,
    render: (r: any) =>
      r.schedule === 'manual'
        ? h('span', { class: 'cell-sub' }, '—')
        : isAdmin
          ? h(NButton, {
              size: 'tiny', round: true, tertiary: true, type: r.enabled ? 'success' : 'info',
              loading: toggling.value === r.id, disabled: toggling.value !== null,
              onClick: () => toggleEnabled(r),
            }, () => (r.enabled ? '启用' : '停用'))
          // 只读账号不给按不动的开关，直接只显示状态文字
          : h(NTag, { size: 'small', round: true, bordered: false, type: r.enabled ? 'success' : 'default' }, () => (r.enabled ? '启用' : '停用')),
  },
  {
    title: '调度', key: 'schedule', width: 130,
    render: (r: any) =>
      r.schedule === 'manual'
        ? h(NTag, { size: 'small', round: true, bordered: false }, () => '手动')
        : h('span', { class: 'mono-dim' }, `${r.schedule === 'daily' ? '每日' : '每周一'} ${String(r.hour).padStart(2, '0')}:${String(r.minute).padStart(2, '0')}`),
  },
  { title: '保留', key: 'keep', width: 70, render: (r: any) => h('span', { class: 'mono-dim' }, `${r.keep} 份`) },
  {
    title: '产物', key: 'files', width: 250,
    render: (r: any) => {
      if (!r.files.length) return h('span', { class: 'cell-sub' }, '—');
      // 这一行是一句带空格的话，不是一个词：375px 实测内容 249px、格子只有 226px，
      // 而 .cell-sub 自带 nowrap+ellipsis，于是最近一次备份的时间被切成「合计 188.6 K… UTC」
      // 这种谁都读不出来的形状。列上没有第二个入口（原来连 title 都没写），所以直接折行。
      const sub = `合计 ${fmtBytes(r.files.reduce((n: number, f: any) => n + (f.size || 0), 0))} · 最近 ${fmtDateTime(r.files[0].mtime)} UTC`;
      return h('div', { class: 'cell-txt' }, [
        h(NButton, { text: true, size: 'small', type: 'primary', class: 'cp-op', onClick: () => router.push({ path: '/backups/files', query: { job: String(r.id) } }) }, () => `${r.files.length} 份`),
        h('div', { class: 'cell-sub cp-prose', title: sub }, sub),
      ]);
    },
  },
  {
    // 「这条任务有没有异地副本」是这一页最要紧的问题之一，而 admin 与 viewer 拿到的是
    // 两个不同的字段：列表接口给 viewer 只留 cloud_linked 布尔位（副本落在哪个桶属于
    // "在哪"，只读账号不需要知道），给 admin 才带目标名。所以这里必须按角色分支，
    // 不能假设 cloud_target_name 一定在——那会让 viewer 那一格永远显示"目标已删除"。
    title: '异地', key: 'cloud', width: 170,
    render: (r: any) => {
      if (!isAdmin) {
        return r.cloud_linked
          ? h(NTag, { size: 'small', round: true, bordered: false, type: 'success' }, () => '已配异地')
          : h('span', { class: 'cell-sub cp-prose', title: '只在本机留副本' }, '—');
      }
      if (!r.cloud_target_id) {
        return h('span', { class: 'cell-sub cp-prose', title: '只在本机留副本，不推异地' }, '—');
      }
      const t = tgById.value[r.cloud_target_id];
      if (!t) {
        // 链接还在、目标没了：runner 每次备份都会为它跑一次注定失败的推送，
        // 本机归档照样成功，所以这一格不能只显示"—"让人以为没配异地。
        return h(NTag, { size: 'tiny', type: 'warning', bordered: false, class: 'cell-sub cp-prose', title: '关联的异地目标已被删除，本机备份照常，但每次推送都会失败' }, () => '目标已删除');
      }
      const sub = t.pushed_count
        ? `已推 ${t.pushed_count} 份 · 最近 ${t.last_push_at ? fmtDateTime(t.last_push_at) : '未知'}`
        : '还没有推上去的产物';
      return h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name cp-prose', title: `${t.name} · ${t.bucket}` }, t.name),
        h(NTag, {
          size: 'tiny', round: true, bordered: false, type: t.enabled ? 'success' : 'warning',
        }, () => (t.enabled ? '已启用' : '已停用')),
        h('div', { class: 'cell-sub cp-prose', title: sub }, sub),
      ]);
    },
  },
  {
    title: '操作', key: 'ops', width: 230, align: 'center', fixed: 'right' as const,
    render: (r: any) =>
      h(CpRowActions, {
        ellipsis: 3,
        buttons: [
          ...(isAdmin
            ? [{ label: busy.value === r.id ? '备份中…' : '立即备份', run: () => run(r.id), disabled: busy.value !== null }]
            : []),
          { label: '备份文件', run: () => router.push({ path: '/backups/files', query: { job: String(r.id) } }), disabled: busy.value === r.id || !r.files.length },
          ...(isAdmin && r.cloud_target_id
            ? [{ label: pushing.value === r.id ? '推送中…' : '补推异地', run: () => pushNow(r), disabled: pushing.value !== null || busy.value !== null || !r.files.length }]
            : []),
          ...(isAdmin
            ? [
                { label: '编辑', run: () => openEdit(r), disabled: busy.value === r.id },
                { label: '删除', danger: true, run: () => confirmDel(r), disabled: busy.value === r.id },
              ]
            : []),
        ],
      }),
  },
];

const tgColumns: any[] = [
  {
    // 弹性列必须给 minWidth：scroll-x auto 下没有它，固定列一超过容器就被挤成 0 宽（375px 实测名字看不见）
    title: '目标', key: 'name', minWidth: 180,
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: icons.CloudOutline, size: 18, color: r.enabled ? 'var(--cp-brand-soft)' : 'var(--cp-text-dim)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name', title: r.name }, r.name),
          // 落点是「协议://主机:端口 / 桶 / 前缀」拼起来的一长串，375px 一行放不下；
          // 而它正是"副本到底推到哪儿去了"要读的内容，所以格子里给短形、title 给全串。
          h('div', { class: 'mono-dim cp-prose', title: `${r.endpoint}/${r.bucket}/${r.prefix || ''}` }, `${r.bucket} · ${r.prefix || '根前缀'}`),
        ]),
      ]),
  },
  {
    title: '启用', key: 'enabled', width: 90,
    render: (r: any) =>
      h(NButton, {
        size: 'tiny', round: true, tertiary: true, type: r.enabled ? 'success' : 'info',
        loading: tgToggling.value === r.id, disabled: tgToggling.value !== null,
        onClick: () => toggleTarget(r),
      }, () => (r.enabled ? '启用' : '停用')),
  },
  {
    // Access Key 只回显掩码，Secret Key 从不回显——但"有没有存过"必须看得见：
    // 缺 Secret 的目标点备份会一路成功到签名那一步才报错，界面上提前说清少一次白跑。
    title: '凭据', key: 'cred', width: 130,
    render: (r: any) =>
      h('div', { class: 'cell-txt' }, [
        h('span', { class: 'mono-dim cp-prose', title: r.access_key_masked }, r.access_key_masked),
        r.has_secret
          ? null
          : h(NTag, { size: 'tiny', type: 'warning', bordered: false, class: 'cell-sub cp-prose', title: '这个目标没有保存 Secret Key，签名会失败，请编辑后重新填一次' }, () => '缺 Secret'),
      ]),
  },
  {
    title: '推送', key: 'push', width: 200,
    render: (r: any) => {
      const ok = r.pushed_count
        ? `已推 ${r.pushed_count} 份 · 最近 ${r.last_push_at ? fmtDateTime(r.last_push_at) : '未知'}`
        : '还没有推上去的产物';
      return h('div', { class: 'cell-txt' }, [
        h('div', { class: 'cell-sub cp-prose', title: ok }, ok),
        r.last_error
          ? h(NTag, { size: 'tiny', type: 'error', bordered: false, class: 'cell-sub cp-prose', title: r.last_error }, () => r.last_error)
          : null,
      ]);
    },
  },
  {
    title: '关联任务', key: 'linked', width: 100,
    render: (r: any) => {
      const n = r.linked.length;
      if (!n) return h('span', { class: 'cell-sub cp-prose', title: '没有任务挂在这个目标上，删掉它不影响备份' }, '—');
      const names = r.linked.map((x: any) => `#${x.id} ${x.kind}:${x.target}`).join('、');
      return h('div', { class: 'cell-txt' }, [
        h('span', { class: 'mono-dim cp-prose', title: names }, `${n} 个`),
        n > 5 ? h('span', { class: 'cell-sub cp-prose', title: names }, '只点名前 5 个') : null,
      ]);
    },
  },
  {
    title: '操作', key: 'ops', width: 210, align: 'center', fixed: 'right' as const,
    render: (r: any) =>
      h(CpRowActions, {
        ellipsis: 3,
        buttons: [
          { label: tgTesting.value === r.id ? '测试中…' : '测试连通', run: () => testTarget(r), disabled: tgTesting.value !== null },
          { label: '编辑', run: () => openTargetEdit(r) },
          { label: '删除', danger: true, run: () => confirmDeleteTarget(r) },
        ],
      }),
  },
];

function displayName(r: any) {
  return r.kind === 'sqlite' ? sqliteTargetName(r.target) : r.target;
}

function sqliteTargetName(target: any) {
  const t = String(target || '');
  const hit = apps.value.find((a: any) => String(a.id) === t || a.name === t);
  return hit ? `${hit.name} · SQLite` : `应用 #${t} · SQLite`;
}

async function toggleEnabled(r: any) {
  if (toggling.value !== null) return;
  toggling.value = r.id;
  try {
    await api.updateBackup(r.id, { enabled: !r.enabled });
    await reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    toggling.value = null;
  }
}

async function run(id: number) {
  if (busy.value !== null) return;
  busy.value = id;
  try {
    const r = await api.runBackup(id);
    if (r.code === 0) msg.success('备份完成');
    else msg.error(`备份失败 (exit ${r.code})`);
    const job = rows.value.find((x: any) => x.id === id);
    if (r.log) {
      logText.value = r.log;
      logTitle.value = job ? displayName(job) : `任务 #${id}`;
      logShow.value = true;
    }
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}

const delDialog = ref<InstanceType<typeof ConfirmActionDialog> | null>(null);
function confirmDel(r: any) {
  delDialog.value?.acceptParams({
    title: '确认删除备份任务',
    names: [displayName(r)],
    msg: '删除任务及其 systemd timer（已生成的备份文件保留），操作不可撤销。',
    api: async () => {
      await api.deleteBackup(r.id);
      await reload();
    },
    successMsg: '已删除',
  });
}

function blankForm() {
  return { kind: 'pg', target: null, schedule: 'daily', hour: 3, minute: 30, keep: 7, enabled: true, cloud_target_id: null };
}
function openCreate() {
  editingId.value = null;
  form.value = blankForm();
  void loadPg();
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
    // 必须带旧值：后端把"字段缺省"当作不改动、把 null/0 当作解除关联，
    // 不带就是要清关联的编辑会变成"每次编辑都顺手把异地链接抹掉"。
    cloud_target_id: r.cloud_target_id ?? null,
  };
  void loadPg();
  show.value = true;
}
// 只列启用中的目标：挂到一个已停用的目标上，定时备份会一路"成功"而异地那份永远不出现
// （push_pending 把它算作 skipped），所以宁可让人在编辑时就看不见这个选项，
// 也不要给一个点了会静默失效的选择。
// 例外是"这条任务现在就停在那个目标上"：那时必须把它列出来，否则下拉会显示一个
// 光秃秃的 id，人读到的是"数据坏了"，而不是"这个目标被停用了"。
const cloudOpts = computed(() => tgList.value
  .filter((t: any) => t.enabled || t.id === form.value.cloud_target_id)
  .map((t: any) => ({
    label: t.enabled ? `${t.name} · ${t.bucket}` : `${t.name} · ${t.bucket}（已停用，备份不会推上来）`,
    value: t.id,
  })));
async function submit() {
  if (submitting.value) return;
  // 切换类型会把 target 置空（pg 的库名不是应用名），而后端 update 对 target=null
  // 的语义是"沿用旧值"：不选目标就提交，任务会静默继续指向原来那个名字，
  // 列表上还显示着新类型 —— 必须在提交前把空目标拦下来。
  if (!form.value.target) {
    msg.error(form.value.kind === 'pg' ? '请选择要备份的数据库（或 all）' : '请选择要备份的应用');
    return;
  }
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

// ---------------------------------------------------------------- 异地目标

function reloadTargets() {
  invalidateCache('backups:targets');
  // 目标与任务是一对多：停用、改名、删掉都会改变任务行上「异地」那一格的结论，
  // 两张表必须同一轮取数，否则会出现"目标已停用、任务行还写着已推 N 份"。
  invalidateCache('backups');
  invalidateCache('audit:');
  return Promise.all([refreshTg().catch(() => undefined), refresh().catch(() => undefined)]);
}

async function pushNow(r: any) {
  if (pushing.value !== null) return;
  pushing.value = r.id;
  try {
    const res = await api.pushBackup(r.id);
    if (res.skipped) msg.info(`这次没有要推的产物：${res.skipped}`);
    else msg.success(`已推送 ${res.pushed.length} 份${res.pruned && res.pruned.length ? `，并按保留份数删掉远端最旧的 ${res.pruned.length} 份` : ''}`);
    await reload();
  } catch (e: any) {
    // 这条路上"没推上去"最容易悄悄发生（对端换了密钥、桶被删），所以错误原文要进界面：
    // 服务端已经遮过凭据，这里可以直接显示。
    msg.error(`补推失败：${e.message}`);
    await reloadTargets();
  } finally {
    pushing.value = null;
  }
}

function openTargetCreate() {
  tgEditingId.value = null;
  tgForm.value = { ...blankTarget(), _masked_ak: '' };
  tgShow.value = true;
}
function openTargetEdit(r: any) {
  tgEditingId.value = r.id;
  // secret_key 从不回显（服务端只给掩码 AK 和 has_secret），所以这里必须留空：
  // 后端 PATCH 的语义是"给了才换"，留空＝不改手上那一份。
  // access_key 回显的是掩码，_masked_ak 记住它，用来分辨"用户没动这一格"和"填了新值"。
  tgForm.value = {
    name: r.name, endpoint: r.endpoint, bucket: r.bucket, region: r.region,
    prefix: r.prefix, access_key: r.access_key_masked, secret_key: '',
    path_style: !!r.path_style, enabled: !!r.enabled, _masked_ak: r.access_key_masked,
  };
  tgShow.value = true;
}
async function submitTarget() {
  if (tgSubmitting.value) return;
  const creating = tgEditingId.value == null;
  if (!tgForm.value.name.trim() || !tgForm.value.endpoint.trim() || !tgForm.value.bucket.trim()) {
    msg.error('目标名、Endpoint、桶名都要填');
    return;
  }
  if (!tgForm.value.access_key.trim()) {
    msg.error(creating ? 'Access Key 要填' : 'Access Key 不能被清空');
    return;
  }
  // 编辑时这一格是服务端给的掩码（`AKIA…ABCD`），原样提交会把掩码当凭据存进去，
  // 之后每一次签名都带着一串"看不懂的假 key"失败。所以掩码必须改成真值才能提交。
  if (!creating && /[…]/.test(tgForm.value.access_key)) {
    msg.error('Access Key 框里现在是掩码，要改就填完整的新值');
    return;
  }
  if (creating && !tgForm.value.secret_key) {
    msg.error('新建异地目标必须填 Secret Key');
    return;
  }
  tgSubmitting.value = true;
  const body: any = {
    name: tgForm.value.name.trim(),
    endpoint: tgForm.value.endpoint.trim(),
    bucket: tgForm.value.bucket.trim(),
    region: tgForm.value.region.trim() || 'us-east-1',
    prefix: tgForm.value.prefix,
    path_style: !!tgForm.value.path_style,
    enabled: !!tgForm.value.enabled,
  };
  if (creating || tgForm.value.access_key !== tgForm.value._masked_ak) body.access_key = tgForm.value.access_key.trim();
  if (tgForm.value.secret_key) body.secret_key = tgForm.value.secret_key;
  try {
    if (creating) await api.createCloudTarget(body);
    else await api.updateCloudTarget(tgEditingId.value!, body);
    msg.success(creating ? '已创建，建议先点「测试连通」确认桶和凭据都对' : '已保存');
    tgShow.value = false;
    await reloadTargets();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    tgSubmitting.value = false;
  }
}

async function toggleTarget(r: any) {
  if (tgToggling.value !== null) return;
  tgToggling.value = r.id;
  try {
    await api.updateCloudTarget(r.id, { enabled: !r.enabled });
    await reloadTargets();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    tgToggling.value = null;
  }
}

async function testTarget(r: any) {
  if (tgTesting.value !== null) return;
  tgTesting.value = r.id;
  try {
    const res = await api.testCloudTarget(r.id);
    probeTitle.value = r.name;
    probeItems.value = [
      { k: '目标', v: res.target },
      { k: 'Endpoint', v: res.endpoint, mono: true },
      { k: '桶', v: res.bucket, mono: true },
      { k: '区域', v: res.region, mono: true },
      { k: '前缀', v: res.prefix || '（根）', mono: true },
      { k: '寻址方式', v: res.addressing === 'path-style' ? 'path-style（endpoint/桶/对象）' : 'virtual-host（桶.endpoint/对象）' },
      { k: '前缀下对象数', v: `${res.objects_under_prefix} 个` },
      { k: '前缀下合计大小', v: fmtBytes(res.bytes_under_prefix || 0) },
      { k: '列表是否读完', v: res.list_truncated ? '没读完（对象很多，只看到前 1000 个）' : '已读完' },
      { k: '对端请求号', v: res.request_id || '（对端未返回）', mono: true },
    ];
    probeShow.value = true;
    msg.success(`「${r.name}」连通正常`);
    // 探通会把 last_error 清空，不重取就还挂着上一次失败的红字
    await reloadTargets();
  } catch (e: any) {
    msg.error(`「${r.name}」连通失败：${e.message}`);
    await reloadTargets();
  } finally {
    tgTesting.value = null;
  }
}

const tgDelDialog = ref<InstanceType<typeof ConfirmActionDialog> | null>(null);
function confirmDeleteTarget(r: any) {
  tgDelDialog.value?.acceptParams({
    title: '确认删除异地目标',
    names: [r.name],
    msg: r.linked.length
      ? `还有 ${r.linked.length} 个任务挂在这个目标上，服务端会拒绝删除；请先在任务里取消关联。`
      : '只是不再往这个目标推副本，桶里已有的对象不会删除（那可能是一份正被人用来恢复的数据），本机的推送记录会一并清掉。',
    api: async () => {
      await api.deleteCloudTarget(r.id);
      await reloadTargets();
    },
    successMsg: '已删除',
  });
}
</script>

<template>
  <div>
    <CpToolbarCard title="备份">
      <template #left>
        <NButton v-if="isAdmin" size="small" type="primary" :icon="icon('AddOutline', 14)" @click="openCreate">新建备份任务</NButton>
        <NButton size="small" tertiary :icon="icon('FolderOpenOutline', 14)" @click="router.push('/backups/files')">备份文件</NButton>
      </template>
      <template #right>
        <NSelect v-model:value="autoSec" :options="AUTO_OPTS" size="small" style="width: 130px; max-width: 100%" aria-label="自动刷新间隔" />
        <NButton size="small" tertiary :loading="loading || refreshing" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
      </template>

      <NAlert type="info" :bordered="false" size="small" style="margin-bottom: var(--space-3)">
        产物在本页管理、在「备份文件」页统一下载/恢复/清理。调度由 systemd timer 独立完成，面板进程停止不影响备份执行。PG 用 pg_dump 自定义格式；应用目录打包自动排除 node_modules / .venv / .next / .output；SQLite 走在线备份 API，热备份不会写出页不一致的库。
      </NAlert>

      <NDataTable
        ref="tableAnchor"
        :columns="columns" :data="rows" size="small" :bordered="false" :loading="loading"
        :scroll-x="'auto'" :max-height="tableH" :row-key="(r: any) => r.id"
      >
        <template #empty>
          <EmptyBox :text="error ? '备份任务获取失败，点上方「刷新」重试' : (isAdmin ? '还没有备份任务，点左上方「新建备份任务」' : '还没有备份任务')" />
        </template>
      </NDataTable>
    </CpToolbarCard>

    <NDrawer v-model:show="show" :width="drawerW">
      <NDrawerContent :title="`${editingId == null ? '新建备份任务' : '编辑备份任务'} - ${form.target || ''}`" closable>
        <NForm label-placement="top">
          <NSpace vertical :size="12">
            <NFormItem label="类型">
              <NSpace vertical :size="2" style="width: 100%">
                <NSelect v-model:value="form.kind" :options="kindOpts" aria-label="备份类型" @update:value="form.target = null" />
                <span class="cp-help">{{ KIND_DESC[form.kind] }}</span>
              </NSpace>
            </NFormItem>
            <NFormItem v-if="form.kind === 'pg'" label="数据库">
              <NSpace vertical :size="2" style="width: 100%">
                <NSpace :size="8">
                  <!-- 下拉来自实例里真实存在的库；PG 没装或连不上时列表是空的，
                       tag+filterable 让用户直接键入，不至于把这条路径整个堵死。 -->
                  <NSelect
                    v-model:value="form.target"
                    :options="pgOpts"
                    filterable
                    tag
                    placeholder="选择数据库"
                    style="width: 230px; max-width: 100%"
                    aria-label="数据库名称"
                  />
                  <NButton size="small" tertiary @click="form.target = 'all'">全部</NButton>
                </NSpace>
                <span class="cp-help">填单个库名只备份该库；选 all 备份实例内全部库。库名只校验格式，之后库不存在的话这个任务每晚都会失败。</span>
              </NSpace>
            </NFormItem>
            <NFormItem v-else :label="form.kind === 'sqlite' ? 'SQLite 应用' : '应用'">
              <NSpace vertical :size="2" style="width: 100%">
                <NSelect v-model:value="form.target" :options="targetOptions" aria-label="备份应用" placeholder="选择应用" />
                <span v-if="form.kind === 'sqlite'" class="cp-help">自动选取应用目录下体积最大的 *.db / *.sqlite*（忽略 -wal / -shm），备份文件名会记下源库名，恢复时按它精确写回。</span>
              </NSpace>
            </NFormItem>
            <NFormItem label="计划">
              <NSpace vertical :size="2" style="width: 100%">
                <NSelect v-model:value="form.schedule" :options="schedOpts" aria-label="备份计划" />
                <span class="cp-help">定时计划渲染成 systemd timer，面板进程停止不影响执行；手动任务不挂 timer，只在列表里点「立即备份」。</span>
              </NSpace>
            </NFormItem>
            <NFormItem v-if="form.schedule !== 'manual'" label="执行时刻">
              <NSpace align="center" :size="8">
                <NInputNumber v-model:value="form.hour" :min="0" :max="23" :input-props="{ 'aria-label': '执行小时' }" style="width: 84px; max-width: 100%" /> 时
                <NInputNumber v-model:value="form.minute" :min="0" :max="59" :input-props="{ 'aria-label': '执行分钟' }" style="width: 84px; max-width: 100%" /> 分（本地时区）
              </NSpace>
            </NFormItem>
            <NSpace justify="space-between" align="center" style="width: 100%">
              <span id="bk-schedule-enabled" style="font-size: var(--fs-sm); color: var(--cp-text-dim)">启用定时计划</span>
              <NSwitch v-model:value="form.enabled" size="small" aria-labelledby="bk-schedule-enabled" />
            </NSpace>
            <NFormItem label="保留份数">
              <NSpace vertical :size="2">
                <NInputNumber v-model:value="form.keep" :min="1" :max="365" :input-props="{ 'aria-label': '保留备份份数' }" style="width: 130px; max-width: 100%" />
                <span class="cp-help">超出份数后由任务自动裁剪最旧的产物（按写入顺序删，同一秒生成的多份按序号排；不是按文件名的字母序）。</span>
              </NSpace>
            </NFormItem>
            <NFormItem v-if="isAdmin" label="异地目标">
              <NSpace vertical :size="2" style="width: 100%">
                <NSelect
                  v-model:value="form.cloud_target_id"
                  :options="cloudOpts"
                  clearable
                  filterable
                  placeholder="不推异地（只在本机留副本）"
                  aria-label="异地备份目标"
                />
                <span class="cp-help">选一个 S3 兼容对象存储目标，这份任务的每个产物会在本机落成功后再推一份过去；本机写入失败时不会推，远端不会留下本地没有的半成品。清空这一格就是解除关联。</span>
              </NSpace>
            </NFormItem>
          </NSpace>
        </NForm>
        <template #footer>
          <NSpace justify="end" :size="12">
            <NButton size="small" :disabled="submitting" @click="show = false">取消</NButton>
            <NButton size="small" type="primary" class="cp-press" :loading="submitting" :disabled="submitting" :icon="icon(editingId == null ? 'AddOutline' : 'SaveOutline', 14)" @click="submit">确定</NButton>
          </NSpace>
        </template>
      </NDrawerContent>
    </NDrawer>

    <NDrawer v-model:show="logShow" :width="drawerW">
      <NDrawerContent :title="`执行日志 - ${logTitle}`" closable>
        <pre class="log-view log-drawer">{{ logText || '（无输出）' }}</pre>
      </NDrawerContent>
    </NDrawer>

    <CpToolbarCard v-if="isAdmin" title="异地备份目标" style="margin-top: 10px">
      <template #left>
        <NButton size="small" type="primary" :icon="icon('AddOutline', 14)" @click="openTargetCreate">新建异地目标</NButton>
      </template>
      <template #right>
        <NButton size="small" tertiary :loading="tgLoading || tgRefreshing" :icon="icon('RefreshOutline', 14)" @click="reloadTargets">刷新</NButton>
      </template>

      <NAlert type="info" :bordered="false" size="small" style="margin-bottom: var(--space-3)">
        给想长期留一份的对象存储（MinIO / AWS S3 / 兼容端点）建一个目标，再在备份任务里选它。推送发生在本机归档写成功之后：本机失败时远端不会留下本地没有的半成品；异地没推上去也只算「少一份副本」，不会把这次备份判成失败，失败原因写在这里的 last_error 和审计里。Secret Key 只写入、从不回显。
      </NAlert>

      <NDataTable
        :columns="tgColumns" :data="tgList" size="small" :bordered="false"
        :loading="tgLoading || tgRefreshing" :scroll-x="'auto'" :row-key="(r: any) => r.id"
      >
        <template #empty>
          <EmptyBox :text="tgError ? '异地目标获取失败，点上方「刷新」重试' : '还没有异地目标，点左上方「新建异地目标」；不配也完全可用，备份只留在本机'" />
        </template>
      </NDataTable>
    </CpToolbarCard>

    <NDrawer v-if="isAdmin" v-model:show="tgShow" :width="tgDrawerW">
      <NDrawerContent :title="`${tgEditingId == null ? '新建异地目标' : '编辑异地目标'} - ${tgForm.name || ''}`" closable>
        <NForm label-placement="top">
          <NSpace vertical :size="12">
            <NFormItem label="目标名">
              <NSpace vertical :size="2" style="width: 100%">
                <NInput v-model:value="tgForm.name" placeholder="offsite-minio" :input-props="{ 'aria-label': '异地目标名称' }" />
                <span class="cp-help">小写字母开头，只允许小写字母、数字与连字符，2-39 位。这个名字会写进审计，也会出现在任务行上。</span>
              </NSpace>
            </NFormItem>
            <NFormItem label="Endpoint">
              <NSpace vertical :size="2" style="width: 100%">
                <NInput v-model:value="tgForm.endpoint" placeholder="https://minio.example.com:9000" :input-props="{ 'aria-label': '对象存储 Endpoint' }" />
                <span class="cp-help">只接受 http/https 的主机名或 IP（可带端口）。指向元数据地址（169.254.169.254 一类）会被拒绝：面板以 root 发这个请求，把它当成对象存储去访问等于送出一个读实例凭据的口子。</span>
              </NSpace>
            </NFormItem>
            <NFormItem label="桶 / 区域">
              <NSpace :size="8" style="width: 100%">
                <NInput v-model:value="tgForm.bucket" placeholder="backups" :input-props="{ 'aria-label': '桶名' }" style="width: 220px; max-width: 100%" />
                <NInput v-model:value="tgForm.region" placeholder="us-east-1" :input-props="{ 'aria-label': '区域' }" style="width: 150px; max-width: 100%" />
              </NSpace>
            </NFormItem>
            <NFormItem label="前缀">
              <NSpace vertical :size="2" style="width: 100%">
                <NInput v-model:value="tgForm.prefix" placeholder="panel/" :input-props="{ 'aria-label': '对象键前缀' }" />
                <span class="cp-help">对象键的前缀，末尾会自动补 /。留空就是直接放在桶根，桶里已经有别的业务时建议填一个独立前缀，远端裁剪只认这个前缀下的自己人。</span>
              </NSpace>
            </NFormItem>
            <NFormItem label="Access Key">
              <NInput v-model:value="tgForm.access_key" placeholder="AKIAIOSFODNN7EXAMPLE" :input-props="{ 'aria-label': 'Access Key' }" />
            </NFormItem>
            <NFormItem :label="tgEditingId == null ? 'Secret Key' : 'Secret Key（留空＝不改动）'">
              <NSpace vertical :size="2" style="width: 100%">
                <NInput
                  v-model:value="tgForm.secret_key"
                  type="password"
                  show-password-on="click"
                  :placeholder="tgEditingId == null ? '对象的拥有者凭据，保存后不再回显' : '（不换就留空）'"
                  :input-props="{ 'aria-label': 'Secret Key' }"
                />
                <span class="cp-help">列表与详情都从不回显这一项，所以这里永远是空的。留空一定理解为"没改"：把空值当新值提交，会把手上唯一那份凭据清掉。</span>
              </NSpace>
            </NFormItem>
            <NSpace justify="space-between" align="center" style="width: 100%">
              <span id="tg-path-style" style="font-size: var(--fs-sm); color: var(--cp-text-dim)">path-style 寻址</span>
              <NSwitch v-model:value="tgForm.path_style" size="small" aria-labelledby="tg-path-style" />
            </NSpace>
            <span class="cp-help">自建 MinIO、以及任何带端口或用 IP 作 host 的端点都要开着（endpoint/桶/对象）；只有云上 virtual-host（桶.endpoint/对象）才关。开错的话请求会打到桶名拼出来的那个域名上，报一个看不懂的 404。</span>
            <NSpace justify="space-between" align="center" style="width: 100%">
              <span id="tg-enabled" style="font-size: var(--fs-sm); color: var(--cp-text-dim)">启用</span>
              <NSwitch v-model:value="tgForm.enabled" size="small" aria-labelledby="tg-enabled" />
            </NSpace>
            <span class="cp-help">停用是真的停事：定时任务遇到停用中的目标会跳过推送（记成"没推"而不是"失败"），手动「补推异地」也会直接拒绝并让你先启用。</span>
          </NSpace>
        </NForm>
        <template #footer>
          <NSpace justify="end" :size="12">
            <NButton size="small" :disabled="tgSubmitting" @click="tgShow = false">取消</NButton>
            <NButton size="small" type="primary" class="cp-press" :loading="tgSubmitting" :disabled="tgSubmitting" :icon="icon(tgEditingId == null ? 'AddOutline' : 'SaveOutline', 14)" @click="submitTarget">确定</NButton>
          </NSpace>
        </template>
      </NDrawerContent>
    </NDrawer>

    <NDrawer v-if="isAdmin" v-model:show="probeShow" :width="tgDrawerW">
      <NDrawerContent :title="`连通性测试 - ${probeTitle}`" closable>
        <InfoList :items="probeItems" />
        <span class="cp-help" style="display: block; margin-top: var(--space-3)">这里只读不写：HeadBucket 验桶与凭据，ListObjects 验我们写的键看得见。测通了不代表下一次备份一定推得上去，对端随时可能换密钥。</span>
      </NDrawerContent>
    </NDrawer>

    <ConfirmActionDialog ref="delDialog" />
    <ConfirmActionDialog v-if="isAdmin" ref="tgDelDialog" />
  </div>
</template>
