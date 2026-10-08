<script setup lang="ts">
import { computed, onMounted, ref, h } from 'vue';
import {
  NCard, NText, NSpace, NDataTable, NButton, useMessage, NInput, NModal, NForm, NFormItem,
  NSelect, NPopconfirm, NTag, NInputNumber, NIcon, NGrid, NGridItem,
} from 'naive-ui';
import { api, getRole } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const isAdmin = computed(() => getRole() === 'admin');
const auditLog = ref<any[]>([]);
const renewing = ref(false);
const renewLog = ref('');

const users = ref<any[]>([]);
const showUser = ref(false);
const userForm = ref({ username: '', password: '', role: 'viewer' });
const showReset = ref(false);
const resetTarget = ref<any>(null);
const resetPwd = ref('');

const alerts = ref<any>({ alert_telegram_bot: '', alert_telegram_chat: '', alert_webhook_url: '', alert_disk_pct: '85', alert_ssl_days: '14' });
const fw = ref<any>(null);

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

async function grab(fn: () => Promise<any>, set: (v: any) => void) {
  // 每个数据块各自兜错：以前是一条链，只读账号在第一步 /api/audit 就收到 403，
  // 于是后面的防火墙状态根本没被请求，卡片永远停在"检测中…"。
  try {
    set(await fn());
  } catch (e: any) {
    msg.error(e.message);
  }
}

const loading = ref(false);

async function load() {
  loading.value = true;
  if (isAdmin.value) {
    await grab(() => api.audit(100), (v) => (auditLog.value = v));
    await grab(() => api.users(), (v) => (users.value = v));
    await grab(() => api.alertSettings(), (v) => (alerts.value = v));
  }
  await grab(() => api.firewall(), (v) => (fw.value = v));
  loading.value = false;
}
async function renew() {
  renewing.value = true;
  try {
    const r = await api.certbotRenew();
    renewLog.value = r.log;
    msg.success('certbot 执行完成');
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    renewing.value = false;
  }
}
async function createUser() {
  if (!userForm.value.username || userForm.value.password.length < 8) return msg.error('用户名必填，密码至少 8 位');
  try {
    await api.createUser(userForm.value.username, userForm.value.password, userForm.value.role);
    msg.success('已创建');
    showUser.value = false;
    userForm.value = { username: '', password: '', role: 'viewer' };
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function toggleRole(u: any) {
  try {
    await api.updateUser(u.id, { role: u.role === 'admin' ? 'viewer' : 'admin' });
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}
function openReset(u: any) {
  resetTarget.value = u;
  resetPwd.value = '';
  showReset.value = true;
}
async function confirmReset() {
  if (resetPwd.value.length < 8) return msg.error('密码至少 8 位');
  try {
    await api.updateUser(resetTarget.value.id, { password: resetPwd.value });
    msg.success('密码已重置');
    showReset.value = false;
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function delUser(u: any) {
  try {
    await api.deleteUser(u.id);
    load();
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function saveAlerts() {
  try {
    await api.saveAlerts(alerts.value);
    msg.success('告警配置已保存');
  } catch (e: any) {
    msg.error(e.message);
  }
}
async function runChecks() {
  try {
    await api.alertRunChecks();
    msg.success('巡检已执行，若触发阈值会立即推送');
  } catch (e: any) {
    msg.error(e.message);
  }
}

async function testAlert() {
  try {
    await api.saveAlerts(alerts.value);
    await api.alertTest();
    msg.success('测试通知已发送（若未收到请检查配置/通道）');
  } catch (e: any) {
    msg.error(e.message);
  }
}

const userCols: any[] = [
  {
    title: '用户', key: 'username',
    render: (u: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: u.role === 'admin' ? icons.ShieldCheckmarkOutline : icons.OptionsOutline, size: 17, color: u.role === 'admin' ? 'var(--cp-warn)' : 'var(--cp-text-mute)' }),
        ]),
        h('div', { class: 'cell-txt' }, [h('span', { class: 'cell-name' }, u.username), h('div', { class: 'cell-sub' }, u.created_at || '')]),
      ]),
  },
  { title: '角色', key: 'role', width: 100, render: (u: any) => h('span', { class: 'vchip' + (u.role === 'admin' ? ' node' : '') }, u.role === 'admin' ? '管理员' : '只读') },
  {
    title: '操作', key: 'ops', width: 260,
    render: (u: any) =>
      h(NSpace, { size: 6 }, () => [
        h(NButton, { size: 'tiny', tertiary: true, icon: ico('SyncOutline', 12), onClick: () => toggleRole(u) }, () => (u.role === 'admin' ? '降为只读' : '升为管理')),
        h(NButton, { size: 'tiny', tertiary: true, icon: ico('KeyOutline', 12), onClick: () => openReset(u) }, () => '重置密码'),
        h(NPopconfirm, { onPositiveClick: () => delUser(u) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: ico('TrashOutline', 12), title: `删除用户 ${u.username}`, 'aria-label': `删除用户 ${u.username}` }, { default: () => '' }),
          default: () => `删除用户 ${u.username}？`,
        }),
      ]),
  },
];

