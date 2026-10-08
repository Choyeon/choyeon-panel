<script setup lang="ts">
import { onMounted, ref, h } from 'vue';
import {
  NTabs, NTabPane, NDataTable, NButton, NSpace, NModal, NInput, NForm, NFormItem,
  NPopconfirm, NCard, NText, NTag, useMessage, NIcon, NGrid, NGridItem, NSkeleton,
} from 'naive-ui';
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
  { title: '大小', key: 'size', width: 100, render: (r: any) => h('span', { class: 'mono-dim' }, r.size) },
  {
    title: '连接数', key: 'conns', width: 90,
    render: (r: any) => h('span', { class: 'vchip' }, `${r.conns} conn`),
  },
  {
    title: '操作', key: 'ops', width: 80,
    render: (r: any) =>
      h(NPopconfirm, { onPositiveClick: () => manage('dropDb', r.name) }, {
        trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline'), title: `删除数据库 ${r.name}`, 'aria-label': `删除数据库 ${r.name}` }, { default: () => '' }),
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
  { title: '登录', key: 'login', width: 90, render: (r: any) => h('span', { class: 'st ' + (r.login ? 'ok' : 'idle') }, [h('span', { class: 'dot ' + (r.login ? 'ok' : 'idle') }), r.login ? '可登录' : '仅组']) },
  { title: 'Superuser', key: 'super', width: 100, render: (r: any) => (r.super ? h(NTag, { size: 'tiny', bordered: false, type: 'warning' }, () => 'SUPER') : h('span', { class: 'cell-sub' }, '—')) },
  {
    title: '操作', key: 'ops', width: 150,
    render: (r: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, icon: ico('PencilOutline'), onClick: () => openPwd(r.name) }, () => '改密'),
        h(NPopconfirm, { onPositiveClick: () => manage('dropRole', r.name) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline'), title: `删除角色 ${r.name}`, 'aria-label': `删除角色 ${r.name}` }, { default: () => '' }),
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
</script>

<template>
  <div>
    <PageHeader title="数据库" sub="PostgreSQL 与 Redis 的库、角色、密码与轻量 SQL 控制台">
      <template #actions>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <NCard size="small" :content-style="{ padding: '0' }">
      <NTabs type="line" animated pane-style="padding: var(--space-4)">
        <NTabPane name="pg" tab="PostgreSQL">
          <NSpace vertical size="medium">
            <NSpace justify="space-between" align="center" style="width: 100%">
              <span class="section-title"><NIcon :component="icons.ServerOutline" :size="15" color="var(--cp-brand-soft)" /> 数据库列表</span>
              <NSpace :size="8">
                <NButton size="small" tertiary @click="load" :icon="ico('RefreshOutline')">刷新</NButton>
                <NButton size="small" type="primary" :icon="ico('AddOutline')" @click="showDb = true">新建数据库</NButton>
              </NSpace>
            </NSpace>
            <NDataTable size="small" :bordered="false" :scroll-x="640" :columns="dbCols" :data="Array.isArray(dbs) ? dbs : []">
              <template #empty><EmptyBox text="无法读取数据库列表（PostgreSQL 未运行？）" /></template>
            </NDataTable>

            <NSpace justify="space-between" align="center" style="width: 100%; margin-top: var(--space-2)">
              <span class="section-title"><NIcon :component="icons.KeyOutline" :size="15" color="var(--cp-warn)" /> 用户 / 角色</span>
              <NButton size="small" :icon="ico('AddOutline')" @click="showRole = true">新建用户</NButton>
            </NSpace>
            <NDataTable size="small" :bordered="false" :scroll-x="620" :columns="roleCols" :data="Array.isArray(roles) ? roles : []" :max-height="320" />
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

        <NTabPane name="redis" tab="Redis">
          <NText v-if="redis?.error" depth="3">{{ redis.error }}</NText>
          <template v-else-if="redis">
            <NGrid :cols="4" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
              <NGridItem span="4 2:2 1:1"><NCard size="small" class="rstat"><div class="rk">版本</div><div class="rv">{{ redis.info?.redis_version || '—' }}</div></NCard></NGridItem>
              <NGridItem span="4 2:2 1:1"><NCard size="small" class="rstat"><div class="rk">运行模式</div><div class="rv">{{ redis.info?.redis_mode || '—' }} / {{ redis.authed ? '面板密码' : 'redis.conf' }}</div></NCard></NGridItem>
              <NGridItem span="4 2:2 1:1"><NCard size="small" class="rstat"><div class="rk">连接客户端</div><div class="rv">{{ redis.info?.connected_clients ?? '—' }}</div></NCard></NGridItem>
              <NGridItem span="4 2:2 1:1"><NCard size="small" class="rstat"><div class="rk">Key 总数</div><div class="rv">{{ redis.dbsize ?? '—' }}</div></NCard></NGridItem>
              <NGridItem span="4 2:2 1:1"><NCard size="small" class="rstat"><div class="rk">内存占用</div><div class="rv">{{ redis.info?.used_memory_human || '—' }}</div></NCard></NGridItem>
              <NGridItem span="4 2:2 1:1"><NCard size="small" class="rstat"><div class="rk">运行时长</div><div class="rv">{{ Math.round((redis.info?.uptime_in_seconds || 0) / 3600) }} 小时</div></NCard></NGridItem>
              <NGridItem span="4 2:2 1:1"><NCard size="small" class="rstat"><div class="rk">累计命令</div><div class="rv">{{ redis.info?.total_commands_processed ?? '—' }}</div></NCard></NGridItem>
              <NGridItem span="4 2:2 1:1"><NCard size="small" class="rstat"><div class="rk">命中率</div><div class="rv" :style="{ color: 'var(--cp-ok)' }">{{ hitRate() }}</div></NCard></NGridItem>
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
</style>
