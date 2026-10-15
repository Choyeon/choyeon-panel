<script setup lang="ts">
import { computed, onMounted, ref, h } from 'vue';
import {
  NTabs, NTabPane, NDataTable, NSpace, NModal, NInput, NForm, NFormItem,
  NPopconfirm, NCard, NText, NTag, useMessage, NIcon, NGrid, NGridItem, NSkeleton,
  NSelect, NPagination, NTooltip,
} from 'naive-ui';
import { NButton } from '../ui';
import { api, getRole } from '../api';
import { icon, icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import RowEditor from '../components/RowEditor.vue';
import RedisKeyPanel from '../components/RedisKeyPanel.vue';

const msg = useMessage();
// 只读账号：写操作在服务端一律拦 403，所以这一页凡是对 viewer 必然失败的入口都不摆出来
// （新建、删除、改密、SQL 执行、行/键写入），只留读取路径。
const isAdmin = computed(() => getRole() === 'admin');
const dbs = ref<any[]>([]);
const roles = ref<any[]>([]);
const redis = ref<any>(null);
const sql = ref('');
// 控制台必须有明确的执行库：服务端曾经没有这个参数，语句一律跑在 psql 的默认库上，
// 用户在浏览标签选了业务库、切到控制台查同一张表却报「表不存在」，界面一个字也没提库名。
const sqlDb = ref('postgres');
const sqlOut = ref('');
const sqlBusy = ref(false);
const showDb = ref(false);
const showRole = ref(false);
const dbForm = ref({ name: '', owner: '' });
const roleForm = ref({ name: '', password: '' });
const redisPass = ref('');
const redisSaving = ref(false);
const showPwd = ref(false);
const pwdTarget = ref('');
const pwdValue = ref('');

const ico = (name: string, size = 14) => icon(name, size);

async function load() {
  try {
    dbs.value = await api.pgDbs();
    if (Array.isArray(dbs.value)) roles.value = await api.pgRoles();
  } catch (e: any) {
    dbs.value = [];
    roles.value = [];
    msg.error(`PostgreSQL: ${e.message}`);
  }
  try {
    redis.value = await api.redis();
  } catch (e: any) {
    redis.value = { error: e.message };
  }
}

const dbCols: any[] = [
  {
    title: '数据库', key: 'name',
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [h(NIcon, { component: icons.CubeOutline, size: 17, color: 'var(--cp-brand-soft)' })]),
        h('div', { class: 'cell-txt' }, [h('span', { class: 'cell-name', title: r.name }, r.name), h('div', { class: 'cell-sub cp-prose', title: `owner: ${r.owner}` }, `owner: ${r.owner}`)]),
      ]),
  },
  { title: '大小', key: 'size', width: 96, render: (r: any) => h('span', { class: 'mono-dim' }, r.size) },
  {
    title: '连接数', key: 'conns', width: 88,
    render: (r: any) => h('span', { class: 'vchip' }, `${r.conns} conn`),
  },
  {
    title: '操作', key: 'ops', width: 70, align: 'center', fixed: 'right' as const,
    render: (r: any) =>
      h(NPopconfirm, { onPositiveClick: () => manage('dropDb', r.name) }, {
        trigger: () => h(NButton, { text: true, size: 'small', type: 'error', class: 'cp-op', title: `删除数据库 ${r.name}` }, { default: () => '删除' }),
        default: () => `确定删除数据库 ${r.name}？数据不可恢复`,
      }),
  },
];
const roleCols: any[] = [
  {
    title: '角色', key: 'name',
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [h(NIcon, { component: icons.KeyOutline, size: 16, color: r.super ? 'var(--cp-warn)' : 'var(--cp-text-mute)' })]),
        h('div', { class: 'cell-txt' }, [h('span', { class: 'cell-name', title: r.name }, r.name)]),
      ]),
  },
  { title: '登录', key: 'login', width: 88, render: (r: any) => h('span', { class: 'st ' + (r.login ? 'ok' : 'idle') }, [h('span', { class: 'dot ' + (r.login ? 'ok' : 'idle') }), r.login ? '可登录' : '仅组']) },
  { title: 'Superuser', key: 'super', width: 92, render: (r: any) => (r.super ? h(NTag, { size: 'tiny', bordered: false, type: 'warning' }, () => 'SUPER') : h('span', { class: 'cell-sub' }, '—')) },
  {
    title: '操作', key: 'ops', width: 110, align: 'center', fixed: 'right' as const,
    render: (r: any) =>
      h('div', { class: 'cp-ops' }, [
        h(NButton, { text: true, size: 'small', type: 'primary', class: 'cp-op', disabled: pgBusy.value !== null, onClick: () => openPwd(r.name) }, () => '改密'),
        h(NPopconfirm, { onPositiveClick: () => manage('dropRole', r.name) }, {
          trigger: () => h(NButton, {
            text: true, size: 'small', type: 'error', class: 'cp-op',
            loading: pgBusy.value === `dropRole:${r.name}`, disabled: pgBusy.value !== null,
            title: `删除角色 ${r.name}`,
          }, { default: () => '删除' }),
          default: () => `删除角色 ${r.name} 及其拥有的所有对象？`,
        }),
      ]),
  },
];

