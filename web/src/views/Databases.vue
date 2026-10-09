<script setup lang="ts">
import { computed, onMounted, ref, h } from 'vue';
import {
  NTabs, NTabPane, NDataTable, NSpace, NModal, NInput, NForm, NFormItem,
  NPopconfirm, NCard, NText, NTag, useMessage, NIcon, NGrid, NGridItem, NSkeleton,
  NSelect, NPagination,
} from 'naive-ui';
import { NButton } from '../ui';
import { api } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const dbs = ref<any[]>([]);
const roles = ref<any[]>([]);
const redis = ref<any>(null);
const sql = ref('');
const sqlOut = ref('');
const sqlBusy = ref(false);
const showDb = ref(false);
const showRole = ref(false);
const dbForm = ref({ name: '', owner: '' });
const roleForm = ref({ name: '', password: '' });
const redisPass = ref('');
const showPwd = ref(false);
const pwdTarget = ref('');
const pwdValue = ref('');

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

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
        h('div', { class: 'cell-txt' }, [h('span', { class: 'cell-name' }, r.name), h('div', { class: 'cell-sub' }, `owner: ${r.owner}`)]),
      ]),
  },
  { title: '大小', key: 'size', width: 96, render: (r: any) => h('span', { class: 'mono-dim' }, r.size) },
  {
    title: '连接数', key: 'conns', width: 88,
    render: (r: any) => h('span', { class: 'vchip' }, `${r.conns} conn`),
  },
  {
    title: '操作', key: 'ops', width: 96,
    render: (r: any) =>
      h(NPopconfirm, { onPositiveClick: () => manage('dropDb', r.name) }, {
        trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline'), title: `删除数据库 ${r.name}` }, { default: () => '删除' }),
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
        h('div', { class: 'cell-txt' }, [h('span', { class: 'cell-name' }, r.name)]),
      ]),
  },
  { title: '登录', key: 'login', width: 88, render: (r: any) => h('span', { class: 'st ' + (r.login ? 'ok' : 'idle') }, [h('span', { class: 'dot ' + (r.login ? 'ok' : 'idle') }), r.login ? '可登录' : '仅组']) },
  { title: 'Superuser', key: 'super', width: 92, render: (r: any) => (r.super ? h(NTag, { size: 'tiny', bordered: false, type: 'warning' }, () => 'SUPER') : h('span', { class: 'cell-sub' }, '—')) },
  {
    title: '操作', key: 'ops', width: 172,
    render: (r: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, icon: ico('PencilOutline'), onClick: () => openPwd(r.name) }, () => '改密'),
        h(NPopconfirm, { onPositiveClick: () => manage('dropRole', r.name) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline'), title: `删除角色 ${r.name}` }, { default: () => '删除' }),
          default: () => `删除角色 ${r.name} 及其拥有的所有对象？`,
        }),
      ]),
  },
];

