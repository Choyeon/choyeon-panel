<script setup lang="ts">
import { computed, onMounted, ref, h } from 'vue';
import {
  NCard, NText, NSpace, useMessage, NInput, NForm, NFormItem,
  NInputNumber, NIcon, NGrid, NGridItem, NTooltip,
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
const alerts = ref<any>({ alert_telegram_bot: '', alert_telegram_chat: '', alert_webhook_url: '', alert_disk_pct: '85', alert_ssl_days: '14' });
const fw = ref<any>(null);

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
  if (isAdmin.value) await grab(() => api.alertSettings(), (v) => (alerts.value = v));
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
            <NText depth="3" style="font-size: var(--fs-xs)">certbot renew 检查所有已安装证书并自动续期（系统每日定时任务已托管，此处用于手动触发验证）。</NText>
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