// PG 的 DDL 一次只让跑一个：DROP ROLE 要连带删它拥有的对象，慢的时候十几秒，
// 连点「删除」会并发两条同样针对该角色的 DDL（第二条报 role does not exist），
// 而 createDb/createRole 的连点会拿到 "already exists"——两条红字盖住已经成功的第一条。
// 弹窗的确定按钮靠 :disabled="pgBusy" 置灰，行内按钮靠同一个标志转圈；
// 早退兜底是给 @keyup.enter 这类不走按钮的入口留的。
const pgBusy = ref<string | null>(null);
async function manage(action: string, name: string, extra: any = {}): Promise<boolean> {
  if (pgBusy.value !== null) return false;
  pgBusy.value = `${action}:${name}`;
  try {
    await api.pgManage({ action, name, ...extra });
    msg.success('完成');
    load();
    return true;
  } catch (e: any) {
    msg.error(e.message);
    return false;
  } finally {
    pgBusy.value = null;
  }
}
async function createDb() {
  if (!dbForm.value.name) return msg.error('请填写名称');
  // 失败时保持弹窗与已填内容不变：以前不管成败都关窗清空，
  // 用户看到红色报错时表单已经空了，只能凭记忆重打一遍。
  if (await manage('createDb', dbForm.value.name, { owner: dbForm.value.owner || undefined })) {
    showDb.value = false;
    dbForm.value = { name: '', owner: '' };
  }
}
async function createRole() {
  if (!roleForm.value.name || !roleForm.value.password) return msg.error('用户名与密码必填');
  if (await manage('createRole', roleForm.value.name, { password: roleForm.value.password })) {
    showRole.value = false;
    roleForm.value = { name: '', password: '' };
  }
}
function openPwd(name: string) {
  pwdTarget.value = name;
  pwdValue.value = '';
  showPwd.value = true;
}
async function confirmPwd() {
  if (!pwdValue.value) return msg.error('密码不能为空');
  if (await manage('setPassword', pwdTarget.value, { password: pwdValue.value })) showPwd.value = false;
}
async function runSql() {
  if (sqlBusy.value) return;
  if (!sql.value.trim()) return;
  sqlBusy.value = true;
  try {
    const r = await api.pgQuery(sql.value, sqlDb.value || 'postgres');
    sqlOut.value = r.result || '(无返回)';
  } catch (e: any) {
    sqlOut.value = '错误: ' + e.message;
  } finally {
    sqlBusy.value = false;
  }
}
async function saveRedisPass() {
  if (redisSaving.value) return;
  const cleared = !redisPass.value.trim();
  redisSaving.value = true;
  try {
    await api.redisPassword(redisPass.value);
    // 空值现在是"清除"（服务端以前静默忽略空值，界面却照样提示"已保存"，
    // 于是填错口令的人永远改不掉它）；文案要说清楚清掉之后读的是 redis.conf。
    msg.success(cleared ? '已清除，改回读取 redis.conf' : '已保存');
    redisPass.value = '';
    load();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    redisSaving.value = false;
  }
}

const hitRate = () => {
  const i = redis.value?.info;
  if (!i) return '—';
  // INFO 里的值是字符串，且 keyspace_hits=0 是合法值（旧写法用 `!i.keyspace_hits`
  // 判断，把"一次都没命中"当成缺字段，永远显示 —）；非数字更要挡住，否则渲染出 NaN%。
  const hit = Number(i.keyspace_hits);
  const mis = Number(i.keyspace_misses);
  if (!Number.isFinite(hit) || !Number.isFinite(mis) || hit + mis <= 0) return '—';
  return Math.round((hit / (hit + mis)) * 100) + '%';
};

onMounted(load);