async function manage(action: string, name: string, extra: any = {}): Promise<boolean> {
  try {
    await api.pgManage({ action, name, ...extra });
    msg.success('完成');
    load();
    return true;
  } catch (e: any) {
    msg.error(e.message);
    return false;
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
  if (!sql.value.trim()) return;
  sqlBusy.value = true;
  try {
    const r = await api.pgQuery(sql.value);
    sqlOut.value = r.result || '(无返回)';
  } catch (e: any) {
    sqlOut.value = '错误: ' + e.message;
  } finally {
    sqlBusy.value = false;
  }
}
async function saveRedisPass() {
  const cleared = !redisPass.value.trim();
  try {
    await api.redisPassword(redisPass.value);
    // 空值现在是"清除"（服务端以前静默忽略空值，界面却照样提示"已保存"，
    // 于是填错口令的人永远改不掉它）；文案要说清楚清掉之后读的是 redis.conf。
    msg.success(cleared ? '已清除，改回读取 redis.conf' : '已保存');
    redisPass.value = '';
    load();
  } catch (e: any) {
    msg.error(e.message);
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
const tTotal = ref(0);
const tPage = ref(1);
const tSize = ref(50);
const tBusy = ref(false);

const dbOptions = computed(() => (Array.isArray(dbs.value) ? dbs.value : []).map((d: any) => ({ label: d.name, value: d.name })));
const browseCols: any[] = [
  { title: '表', key: 'name', render: (r: any) => h('div', { class: 'cell-main' }, [h('span', { class: 'cell-ico' }, [h(NIcon, { component: icons.DocumentTextOutline, size: 15, color: 'var(--cp-brand-soft)' })]), h('div', { class: 'cell-txt' }, [h('span', { class: 'cell-name' }, r.name), h('div', { class: 'cell-sub' }, r.schema)])]) },
  { title: '行数（估）', key: 'row_estimate', width: 110, render: (r: any) => h('span', { class: 'mono-dim' }, String(r.row_estimate ?? '—')) },
  { title: '大小', key: 'size', width: 100, render: (r: any) => h('span', { class: 'vchip' }, r.size ?? '—') },
  { title: '', key: 'go', width: 80, render: (r: any) => h(NButton, { size: 'tiny', tertiary: true, type: 'primary', onClick: () => pickTable(r) }, () => '查看数据') },
];
const dataCols = computed<any[]>(() => tCols.value.map((c: string) => ({
  title: c, key: c, ellipsis: { tooltip: false }, width: 180,
  render: (r: any) => fmtCell(r[c]),
})));
const pairCols: any[] = [
  { title: 'Field / Member', key: 'field', ellipsis: { tooltip: false } },
  { title: 'Value / Score', key: 'value', ellipsis: { tooltip: false } },
];
const idxCols: any[] = [
  { title: '#', key: 'i', width: 70 },
  { title: 'Value', key: 'v', ellipsis: { tooltip: false } },
];
const keyCols: any[] = [
  { type: 'selection' },
  { title: 'Key', key: 'k', render: (r: any) => h('span', { class: 'mono-dim', style: 'word-break:break-all' }, r.k) },
];

function fmtCell(v: any) {
  if (v === null || v === undefined) return h('span', { class: 'cell-sub' }, 'NULL');
  if (typeof v === 'object') return h('span', { class: 'mono-dim', style: 'font-size:var(--fs-2xs)' }, JSON.stringify(v));
  return String(v);
}

async function loadTables() {
  curTable.value = null;
  tRows.value = [];
  tCols.value = [];
  if (!brDb.value) return;
  tablesBusy.value = true;
  try {
    tables.value = await api.pgTables(brDb.value);
  } catch (e: any) {
    tables.value = [];
    msg.error(e.message);
  } finally {
    tablesBusy.value = false;
  }
}
async function pickTable(t: any) {
  curTable.value = t;
  tPage.value = 1;
  await loadTableData();
}
async function loadTableData() {
  if (!brDb.value || !curTable.value) return;
  tBusy.value = true;
  try {
    const r = await api.pgTable(brDb.value, curTable.value.schema, curTable.value.name, tPage.value, tSize.value);
    tCols.value = r.columns || [];
    tRows.value = r.rows || [];
    tTotal.value = r.total || 0;
  } catch (e: any) {
    tRows.value = [];
    msg.error(e.message);
  } finally {
    tBusy.value = false;
  }
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
const jsonValue = computed(() => {
  const v = rDetail.value?.value;
  if (rDetail.value?.type !== 'string' || typeof v !== 'string') return '';
  try {
    return JSON.stringify(JSON.parse(v), null, 2);
  } catch {
    return '';
  }
});

async function scanKeys(more = false) {
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
async function inspectKey(k: string) {
  rDetailBusy.value = true;
  try {
    rDetail.value = await api.redisKey(k);
  } catch (e: any) {
    rDetail.value = null;
    msg.error(e.message);
  } finally {
    rDetailBusy.value = false;
  }
}
async function delKeys(keys: string[]) {
  try {
    const r = await api.redisDel(keys);
    msg.success(`已删除 ${r.deleted} 个 key`);
    rSel.value = [];
    if (rDetail.value && keys.includes(rDetail.value.key)) rDetail.value = null;
    scanKeys(false);
  } catch (e: any) {
    msg.error(e.message);
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
      <NTabs type="line" animated :tabs-padding="16" pane-style="padding: var(--space-4)">
        <NTabPane name="pg" tab="PostgreSQL">
          <NSpace vertical size="medium">
            <NSpace justify="space-between" align="center" style="width: 100%">
              <span class="section-title"><NIcon :component="icons.ServerOutline" :size="15" color="var(--cp-brand-soft)" /> 数据库列表</span>
              <NSpace :size="8">
                <NButton size="small" tertiary @click="load" :icon="ico('RefreshOutline')">刷新</NButton>
                <NButton size="small" type="primary" :icon="ico('AddOutline')" @click="showDb = true">新建数据库</NButton>
              </NSpace>
            </NSpace>
            <NDataTable size="small" :bordered="false" :columns="dbCols" :data="Array.isArray(dbs) ? dbs : []" :scroll-x="520">
              <template #empty><EmptyBox text="无法读取数据库列表（PostgreSQL 未运行？）" /></template>
            </NDataTable>

            <NSpace justify="space-between" align="center" style="width: 100%; margin-top: var(--space-2)">
              <span class="section-title"><NIcon :component="icons.KeyOutline" :size="15" color="var(--cp-warn)" /> 用户 / 角色</span>
              <NButton size="small" :icon="ico('AddOutline')" @click="showRole = true">新建用户</NButton>
            </NSpace>
            <NDataTable size="small" :bordered="false" :columns="roleCols" :data="Array.isArray(roles) ? roles : []" :max-height="320" :scroll-x="520">
              <template #empty><EmptyBox text="没有可读取的角色（PostgreSQL 未运行？）" /></template>
            </NDataTable>
          </NSpace>
        </NTabPane>

        <NTabPane name="sql" tab="SQL 控制台">
          <NSpace vertical :size="12" style="width: 100%">
            <NInput
              v-model:value="sql" type="textarea" :autosize="{ minRows: 4, maxRows: 9 }"
              placeholder="SELECT * FROM some_table LIMIT 20;"
              :input-props="{ class: 'mono', 'aria-label': 'SQL 语句', style: 'font-size: var(--fs-sm)' }"
              @keydown.ctrl.enter="runSql"
            />
            <NSpace align="center">
              <NButton type="primary" class="cp-press" :loading="sqlBusy" :icon="ico('PlayOutline')" @click="runSql">执行</NButton>
              <NText depth="3" style="font-size: var(--fs-xs)">Ctrl+Enter 快速执行 · 以 postgres 超级用户运行，请谨慎操作</NText>
            </NSpace>
            <div class="log-view" style="max-height: 380px" role="region" aria-live="polite" aria-label="SQL 执行结果">{{ sqlOut || '结果将显示在这里…' }}</div>
          </NSpace>
        </NTabPane>

        <NTabPane name="browse" tab="PG 数据浏览">
          <NSpace vertical :size="12">
            <NSpace align="center" :size="10" wrap>
              <NText depth="3" style="font-size: var(--fs-xs)">数据库</NText>
              <NSelect v-model:value="brDb" :options="dbOptions" size="small" style="width: 200px" placeholder="选择数据库" aria-label="浏览的数据库" @update:value="loadTables" />
              <NText depth="3" style="font-size: var(--fs-xs)">共 {{ tables.length }} 张表</NText>
            </NSpace>
            <NDataTable v-if="!curTable" size="small" :bordered="false" :columns="browseCols" :data="tables" :loading="tablesBusy" max-height="calc(100vh - 400px)" :scroll-x="560">
              <template #empty><EmptyBox :text="brDb ? (tablesBusy ? '加载中…' : '这个库里没有表') : '先选择一个数据库'" /></template>
            </NDataTable>
            <template v-else>
              <NSpace align="center" :size="10" wrap>
                <NButton size="tiny" tertiary :icon="ico('ArrowBackOutline')" @click="curTable = null">表列表</NButton>
                <NTag size="small" :bordered="false" type="info">{{ curTable.schema }}.{{ curTable.name }}</NTag>
                <NText depth="3" style="font-size: var(--fs-xs)">{{ tTotal }} 行 · {{ tCols.length }} 列</NText>
                <NButton size="tiny" tertiary :icon="ico('RefreshOutline')" @click="loadTableData">刷新</NButton>
                <NPagination v-model:page="tPage" :page-count="Math.max(1, Math.ceil(tTotal / tSize))" :page-size="tSize" size="small" :show-quick-jumper="false" @update:page="onPage" />
              </NSpace>
              <NDataTable size="small" :bordered="false" :columns="dataCols" :data="tRows" :loading="tBusy" max-height="calc(100vh - 430px)" :row-key="(r: any) => r" :scroll-x="Math.max(560, tCols.length * 180)">
                <template #empty><EmptyBox text="这张表没有数据" /></template>
              </NDataTable>
            </template>
          </NSpace>
        </NTabPane>

        <NTabPane name="rbr" tab="Redis 浏览器">
          <NSpace vertical :size="12">
            <NSpace align="center" :size="8" wrap>
              <NInput v-model:value="rPattern" size="small" placeholder="匹配模式，如 session:*" style="width: 220px" :input-props="{ 'aria-label': 'Redis key 匹配模式', class: 'mono' }" @keyup.enter="scanKeys(false)" />
              <NButton size="small" type="primary" :loading="rBusy" :icon="ico('SearchOutline')" @click="scanKeys(false)">扫描</NButton>
              <NButton v-if="rCursor !== '0'" size="small" tertiary :loading="rBusy" :icon="ico('RefreshCircleOutline')" @click="scanKeys(true)">加载更多</NButton>
              <NText depth="3" style="font-size: var(--fs-xs)">已加载 {{ rKeys.length }} 个 key</NText>
              <NPopconfirm v-if="rSel.length" @positive-click="delKeys(rSel)">
                <template #trigger>
                  <NButton size="small" tertiary type="error" :icon="ico('TrashOutline')">删除选中（{{ rSel.length }}）</NButton>
                </template>
                删除这 {{ rSel.length }} 个 key？UNLINK 异步回收，不可恢复。
              </NPopconfirm>
            </NSpace>
            <div class="rbr-flex">
              <div class="rk-list">
                <NDataTable
                  size="small" :bordered="false"
                  :columns="keyCols"
                  :data="rKeys.map((k) => ({ k }))"
                  :row-key="(r: any) => r.k"
                  v-model:checked-row-keys="rSel"
                  :loading="rBusy"
                  :max-height="380"
                >
                  <template #empty><EmptyBox :text="rBusy ? '扫描中…' : '没有匹配的 key（SCAN 从 0 开始，可换模式再试）'" /></template>
                </NDataTable>
              </div>
              <NCard v-if="rDetail" size="small" class="rk-detail" :title="rDetail.key">
                <template #header-extra>
                  <NSpace :size="6">
                    <NTag size="tiny" :bordered="false" type="warning">{{ rDetail.type }}</NTag>
                    <NTag size="tiny" :bordered="false">{{ rDetail.ttl === -1 ? '永不过期' : rDetail.ttl === -2 || rDetail.ttl == null ? '—' : `${rDetail.ttl}s 剩余` }}</NTag>
                    <NTag size="tiny" :bordered="false">{{ rDetail.mem == null ? '—' : `${rDetail.mem} B` }}</NTag>
                  </NSpace>
                </template>
                <NSpace vertical :size="8">
                  <NText v-if="rDetail.truncated" depth="3" style="font-size: var(--fs-xs)">内容过长，仅显示前部分（截断）</NText>
                  <pre v-if="jsonValue" class="mono-pre">{{ jsonValue }}</pre>
                  <pre v-else-if="rDetail.type === 'string'" class="mono-pre">{{ rDetail.value }}</pre>
                  <NDataTable
                    v-else-if="rDetail.type === 'hash' || rDetail.type === 'zset'"
                    size="small" :bordered="false" :max-height="280"
                    :columns="pairCols"
                    :data="rDetail.value || []" :row-key="(r: any) => r"
                  />
                  <NDataTable
                    v-else-if="Array.isArray(rDetail.value)"
                    size="small" :bordered="false" :max-height="280"
                    :columns="idxCols"
                    :data="(rDetail.value || []).map((v: any, i: number) => ({ i, v }))" :row-key="(r: any) => r.i"
                  />
                  <NText v-else depth="3" style="font-size: var(--fs-xs)">该类型暂不支持在线查看（{{ rDetail.type }}）</NText>
                  <NSpace justify="end">
                    <NPopconfirm @positive-click="delKeys([rDetail.key])">
                      <template #trigger>
                        <NButton size="tiny" tertiary type="error" :icon="ico('TrashOutline')">删除此 key</NButton>
                      </template>
                      UNLINK {{ rDetail.key }}？
                    </NPopconfirm>
                  </NSpace>
                </NSpace>
              </NCard>
              <NText v-else-if="rKeys.length" depth="3" style="font-size: var(--fs-xs); align-self: center">点击左侧 key 查看内容</NText>
            </div>
          </NSpace>
        </NTabPane>

        <NTabPane name="redis" tab="Redis">
          <EmptyBox v-if="redis?.error" :text="`Redis 连接失败：${redis.error}`" />
          <template v-else-if="redis">
            <NGrid cols="2 s:4" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
              <NGridItem span="1"><NCard size="small" class="rstat"><div class="rk">版本</div><div class="rv">{{ redis.info?.redis_version || '—' }}</div></NCard></NGridItem>
              <NGridItem span="1"><NCard size="small" class="rstat"><div class="rk">运行模式</div><div class="rv">{{ redis.info?.redis_mode || '—' }} / {{ redis.authed ? '面板密码' : 'redis.conf' }}</div></NCard></NGridItem>
              <NGridItem span="1"><NCard size="small" class="rstat"><div class="rk">连接客户端</div><div class="rv">{{ redis.info?.connected_clients ?? '—' }}</div></NCard></NGridItem>
              <NGridItem span="1"><NCard size="small" class="rstat"><div class="rk">Key 总数</div><div class="rv">{{ redis.dbsize ?? '—' }}</div></NCard></NGridItem>
              <NGridItem span="1"><NCard size="small" class="rstat"><div class="rk">内存占用</div><div class="rv">{{ redis.info?.used_memory_human || '—' }}</div></NCard></NGridItem>
              <NGridItem span="1"><NCard size="small" class="rstat"><div class="rk">运行时长</div><div class="rv">{{ Math.round((redis.info?.uptime_in_seconds || 0) / 3600) }} 小时</div></NCard></NGridItem>
              <NGridItem span="1"><NCard size="small" class="rstat"><div class="rk">累计命令</div><div class="rv">{{ redis.info?.total_commands_processed ?? '—' }}</div></NCard></NGridItem>
              <NGridItem span="1"><NCard size="small" class="rstat"><div class="rk">命中率</div><div class="rv" :style="{ color: 'var(--cp-ok)' }">{{ hitRate() }}</div></NCard></NGridItem>
            </NGrid>
            <NSpace align="center" style="margin-top: var(--space-4)">
              <NInput v-model:value="redisPass" type="password" show-password-on="click" placeholder="Redis 密码（留空保存＝清除，改读 redis.conf）" style="width: 320px" size="small" :input-props="{ 'aria-label': 'Redis 密码' }" />
              <NButton size="small" :icon="ico('SaveOutline')" @click="saveRedisPass">保存</NButton>
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
          <NButton type="primary" block class="cp-press" :icon="ico('AddOutline')" @click="createDb">创建</NButton>
        </NSpace>
      </NForm>
    </NModal>
    <NModal v-model:show="showRole" preset="card" title="新建用户" style="width: 400px; max-width: 94vw">
      <NForm label-placement="left" label-width="70">
        <NSpace vertical :size="12">
          <NFormItem label="用户名"><NInput v-model:value="roleForm.name" :input-props="{ 'aria-label': '数据库用户名' }" /></NFormItem>
          <NFormItem label="密码"><NInput v-model:value="roleForm.password" type="password" show-password-on="click" :input-props="{ 'aria-label': '数据库用户密码' }" /></NFormItem>
          <NButton type="primary" block class="cp-press" :icon="ico('AddOutline')" @click="createRole">创建</NButton>
        </NSpace>
      </NForm>
    </NModal>
    <NModal v-model:show="showPwd" preset="card" :title="`重置密码 · ${pwdTarget}`" style="width: 400px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NInput v-model:value="pwdValue" type="password" show-password-on="click" placeholder="新密码" :input-props="{ 'aria-label': '新密码' }" @keyup.enter="confirmPwd" />
        <NSpace justify="end">
          <NButton tertiary @click="showPwd = false">取消</NButton>
          <NButton type="primary" class="cp-press" :icon="ico('SaveOutline')" @click="confirmPwd">确认修改</NButton>
        </NSpace>
      </NSpace>
    </NModal>
  </div>
</template>

<style scoped>
.rstat :deep(.n-card__content) { padding: var(--space-3) var(--space-4); }
.rk { font-size: var(--fs-2xs); color: var(--cp-text-mute); margin-bottom: var(--space-1); }
.rv { font-size: var(--fs-lg); font-weight: 600; color: var(--cp-text); }
.mono-pre {
  margin: 0; padding: var(--space-3); max-height: 300px; overflow: auto;
  background: var(--cp-bg-soft, rgba(128, 128, 128, 0.08));
  border: 1px solid var(--cp-border, rgba(128, 128, 128, 0.18));
  border-radius: var(--radius-md, 8px);
  font-family: var(--font-mono, ui-monospace, monospace); font-size: var(--fs-xs);
  white-space: pre-wrap; word-break: break-all; color: var(--cp-text);
}
/* 窄容器（侧栏展开/窗口收窄）下 key 列表与详情上下堆叠，避免横向溢出裁切 */
.rbr-flex { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-start; }
.rk-list { flex: 1 1 320px; min-width: 0; }
.rk-detail { flex: 1 1 340px; min-width: 0; }
</style>