const auditCols: any[] = [
  { title: '时间 (UTC)', key: 'created_at', width: 170, render: (r: any) => h('span', { class: 'mono-dim' }, r.created_at) },
  { title: '用户', key: 'username', width: 110 },
  { title: '操作', key: 'action', width: 170, render: (r: any) => h(NTag, { size: 'tiny', bordered: false, type: /delete|stop|purge/i.test(r.action) ? 'error' : /deploy|start|create|save|apply/i.test(r.action) ? 'success' : 'default' }, () => r.action) },
  { title: '详情', key: 'detail', ellipsis: { tooltip: true } },
];

onMounted(load);
</script>

<template>
  <div>
    <PageHeader title="系统设置" sub="用户与权限、告警通道、防火墙状态、证书维护与操作审计">
      <template #actions>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <NGrid :cols="2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem v-if="isAdmin" span="2 1:2">
        <NCard size="small">
          <template #header><span class="section-title"><NIcon :component="icons.OptionsOutline" :size="15" color="var(--cp-brand-soft)" /> 用户管理</span></template>
          <template #header-extra><NButton size="tiny" type="primary" :icon="ico('AddOutline', 12)" @click="showUser = true">新建用户</NButton></template>
          <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-2)">只读账号可查看全部监控/日志/数据，但不能执行启停、部署、删除、终端等任何写操作。</NText>
          <NDataTable size="small" :bordered="false" :loading="loading" :scroll-x="620" :columns="userCols" :data="users">
            <template #empty><EmptyBox text="暂无用户" /></template>
          </NDataTable>
        </NCard>
      </NGridItem>

      <NGridItem v-if="isAdmin" span="2 1:2">
        <NCard size="small">
          <template #header><span class="section-title"><NIcon :component="icons.AlertCircleOutline" :size="15" color="var(--cp-warn)" /> 告警通知</span></template>
          <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-3)">每 30 分钟自动巡检：磁盘使用率、服务 failed、SSL 剩余天数。触发后经 Telegram Bot 或 Webhook 推送，同类告警每天最多一次。</NText>
          <NForm label-placement="top" size="small">
            <NFormItem label="Telegram Bot Token"><NInput v-model:value="alerts.alert_telegram_bot" placeholder="如 123456:ABC-DEF…" :input-props="{ 'aria-label': 'Telegram Bot Token' }" /></NFormItem>
            <NFormItem label="Telegram Chat ID"><NInput v-model:value="alerts.alert_telegram_chat" placeholder="如 987654321" :input-props="{ 'aria-label': 'Telegram Chat ID' }" /></NFormItem>
            <NFormItem label="通用 Webhook URL"><NInput v-model:value="alerts.alert_webhook_url" placeholder="POST {text}，兼容主流机器人网关" :input-props="{ 'aria-label': '通用 Webhook URL' }" /></NFormItem>
            <NSpace :size="16">
              <NFormItem label="磁盘阈值 %" style="margin-bottom: 0"><NInputNumber v-model:value="alerts.alert_disk_pct" :min="50" :max="99" :input-props="{ 'aria-label': '磁盘使用率阈值百分比' }" style="width: 130px" /></NFormItem>
              <NFormItem label="SSL 剩余天数" style="margin-bottom: 0"><NInputNumber v-model:value="alerts.alert_ssl_days" :min="0" :max="60" :input-props="{ 'aria-label': 'SSL 剩余天数阈值' }" style="width: 130px" /></NFormItem>
            </NSpace>
          </NForm>
          <NSpace style="margin-top: var(--space-3)">
            <NButton type="primary" size="small" :icon="ico('SaveOutline')" @click="saveAlerts">保存配置</NButton>
            <NButton tertiary size="small" :icon="ico('CloudUploadOutline')" @click="testAlert">发送测试通知</NButton>
            <NButton tertiary size="small" :icon="ico('PulseOutline')" @click="runChecks">立即巡检</NButton>
          </NSpace>
        </NCard>
      </NGridItem>

      <NGridItem span="2 1:1">
        <NCard size="small">
          <template #header><span class="section-title"><NIcon :component="icons.ShieldCheckmarkOutline" :size="15" color="var(--cp-ok)" /> 防火墙</span></template>
          <NSpace vertical :size="12">
            <NSpace align="center" :size="8">
              <span class="st" :class="fw?.active ? 'ok' : 'err'"><span class="dot" :class="fw?.active ? 'ok' : 'err'"></span>{{ fw ? (fw.active ? 'ufw 已启用' : 'ufw 未启用') : '检测中…' }}</span>
              <NText depth="3" style="font-size: var(--fs-xs)">面板对防火墙只读；开启/改规则请 SSH 手动操作，以免误锁 SSH。</NText>
            </NSpace>
            <div v-if="fw?.output" class="log-view" style="max-height: 220px">{{ fw.output }}</div>
          </NSpace>
        </NCard>
      </NGridItem>

      <NGridItem v-if="isAdmin" span="2 1:1">
        <NCard size="small">
          <template #header><span class="section-title"><NIcon :component="icons.KeyOutline" :size="15" color="var(--cp-brand-soft)" /> 证书维护</span></template>
          <NSpace vertical :size="12">
            <NText depth="3" style="font-size: var(--fs-xs)">certbot renew 检查所有已安装证书并自动续期（系统每日定时任务已托管，此处用于手动触发验证）。</NText>
            <NButton type="primary" size="small" :loading="renewing" :icon="ico('SyncOutline')" style="align-self: flex-start" @click="renew">立即续期</NButton>
            <div v-if="renewLog" class="log-view" style="max-height: 220px">{{ renewLog }}</div>
          </NSpace>
        </NCard>
      </NGridItem>

      <NGridItem v-if="isAdmin" span="2">
        <NCard size="small">
          <template #header><span class="section-title"><NIcon :component="icons.DocumentTextOutline" :size="15" color="var(--cp-text-mute)" /> 操作审计</span></template>
          <template #header-extra><NButton size="tiny" tertiary :icon="ico('RefreshOutline', 12)" @click="load">刷新</NButton></template>
          <NDataTable size="small" :bordered="false" :scroll-x="760" :max-height="380" :columns="auditCols" :data="auditLog">
            <template #empty><EmptyBox text="暂无操作记录" /></template>
          </NDataTable>
        </NCard>
      </NGridItem>
    </NGrid>

    <NModal v-model:show="showUser" preset="card" title="新建用户" style="width: 400px; max-width: 94vw">
      <NForm label-placement="left" label-width="70">
        <NSpace vertical :size="12">
          <NFormItem label="用户名"><NInput v-model:value="userForm.username" :input-props="{ 'aria-label': '用户名' }" /></NFormItem>
          <NFormItem label="密码"><NInput v-model:value="userForm.password" type="password" show-password-on="click" placeholder="至少 8 位" :input-props="{ 'aria-label': '密码' }" /></NFormItem>
          <NFormItem label="角色">
            <NSelect v-model:value="userForm.role" :options="[{ label: '只读 viewer', value: 'viewer' }, { label: '管理员 admin', value: 'admin' }]" aria-label="用户角色" />
          </NFormItem>
          <NButton type="primary" block class="cp-press" :icon="ico('AddOutline')" @click="createUser">创建</NButton>
        </NSpace>
      </NForm>
    </NModal>

    <NModal v-model:show="showReset" preset="card" :title="`重置密码 · ${resetTarget?.username || ''}`" style="width: 400px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NInput v-model:value="resetPwd" type="password" show-password-on="click" placeholder="新密码（至少 8 位）" :input-props="{ 'aria-label': '新密码' }" @keyup.enter="confirmReset" />
        <NSpace justify="end">
          <NButton tertiary @click="showReset = false">取消</NButton>
          <NButton type="primary" class="cp-press" :icon="ico('SaveOutline')" @click="confirmReset">确认重置</NButton>
        </NSpace>
      </NSpace>
    </NModal>
  </div>
</template>