/* ---------- PG 数据浏览 ---------- */
const brDb = ref<string | null>(null);
const tables = ref<any[]>([]);
const tablesBusy = ref(false);
const curTable = ref<any>(null);
const tRows = ref<any[]>([]);
const tCols = ref<string[]>([]);
const tColMeta = ref<any[]>([]);
const tPk = ref<string[]>([]);
const tTotal = ref(0);
const tPage = ref(1);
const tSize = ref(50);
const tBusy = ref(false);
const showRow = ref(false);
const rowModel = ref<any>(null);
const rowInsert = ref(false);
const rowFocus = ref<string | null>(null);

const dbOptions = computed(() => (Array.isArray(dbs.value) ? dbs.value : []).map((d: any) => ({ label: d.name, value: d.name })));
// PG 未运行时 dbs 是空数组：控制台至少要有 postgres 可选，否则下拉是空的、执行库显示成空白。
const sqlOptions = computed(() => {
  const o = dbOptions.value;
  return o.some((x: any) => x.value === 'postgres') ? o : [{ label: 'postgres', value: 'postgres' }, ...o];
});
const browseCols: any[] = [
  { title: '表', key: 'name', render: (r: any) => h('div', { class: 'cell-main' }, [h('span', { class: 'cell-ico' }, [h(NIcon, { component: icons.DocumentTextOutline, size: 15, color: 'var(--cp-brand-soft)' })]), h('div', { class: 'cell-txt' }, [h('span', { class: 'cell-name', title: r.name }, r.name), h('div', { class: 'cell-sub cp-prose', title: r.schema }, r.schema)])]) },
  { title: '行数（估）', key: 'row_estimate', width: 110, render: (r: any) => h('span', { class: 'mono-dim' }, String(r.row_estimate ?? '—')) },
  { title: '大小', key: 'size', width: 100, render: (r: any) => h('span', { class: 'vchip' }, r.size ?? '—') },
  { title: '操作', key: 'go', width: 80, align: 'center', render: (r: any) => h(NButton, { text: true, size: 'small', type: 'primary', class: 'cp-op', onClick: () => pickTable(r) }, () => '查看数据') },
];
const dataCols = computed<any[]>(() => [
  {
    title: '操作', key: 'go', width: 60, fixed: 'left' as const, className: 'row-ops',
    render: (r: any) => h(NButton, { text: true, size: 'small', type: 'primary', class: 'cp-op', onClick: () => openRow(r, false) }, () => '查看'),
  },
  ...tCols.value.map((c: string) => ({
    // 这里的 hover tooltip 是桌面端的补充，不是唯一入口：整行点击（:row-props="rowProps"）
    // 打开的行详情抽屉里有完整值 + 编辑，触屏靠它。测试锁的就是这条通道还在。
    title: c, key: c, ellipsis: { tooltip: true }, width: 180,
    render: (r: any) => fmtCell(r[c], c, r),
  })),
]);
const keyCols: any[] = [
  { type: 'selection' },
  { title: 'Key', key: 'k', ellipsis: { tooltip: true }, render: (r: any) => h('span', { class: 'mono-dim' }, r.k) },
  {
    title: '操作', key: 'go', width: 60, className: 'row-ops',
    render: (r: any) => h(NButton, { text: true, size: 'small', type: 'primary', class: 'cp-op', onClick: () => inspectKey(r.k) }, () => '查看'),
  },
];

// 操作列/勾选列按角色收起来：留一列必然 403 的按钮，比不给按钮更让人困惑。
const dropOps = (cols: any[]) => cols.filter((c) => isAdmin.value || c.key !== 'ops');
const dbColsV = computed(() => dropOps(dbCols));
const roleColsV = computed(() => dropOps(roleCols));
const keyColsV = computed(() => (isAdmin.value ? keyCols : keyCols.filter((c) => c.type !== 'selection')));

function jsonBrief(v: any) {
  if (Array.isArray(v)) return `Array(${v.length})`;
  return `{ ${Object.keys(v || {}).length} 项 }`;
}

function fmtCell(v: any, col = '', row?: any) {
  if (v === null || v === undefined) return h('span', { class: 'cell-sub' }, 'NULL');
  if (typeof v === 'object') {
    // 字典/数组在 180px 网格里只会糊成一串引号：单元格给摘要芯片，
    // 悬停看格式化 JSON，点击直接打开行详情（那里有完整的树 + 编辑）。
    return h(NTooltip, { trigger: 'hover', placement: 'top' }, {
      trigger: () =>
        h('button', {
          type: 'button', class: 'json-chip', 'aria-haspopup': 'dialog',
          'aria-label': `查看 ${col} 列的完整内容`,
          // stopPropagation 拦下整行点击，否则一次点击开两次抽屉；
          // 真正打开的动作在这里做，并把这一列作为焦点交给抽屉。
          onClick: (e: MouseEvent) => {
            e.stopPropagation();
            if (row) openRow(row, false, col);
          },
        }, jsonBrief(v)),
      default: () => h('pre', { class: 'json-tip' }, JSON.stringify(v, null, 2).slice(0, 3000)),
    });
  }
  return String(v);
}

