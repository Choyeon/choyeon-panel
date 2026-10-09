<script setup lang="ts">
import { computed, onMounted, ref, watch, h } from 'vue';
import {
  NCard, NText, NSpace, useMessage, NInput, NForm, NFormItem,
  NInputNumber, NIcon, NGrid, NGridItem, NTooltip, NSwitch,
} from 'naive-ui';
import { NButton } from '../ui';
import { api, getRole } from '../api';
import { icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const isAdmin = computed(() => getRole() === 'admin');
const renewing = ref(false);
const renewLog = ref('');
// 阈值类配置在库里可能根本没写过（值为空串），巡检侧用的是代码默认值。
// 这里把空值显示成那个默认值，界面和真实生效的阈值才是一致的；保存后就是显式值。
const ALERT_DEFAULTS: Record<string, string> = {
  alert_disk_pct: '85', alert_ssl_days: '14', alert_cpu_pct: '80', alert_mem_pct: '90',
  alert_load1_x: '2', alert_keep_days: '30', alert_keep_rows: '2000', alert_probe: '1',
};
const NUM_ALERT_KEYS = ['alert_disk_pct', 'alert_ssl_days', 'alert_cpu_pct', 'alert_mem_pct',
                        'alert_load1_x', 'alert_keep_days', 'alert_keep_rows'];

function normalizeAlerts(v: any) {
  const out = { ...v };
  Object.entries(ALERT_DEFAULTS).forEach(([k, def]) => {
    if (out[k] === undefined || out[k] === null || out[k] === '') out[k] = def;
  });
  // NInputNumber 只认 number；库里存的是字符串，直接绑会渲染成空框，用户以为没配置。
  NUM_ALERT_KEYS.forEach((k) => { out[k] = Number(out[k]); });
  return out;
}

const alerts = ref<any>(normalizeAlerts({}));
const fw = ref<any>(null);
const probeList = ref<{ name: string; expect: string }[]>([]);
const probeDraft = ref({ name: '', expect: '' });

watch(alerts, (v) => {
  probeList.value = Object.entries(v?.probe_overrides || {}).map(([name, expect]) => ({ name, expect: String(expect) }));
});

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

async function grab(fn: () => Promise<any>, set: (v: any) => void) {
  // 每个数据块各自兜错：以前是一条链，只读账号在第一步就收到 403，
  // 于是后面的防火墙状态根本没被请求，卡片永远停在"检测中…"。
  try {
    set(await fn());
  } catch (e: any) {
    msg.error(e.message);
  }
}

async function load() {
  if (isAdmin.value) await grab(() => api.alertSettings(), (v) => (alerts.value = normalizeAlerts(v)));
  await grab(() => api.firewall(), (v) => (fw.value = v));
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
async function saveAlerts() {
  try {
    await api.saveAlerts(alerts.value);
    msg.success('告警配置已保存');
  } catch (e: any) {
    msg.error(e.message);
  }
}

// 探活期望状态码按应用覆写：键名 probe_expect:<应用名>，服务端会核对应用是否存在，
// 所以这里不做本地白名单，写错了会被 400 挡回来并显示原因。
async function saveProbeRow(entry: { name: string; expect: string }) {
  try {
    await api.saveAlerts({ [`probe_expect:${entry.name}`]: (entry.expect || '').trim() });
    msg.success(`已保存 ${entry.name} 的期望状态码`);
    await load();
  } catch (e: any) {
    msg.error(e.message);
  }
}

async function addProbeRow() {
  const name = probeDraft.value.name.trim();
  if (!name) { msg.warning('先填应用名'); return; }
  if (probeList.value.some((r) => r.name === name)) { msg.warning('这个应用已经有覆写了，直接改上面那行'); return; }
  await saveProbeRow({ name, expect: probeDraft.value.expect });
  probeDraft.value = { name: '', expect: '' };
}

async function dropProbeRow(name: string) {
  // 空值就是"取消覆写"：服务端存空串，巡检侧回落全局期望，不为删一行配置新增端点。
  await saveProbeRow({ name, expect: '' });
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

onMounted(load);
</script>

<template>
  <div>
    <PageHeader title="系统设置" sub="告警通道、防火墙状态与证书维护；用户与审计已升级为独立页面">
      <template #actions>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <NGrid cols="1 s:2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <NGridItem v-if="isAdmin" span="1 s:2">
        <NCard size="small">
          <template #header><span class="section-title"><NIcon :component="icons.AlertCircleOutline" :size="15" color="var(--cp-warn)" /> 告警通知</span></template>
          <NText depth="3" style="font-size: var(--fs-xs); display: block; margin-bottom: var(--space-3)">每 30 分钟自动巡检：磁盘、CPU、内存、负载、服务 failed、SSL 剩余天数，以及逐个应用的 HTTP 探活（只连 127.0.0.1 的端口，看它有没有应答）。触发后经 Telegram Bot 或 Webhook 推送，同类告警每天最多一次；资源类阈值要连续两轮超标才推送，历史里当轮就留痕。</NText>
          <NForm label-placement="top" size="small">
            <NFormItem label="Telegram Bot Token"><NInput v-model:value="alerts.alert_telegram_bot" placeholder="如 123456:ABC-DEF…" :input-props="{ 'aria-label': 'Telegram Bot Token' }" /></NFormItem>
            <NFormItem label="Telegram Chat ID"><NInput v-model:value="alerts.alert_telegram_chat" placeholder="如 987654321" :input-props="{ 'aria-label': 'Telegram Chat ID' }" /></NFormItem>
            <NFormItem label="通用 Webhook URL"><NInput v-model:value="alerts.alert_webhook_url" placeholder="POST {text}，兼容主流机器人网关" :input-props="{ 'aria-label': '通用 Webhook URL' }" /></NFormItem>
            <NSpace :size="16" wrap>
              <NFormItem label="磁盘阈值 %" style="margin-bottom: 0"><NInputNumber v-model:value="alerts.alert_disk_pct" :min="50" :max="99" :input-props="{ 'aria-label': '磁盘使用率阈值百分比' }" style="width: 118px" /></NFormItem>
              <NFormItem label="SSL 剩余天数" style="margin-bottom: 0"><NInputNumber v-model:value="alerts.alert_ssl_days" :min="0" :max="60" :input-props="{ 'aria-label': 'SSL 剩余天数阈值' }" style="width: 118px" /></NFormItem>
              <NFormItem label="CPU 阈值 %" style="margin-bottom: 0"><NInputNumber v-model:value="alerts.alert_cpu_pct" :min="1" :max="99" :input-props="{ 'aria-label': 'CPU 使用率阈值百分比' }" style="width: 118px" /></NFormItem>
              <NFormItem label="内存阈值 %" style="margin-bottom: 0"><NInputNumber v-model:value="alerts.alert_mem_pct" :min="1" :max="99" :input-props="{ 'aria-label': '内存使用率阈值百分比' }" style="width: 118px" /></NFormItem>
              <NFormItem label="load1 ×核数" style="margin-bottom: 0"><NInputNumber v-model:value="alerts.alert_load1_x" :min="1" :max="32" :input-props="{ 'aria-label': '一分钟负载阈值倍数' }" style="width: 118px" /></NFormItem>
            </NSpace>
            <NSpace :size="16" wrap align="end" style="margin-top: var(--space-3)">
              <NFormItem label="应用探活" style="margin-bottom: 0">
                <NSwitch
                  :value="String(alerts.alert_probe) !== '0'" size="small" aria-label="是否启用应用 HTTP 探活"
                  @update:value="(v: boolean) => { alerts.alert_probe = v ? '1' : '0'; }"
                />
              </NFormItem>
              <NFormItem label="期望状态码" style="margin-bottom: 0">
                <NInput v-model:value="alerts.alert_probe_expect" placeholder="2xx/3xx" style="width: 130px" :input-props="{ 'aria-label': '全局探活期望状态码' }" />
              </NFormItem>
              <NFormItem label="历史保留天数" style="margin-bottom: 0"><NInputNumber v-model:value="alerts.alert_keep_days" :min="1" :max="365" :input-props="{ 'aria-label': '告警历史保留天数' }" style="width: 118px" /></NFormItem>
              <NFormItem label="历史上限行" style="margin-bottom: 0"><NInputNumber v-model:value="alerts.alert_keep_rows" :min="100" :max="20000" :step="100" :input-props="{ 'aria-label': '告警历史最多保留行数' }" style="width: 118px" /></NFormItem>
            </NSpace>
            <NFormItem label="按应用覆写期望状态码" style="margin-top: var(--space-3); margin-bottom: 0">
              <NSpace vertical :size="6" style="width: 100%">
                <NText depth="3" style="font-size: var(--fs-2xs)">探活只看端口是否应答：连不上或超时才算 crit，4xx/5xx 说明进程还在。覆写值留空即回落到上面的全局期望。</NText>
                <NSpace v-for="row in probeList" :key="row.name" align="center" :size="8">
                  <span class="mono-dim cp-break" style="min-width: 148px">{{ row.name }}</span>
                  <NInput :value="row.expect" size="tiny" style="width: 132px" placeholder="200 204" :input-props="{ 'aria-label': `${row.name} 的期望状态码` }" @update:value="(v: string) => { row.expect = v; }" />
                  <NButton size="tiny" tertiary :icon="ico('SaveOutline', 12)" @click="saveProbeRow(row)">保存</NButton>
                  <NButton size="tiny" tertiary :icon="ico('TrashOutline', 12)" aria-label="删除该覆写" @click="dropProbeRow(row.name)">删除</NButton>
                </NSpace>
                <EmptyBox v-if="!probeList.length" text="还没有按应用覆写，默认全部走全局期望状态码" />
                <NSpace align="center" :size="8">
                  <NInput v-model:value="probeDraft.name" size="tiny" style="width: 148px" placeholder="应用名" :input-props="{ 'aria-label': '新增覆写的应用名' }" @keyup.enter="addProbeRow" />
                  <NInput v-model:value="probeDraft.expect" size="tiny" style="width: 132px" placeholder="200 204" :input-props="{ 'aria-label': '新增覆写的期望状态码' }" @keyup.enter="addProbeRow" />
                  <NButton size="tiny" tertiary :icon="ico('AddOutline', 12)" @click="addProbeRow">新增覆写</NButton>
                </NSpace>
              </NSpace>
            </NFormItem>
          </NForm>
          <NSpace style="margin-top: var(--space-3)">
            <NButton type="primary" size="small" :icon="ico('SaveOutline')" @click="saveAlerts">保存配置</NButton>
            <NButton tertiary size="small" :icon="ico('CloudUploadOutline')" @click="testAlert">发送测试通知</NButton>
            <NButton tertiary size="small" :icon="ico('PulseOutline')" @click="runChecks">立即巡检</NButton>
          </NSpace>
        </NCard>
      </NGridItem>

      <NGridItem span="1 s:2 m:1">
        <NCard size="small">
          <template #header><span class="section-title"><NIcon :component="icons.ShieldCheckmarkOutline" :size="15" color="var(--cp-ok)" /> 防火墙</span></template>
          <NSpace vertical :size="12">
            <NSpace align="center" :size="8">
              <span class="st" :class="fw?.active ? 'ok' : 'err'"><span class="dot" :class="fw?.active ? 'ok' : 'err'"></span>{{ fw ? (fw.active ? 'ufw 已启用' : 'ufw 未启用') : '检测中…' }}</span>
              <NText depth="3" style="font-size: var(--fs-xs)">面板对防火墙只读；开启/改规则请 SSH 手动操作，以免误锁 SSH。</NText>
            </NSpace>
            <div v-if="fw?.output" class="log-view" style="max-height: 220px">{{ fw.output }}</div>
            <EmptyBox v-else-if="fw" text="ufw 未返回规则输出：未安装时属正常，规则请用 SSH 手动维护" />
          </NSpace>
        </NCard>
      </NGridItem>

      <NGridItem v-if="isAdmin" span="1 s:2 m:1">
        <NCard size="small">
          <template #header><span class="section-title"><NIcon :component="icons.KeyOutline" :size="15" color="var(--cp-brand-soft)" /> 证书维护</span></template>
          <NSpace vertical :size="12">
            <NText depth="3" style="font-size: var(--fs-xs)">certbot renew 检查所有已安装证书并自动续期。是否真的有人定时跑它，看安全自检页的「证书续期」一项（它会去查 certbot.timer 与 crontab，查不到就如实标 warn）；这里只用于手动触发验证。</NText>
            <NButton type="primary" size="small" :loading="renewing" :icon="ico('SyncOutline')" style="align-self: flex-start" @click="renew">立即续期</NButton>
            <div v-if="renewLog" class="log-view" style="max-height: 220px">{{ renewLog }}</div>
            <EmptyBox v-else text="本次会话还没有执行过续期，点击上方按钮后即可看到 certbot 输出" />
          </NSpace>
        </NCard>
      </NGridItem>

      <NGridItem span="1 s:2">
        <NCard size="small">
          <template #header><span class="section-title"><NIcon :component="icons.LayersOutline" :size="15" color="var(--cp-text-mute)" /> 管理入口</span></template>
          <NSpace :size="10" wrap>
            <NTooltip v-if="isAdmin" trigger="hover">
              <template #trigger>
                <NButton size="small" tertiary :icon="ico('PeopleOutline')" @click="$router.push('/users')">用户管理</NButton>
              </template>
              账号、角色与密码
            </NTooltip>
            <NTooltip v-if="isAdmin" trigger="hover">
              <template #trigger>
                <NButton size="small" tertiary :icon="ico('GitNetworkOutline')" @click="$router.push('/audit')">操作审计</NButton>
              </template>
              所有写操作留痕
            </NTooltip>
            <NTooltip trigger="hover">
              <template #trigger>
                <NButton size="small" tertiary :icon="ico('CodeSlashOutline')" @click="$router.push('/logs')">面板日志</NButton>
              </template>
              journalctl 实时流
            </NTooltip>
          </NSpace>
        </NCard>
      </NGridItem>
    </NGrid>
  </div>
</template>