/** 整行可点开详情，但操作列/勾选框的点击要放行，否则点删除会先弹抽屉。 */
function rowProps(r: any) {
  return {
    class: 'cp-row-click',
    onClick: (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.closest('.row-ops') || t.closest('input') || t.closest('.n-checkbox'))) return;
      openRow(r, false);
    },
  };
}
function keyRowProps(r: any) {
  return { class: 'cp-row-click', onClick: () => inspectKey(r.k) };
}
function openRow(r: any, insert: boolean, focus: string | null = null) {
  rowInsert.value = insert;
  rowModel.value = insert ? null : r;
  rowFocus.value = focus;
  showRow.value = true;
}

// 快速切库时同样要认序号：先发的 pgTables 后到就会把 A 库的表名画在 B 库下面，
// 用户点进去的是「B 库里的 A 表」，报的错看起来像权限问题。和 loadTableData 一个口径。
let tablesSeq = 0;
async function loadTables() {
  const seq = ++tablesSeq;
  curTable.value = null;
  tRows.value = [];
  tCols.value = [];
  tColMeta.value = [];
  tPk.value = [];
  if (!brDb.value) return;
  tablesBusy.value = true;
  try {
    const r = await api.pgTables(brDb.value);
    if (seq !== tablesSeq) return;
    tables.value = r;
  } catch (e: any) {
    if (seq !== tablesSeq) return;
    tables.value = [];
    msg.error(e.message);
  } finally {
    if (seq === tablesSeq) tablesBusy.value = false;
  }
}
async function pickTable(t: any) {
  curTable.value = t;
  tPage.value = 1;
  await loadTableData();
}
// 连点翻页 / 快速切表时，先发的请求可能后到：不比对序号就会把当前页覆盖成
// 上一个请求的结果（看到的是第 2 页的数据、页码却显示第 3 页）。
let tableSeq = 0;
async function loadTableData() {
  if (!brDb.value || !curTable.value) return;
  const seq = ++tableSeq;
  tBusy.value = true;
  try {
    const r = await api.pgTable(brDb.value, curTable.value.schema, curTable.value.name, tPage.value, tSize.value);
    if (seq !== tableSeq) return;
    tCols.value = r.columns || [];
    tColMeta.value = r.cols || [];
    tPk.value = r.pk || [];
    tRows.value = r.rows || [];
    tTotal.value = r.total || 0;
  } catch (e: any) {
    if (seq !== tableSeq) return;
    tRows.value = [];
    msg.error(e.message);
  } finally {
    if (seq === tableSeq) tBusy.value = false;
  }
}
async function onRowSaved() {
  // 保存后 tRows 换成一批新对象，抽屉里的 rowModel 还指着旧的那一行：
  // 改过主键（或按 ctid 定位）的行会以旧键再次提交，更新/删除都命中不到，
  // 而只读详情也停在改之前的值上，看着像"保存没生效"。收回列表重选。
  showRow.value = false;
  await loadTableData();
}
function onPage(p: number) {
  tPage.value = p;
  loadTableData();
}

/* ---------- Redis 浏览器 ---------- */
const rPattern = ref('*');
const rKeys = ref<string[]>([]);
const rCursor = ref('0');
const rBusy = ref(false);
const rDetail = ref<any>(null);
const rSel = ref<string[]>([]);
const rDetailBusy = ref(false);
const deletingKeys = ref(false);

async function scanKeys(more = false) {
  // 「加载更多」连点会拿同一个 cursor 要两页，两份同样的键名都 append 进列表：
  // 键重复显示一遍，条数也对不上 r.dbsize。
  if (rBusy.value) return;
  rBusy.value = true;
  try {
    const r = await api.redisKeys(more ? rCursor.value : '0', rPattern.value || '*');
    rKeys.value = more ? [...rKeys.value, ...(r.keys || [])] : (r.keys || []);
    rCursor.value = r.cursor || '0';
    if (!more && rDetail.value) rDetail.value = null;
  } catch (e: any) {
    if (!more) rKeys.value = [];
    msg.error(e.message);
  } finally {
    rBusy.value = false;
  }
}
let keySeq = 0;
async function inspectKey(k: string) {
  const seq = ++keySeq;
  rDetailBusy.value = true;
  try {
    const d = await api.redisKey(k);
    if (seq !== keySeq) return;
    rDetail.value = d;
  } catch (e: any) {
    if (seq !== keySeq) return;
    rDetail.value = null;
    msg.error(e.message);
  } finally {
    if (seq === keySeq) rDetailBusy.value = false;
  }
}
function refreshDetail() {
  if (rDetail.value?.key) void inspectKey(rDetail.value.key);
}
async function delKeys(keys: string[]) {
  // 批量删 key 是唯一不可撤销的操作：连点会发两次 DEL，第二次报「已删除 0 个 key」，
  // 让人怀疑第一次是不是也没删掉。用 rBusy 之外的独立标志——删除进行中时扫描按钮也该置灰。
  if (deletingKeys.value || rBusy.value) return;
  deletingKeys.value = true;
  try {
    const r = await api.redisDel(keys);
    msg.success(`已删除 ${r.deleted} 个 key`);
    rSel.value = [];
    if (rDetail.value && keys.includes(rDetail.value.key)) rDetail.value = null;
    await scanKeys(false);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    deletingKeys.value = false;
  }
}
</script>

<template>
  <div>
    <PageHeader title="数据库" sub="PostgreSQL 与 Redis 的库、角色、密码与轻量 SQL 控制台">
      <template #actions>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <NCard size="small" :content-style="{ padding: '0' }">
      <!-- show:lazy：五个标签各有一张大表，进页就全挂载等于一次性建五份 DOM；没点开的标签等点击时再挂。 -->
      <NTabs type="line" animated display-directive="show:lazy" :tabs-padding="16" pane-style="padding: var(--space-4)">
        <NTabPane name="pg" tab="PostgreSQL">
          <NSpace vertical size="medium">
            <NSpace justify="space-between" align="center" style="width: 100%">
              <span class="section-title"><NIcon :component="icons.ServerOutline" :size="15" color="var(--cp-brand-soft)" /> 数据库列表</span>
              <NSpace :size="8">
                <NButton size="small" tertiary @click="load" :icon="ico('RefreshOutline')">刷新</NButton>
                <NButton v-if="isAdmin" size="small" type="primary" :icon="ico('AddOutline')" @click="showDb = true">新建数据库</NButton>
              </NSpace>
            </NSpace>
            <!-- 两个列表同屏，这里不用 --table-h 撑满视口（角色表已有 320 固定上限），保持自然高度 -->
            <NDataTable size="small" :bordered="false" :columns="dbColsV" :data="Array.isArray(dbs) ? dbs : []" :row-key="(r: any) => r.name" :scroll-x="'auto'">
              <template #empty><EmptyBox text="无法读取数据库列表（PostgreSQL 未运行？）" /></template>
            </NDataTable>

            <NSpace justify="space-between" align="center" style="width: 100%; margin-top: var(--space-2)">
              <span class="section-title"><NIcon :component="icons.KeyOutline" :size="15" color="var(--cp-warn)" /> 用户 / 角色</span>
              <NButton v-if="isAdmin" size="small" :icon="ico('AddOutline')" @click="showRole = true">新建用户</NButton>
            </NSpace>
            <NDataTable size="small" :bordered="false" :columns="roleColsV" :data="Array.isArray(roles) ? roles : []" :row-key="(r: any) => r.name" :max-height="320" :scroll-x="'auto'">
              <template #empty><EmptyBox text="没有可读取的角色（PostgreSQL 未运行？）" /></template>
            </NDataTable>
          </NSpace>
        </NTabPane>

        <NTabPane v-if="isAdmin" name="sql" tab="SQL 控制台">
          <NSpace vertical :size="12" style="width: 100%">
            <NSpace align="center" :size="10">
              <NText depth="3" style="font-size: var(--fs-xs)">执行库</NText>
              <NSelect v-model:value="sqlDb" :options="sqlOptions" size="small" style="width: 200px; max-width: 100%" placeholder="选择数据库" aria-label="SQL 执行的数据库" />
              <NText depth="3" style="font-size: var(--fs-xs)">语句只在上面选中的库里执行</NText>
            </NSpace>
            <NInput
              v-model:value="sql" type="textarea" :autosize="{ minRows: 4, maxRows: 9 }"
              placeholder="SELECT * FROM some_table LIMIT 20;"
              :input-props="{ class: 'mono', 'aria-label': 'SQL 语句', style: 'font-size: var(--fs-sm)' }"
              @keydown.ctrl.enter="runSql"
            />
            <NSpace align="center">
              <NButton type="primary" class="cp-press" :loading="sqlBusy" :disabled="sqlBusy" :icon="ico('PlayOutline')" @click="runSql">执行</NButton>
              <NText depth="3" style="font-size: var(--fs-xs)">Ctrl+Enter 快速执行 · 以 postgres 超级用户连接 {{ sqlDb || 'postgres' }}，请谨慎操作</NText>
            </NSpace>
            <div class="log-view" style="max-height: 380px" role="region" aria-live="polite" aria-label="SQL 执行结果">{{ sqlOut || '结果将显示在这里…' }}</div>
          </NSpace>
        </NTabPane>

        <NTabPane name="browse" tab="PG 数据浏览">
          <NSpace vertical :size="12">
            <NSpace align="center" :size="10" wrap>
              <NText depth="3" style="font-size: var(--fs-xs)">数据库</NText>
              <NSelect v-model:value="brDb" :options="dbOptions" size="small" style="width: 200px; max-width: 100%" placeholder="选择数据库" aria-label="浏览的数据库" @update:value="loadTables" />
              <NText depth="3" style="font-size: var(--fs-xs)">共 {{ tables.length }} 张表</NText>
            </NSpace>
            <NDataTable v-if="!curTable" size="small" :bordered="false" :columns="browseCols" :data="tables" :loading="tablesBusy" :row-key="(r: any) => `${r.schema}.${r.name}`" class="cp-tbl tbl-dbs-browse" max-height="var(--table-h)" :scroll-x="'auto'">
              <template #empty><EmptyBox :text="brDb ? (tablesBusy ? '加载中…' : '这个库里没有表') : '先选择一个数据库'" /></template>
            </NDataTable>
            <template v-else>
              <NSpace align="center" :size="10" wrap>
                <NButton size="tiny" tertiary :icon="ico('ArrowBackOutline')" @click="curTable = null">表列表</NButton>
                <NTag size="small" :bordered="false" type="info">{{ curTable.schema }}.{{ curTable.name }}</NTag>
                <NText depth="3" style="font-size: var(--fs-xs)">{{ tTotal }} 行 · {{ tCols.length }} 列 · 点击任意行{{ isAdmin ? '查看/编辑' : '查看详情' }}</NText>
                <NButton size="tiny" tertiary :icon="ico('RefreshOutline')" @click="loadTableData">刷新</NButton>
                <NButton v-if="isAdmin" size="tiny" type="primary" tertiary :icon="ico('AddOutline')" :disabled="!tColMeta.length" @click="openRow(null, true)">新增行</NButton>
                <NPagination v-model:page="tPage" :page-count="Math.max(1, Math.ceil(tTotal / tSize))" :page-size="tSize" size="small" :show-quick-jumper="false" @update:page="onPage" />
              </NSpace>
              <NDataTable size="small" :bordered="false" :columns="dataCols" :data="tRows" :loading="tBusy" class="cp-tbl tbl-dbs-data" :max-height="'var(--table-h)'" :row-key="(r: any) => r._ctid" :row-props="rowProps" :scroll-x="'auto'">
                <template #empty><EmptyBox text="这张表没有数据" /></template>
              </NDataTable>
            </template>
          </NSpace>
        </NTabPane>

        <NTabPane name="rbr" tab="Redis 浏览器">
          <NSpace vertical :size="12">
            <NSpace align="center" :size="8" wrap>
              <NInput v-model:value="rPattern" size="small" placeholder="匹配模式，如 session:*" style="width: 220px; max-width: 100%" :input-props="{ 'aria-label': 'Redis key 匹配模式', class: 'mono' }" @keyup.enter="scanKeys(false)" />
              <NButton size="small" type="primary" :loading="rBusy" :disabled="rBusy || deletingKeys" :icon="ico('SearchOutline')" @click="scanKeys(false)">扫描</NButton>
              <NButton v-if="rCursor !== '0'" size="small" tertiary :loading="rBusy" :disabled="deletingKeys" :icon="ico('RefreshCircleOutline')" @click="scanKeys(true)">加载更多</NButton>
              <NText depth="3" style="font-size: var(--fs-xs)">已加载 {{ rKeys.length }} 个 key</NText>
              <NPopconfirm v-if="isAdmin && rSel.length" @positive-click="delKeys(rSel)">
                <template #trigger>
                  <NButton size="small" tertiary type="error" :loading="deletingKeys" :disabled="deletingKeys || rBusy" :icon="ico('TrashOutline')">删除选中（{{ rSel.length }}）</NButton>
                </template>
                删除这 {{ rSel.length }} 个 key？UNLINK 异步回收，不可恢复。
              </NPopconfirm>
            </NSpace>
            <div class="rbr-flex">
              <div class="rk-list">
                <NDataTable
                  size="small" :bordered="false"
                  :columns="keyColsV"
                  :data="rKeys.map((k) => ({ k }))"
                  :row-key="(r: any) => r.k"
                  v-model:checked-row-keys="rSel"
                  :loading="rBusy"
                  :row-props="keyRowProps"
                  :row-class-name="(r: any) => (r.k === rDetail?.key ? 'row-active' : '')"
                  :max-height="380"
                  :scroll-x="'auto'"
                >
                  <template #empty><EmptyBox :text="rBusy ? '扫描中…' : '没有匹配的 key（SCAN 从 0 开始，可换模式再试）'" /></template>
                </NDataTable>
              </div>
              <RedisKeyPanel
                v-if="rDetail" :detail="rDetail" :busy="rDetailBusy"
                @refresh="refreshDetail" @deleted="(k: string) => delKeys([k])"
              />
              <NSkeleton v-else-if="rDetailBusy" height="220px" class="cp-shimmer" style="flex: 1 1 340px; border-radius: var(--radius-lg)" />
              <div v-else-if="rKeys.length" class="rk-hint">
                <NIcon :component="icons.EyeOutline" :size="18" color="var(--cp-text-mute)" />
                <span>点击左侧任意 key 查看并编辑内容</span>
              </div>
            </div>
          </NSpace>
        </NTabPane>

        <NTabPane name="redis" tab="Redis">
          <EmptyBox v-if="redis?.error" :text="`Redis 连接失败：${redis.error}`" />
          <template v-else-if="redis">
            <!-- 手机档两列小卡各 ~150px，「standalone / 面板密码」这类组合值被折得没法读；
                 ≤640px（默认档）收成一列。 -->
            <NGrid cols="1 s:2 m:4" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
              <NGridItem span="1 s:1 m:1"><NCard size="small" class="rstat"><div class="rk">版本</div><div class="rv">{{ redis.info?.redis_version || '—' }}</div></NCard></NGridItem>
              <NGridItem span="1 s:1 m:1"><NCard size="small" class="rstat"><div class="rk">运行模式</div><div class="rv">{{ redis.info?.redis_mode || '—' }} / {{ redis.authed ? '面板密码' : 'redis.conf' }}</div></NCard></NGridItem>
              <NGridItem span="1 s:1 m:1"><NCard size="small" class="rstat"><div class="rk">连接客户端</div><div class="rv">{{ redis.info?.connected_clients ?? '—' }}</div></NCard></NGridItem>
              <NGridItem span="1 s:1 m:1"><NCard size="small" class="rstat"><div class="rk">Key 总数</div><div class="rv">{{ redis.dbsize ?? '—' }}</div></NCard></NGridItem>
              <NGridItem span="1 s:1 m:1"><NCard size="small" class="rstat"><div class="rk">内存占用</div><div class="rv">{{ redis.info?.used_memory_human || '—' }}</div></NCard></NGridItem>
              <NGridItem span="1 s:1 m:1"><NCard size="small" class="rstat"><div class="rk">运行时长</div><div class="rv">{{ Math.round((redis.info?.uptime_in_seconds || 0) / 3600) }} 小时</div></NCard></NGridItem>
              <NGridItem span="1 s:1 m:1"><NCard size="small" class="rstat"><div class="rk">累计命令</div><div class="rv">{{ redis.info?.total_commands_processed ?? '—' }}</div></NCard></NGridItem>
              <NGridItem span="1 s:1 m:1"><NCard size="small" class="rstat"><div class="rk">命中率</div><div class="rv" :style="{ color: 'var(--cp-ok)' }">{{ hitRate() }}</div></NCard></NGridItem>
            </NGrid>
            <NSpace v-if="isAdmin" align="center" :size="12" wrap style="margin-top: var(--space-4)">
              <!-- 输入框里的 placeholder 不滚动也不换行：24 个中文字在 320px 里必然被裁成半截。
                   说明挪到框外用文字写清楚，框内只留能看全的短提示。 -->
              <NInput v-model:value="redisPass" type="password" show-password-on="click" placeholder="Redis 密码" style="width: 240px; max-width: 100%" size="small" :input-props="{ 'aria-label': 'Redis 密码' }" />
              <NButton size="small" :loading="redisSaving" :disabled="redisSaving" :icon="ico('SaveOutline')" @click="saveRedisPass">保存</NButton>
              <NText depth="3" style="font-size: var(--fs-xs)">留空保存＝清除面板密码，改回读取 redis.conf</NText>
            </NSpace>
          </template>
          <NSpace v-else vertical :size="12" style="width: 100%">
            <NSkeleton v-for="i in 4" :key="i" height="52px" class="cp-shimmer" style="border-radius: var(--radius-lg)" />
          </NSpace>
        </NTabPane>
      </NTabs>
    </NCard>

    <NModal v-model:show="showDb" preset="card" title="新建数据库" style="width: 400px; max-width: 94vw">
      <NForm label-placement="left" label-width="70">
        <NSpace vertical :size="12">
          <NFormItem label="名称"><NInput v-model:value="dbForm.name" placeholder="小写/下划线" :input-props="{ 'aria-label': '数据库名称' }" /></NFormItem>
          <NFormItem label="Owner"><NInput v-model:value="dbForm.owner" placeholder="可选，已存在的角色" :input-props="{ 'aria-label': '数据库属主角色' }" /></NFormItem>
          <NButton type="primary" block class="cp-press" :loading="pgBusy === `createDb:${dbForm.name}`" :disabled="pgBusy !== null" :icon="ico('AddOutline')" @click="createDb">创建</NButton>
        </NSpace>
      </NForm>
    </NModal>
    <NModal v-model:show="showRole" preset="card" title="新建用户" style="width: 400px; max-width: 94vw">
      <NForm label-placement="left" label-width="70">
        <NSpace vertical :size="12">
          <NFormItem label="用户名"><NInput v-model:value="roleForm.name" :input-props="{ 'aria-label': '数据库用户名' }" /></NFormItem>
          <NFormItem label="密码"><NInput v-model:value="roleForm.password" type="password" show-password-on="click" :input-props="{ 'aria-label': '数据库用户密码' }" /></NFormItem>
          <NButton type="primary" block class="cp-press" :loading="pgBusy === `createRole:${roleForm.name}`" :disabled="pgBusy !== null" :icon="ico('AddOutline')" @click="createRole">创建</NButton>
        </NSpace>
      </NForm>
    </NModal>
    <NModal v-model:show="showPwd" preset="card" :title="`重置密码 · ${pwdTarget}`" style="width: 400px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NInput v-model:value="pwdValue" type="password" show-password-on="click" placeholder="新密码" :input-props="{ 'aria-label': '新密码' }" @keyup.enter="confirmPwd" />
        <NSpace justify="end">
          <NButton tertiary @click="showPwd = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="pgBusy === `setPassword:${pwdTarget}`" :disabled="pgBusy !== null" :icon="ico('SaveOutline')" @click="confirmPwd">确认修改</NButton>
        </NSpace>
      </NSpace>
    </NModal>

    <RowEditor
      v-if="brDb && curTable" v-model:show="showRow" :db="brDb"
      :schema="curTable.schema" :table="curTable.name"
      :cols="tColMeta" :pk="tPk" :row="rowModel" :insert="rowInsert" :focus="rowFocus"
      @saved="onRowSaved"
    />
  </div>
</template>

<style scoped>
.rstat :deep(.n-card__content) { padding: var(--space-3) var(--space-4); }
.rk { font-size: var(--fs-2xs); color: var(--cp-text-mute); margin-bottom: var(--space-1); }
.rv { font-size: var(--fs-lg); font-weight: 600; color: var(--cp-text); }
/* 窄容器（侧栏展开/窗口收窄）下 key 列表与详情上下堆叠，避免横向溢出裁切 */
.rbr-flex { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-start; }
.rk-list { flex: 1 1 320px; min-width: 0; }
.rk-detail { flex: 1 1 340px; min-width: 0; }
/* 占位提示自己占住详情列的宽度：以前它是个裸 NText，被 flex 挤到容器外沿、文字裁半截 */
.rk-hint {
  flex: 1 1 340px; min-width: 0; align-self: stretch;
  display: flex; align-items: center; justify-content: center; gap: var(--space-2);
  padding: var(--space-6) var(--space-4);
  border: 1px dashed var(--cp-border, rgba(128, 128, 128, 0.28));
  border-radius: var(--radius-lg, 12px);
  color: var(--cp-text-mute); font-size: var(--fs-xs);
}
</style>
