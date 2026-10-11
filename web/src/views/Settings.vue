<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  NSpace, useMessage, NInput, NForm, NFormItem,
  NInputNumber, NSwitch, NDrawer, NDrawerContent, NText,
} from 'naive-ui';
import { useRoute, useRouter } from 'vue-router';
import { NButton } from '../ui';
import { api, getRole } from '../api';
import { icon } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import PageTabBar from '../components/PageTabBar.vue';
import CpToolbarCard from '../components/CpToolbarCard.vue';
import CpSettingRow from '../components/CpSettingRow.vue';
import EmptyBox from '../components/EmptyBox.vue';

const msg = useMessage();
const route = useRoute();
const router = useRouter();
const isAdmin = computed(() => getRole() === 'admin');
const renewing = ref(false);
const renewLog = ref('');
const renewSummary = ref('');
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
  // 空值在服务端就是「没有覆写」（_app_expectation 用 `or` 回落全局期望），
  // 所以列表里也不该出现空行：点删除后那一行必须真的消失，否则看着像没删掉。
  probeList.value = Object.entries(v?.probe_overrides || {})
    .filter(([, expect]) => String(expect).trim())
    .map(([name, expect]) => ({ name, expect: String(expect) }));
});

const ico = (name: string, size = 14) => icon(name, size);

// 1Panel 设置页 <640px 时 label 从左侧改到顶上
const mq = window.matchMedia('(max-width: 640px)');
const isMobile = ref(mq.matches);
const onMq = (e: MediaQueryListEvent) => { isMobile.value = e.matches; };

const navTabs = computed(() => {
  const t = [{ key: '/settings', label: '系统设置' }];
  if (isAdmin.value) t.push({ key: '/users', label: '用户管理' }, { key: '/audit', label: '操作审计' }, { key: '/logs', label: '面板日志' });
  return t;
});

// 敏感值只显示前 4 位，完整值只能在抽屉里看/改
function mask(v: any) {
  const s = String(v ?? '').trim();
  return s ? `${s.slice(0, 4)}****` : '';
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
  // 防火墙规则表（开了哪些端口、来源地址）按 admin 收口，与后端 /api/firewall 同一口径；
  // 少了这个判断，只读账号一进设置页就吃到一条 403 报错。
  if (isAdmin.value) await grab(() => api.firewall(), (v) => (fw.value = v));
}
async function renew() {
  renewing.value = true;
  try {
    const r = await api.certbotRenew();
    renewLog.value = r.log;
    // 「执行完成」不等于「续到了」：certbot 对没到期的证书同样成功退出，
    // 结论由后端按到期日实际变化算出来，界面照它说，不要自己下判断。
    renewSummary.value = r.summary || '';
    if (r.reloaded === false && r.renewed?.length) msg.warning(`${r.summary}（nginx 重载失败，请手动 reload）`);
    else msg.success(r.summary || 'certbot 已执行');
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    renewing.value = false;
  }
}
// 保存即生效：单项失败把显示值回滚，不留一个"看起来存上了"的假状态
async function saveOne(key: string, value: string, rollback: any) {
  try {
    await api.saveAlerts({ [key]: value });
    msg.success('已保存');
  } catch (e: any) {
    alerts.value[key] = rollback;
    msg.error(e.message);
  }
}
async function commitNum(key: string, v: number | null) {
  if (v === null || Number.isNaN(v)) return;
  const prev = alerts.value[key];
  alerts.value[key] = v;
  await saveOne(key, String(v), prev);
}
async function commitProbe(v: boolean) {
  const prev = alerts.value.alert_probe;
  alerts.value.alert_probe = v ? '1' : '0';
  await saveOne('alert_probe', alerts.value.alert_probe, prev);
}
// 文本项走 change（失焦/回车提交），避免逐字符打后端
async function commitExpect(v: string) {
  const prev = alerts.value.alert_probe_expect;
  if (v === prev) return;
  alerts.value.alert_probe_expect = v;
  await saveOne('alert_probe_expect', v, prev);
}

const CHAN_META = {
  alert_telegram_bot: { label: 'Telegram Bot Token', placeholder: '如 123456:ABC-DEF…' },
  alert_telegram_chat: { label: 'Telegram Chat ID', placeholder: '如 987654321' },
  alert_webhook_url: { label: '通用 Webhook URL', placeholder: 'POST {text}，兼容主流机器人网关' },
} as const;
type ChanKey = keyof typeof CHAN_META;
const chanOpen = ref(false);
const chanKey = ref<ChanKey>('alert_telegram_bot');
const chanDraft = ref('');
const chanSaving = ref(false);
const chanMeta = computed(() => CHAN_META[chanKey.value]);
function openChan(key: ChanKey) {
  chanKey.value = key;
  chanDraft.value = alerts.value[key] ?? '';
  chanOpen.value = true;
}
async function saveChan() {
  const key = chanKey.value;
  chanSaving.value = true;
  try {
    await api.saveAlerts({ [key]: chanDraft.value });
    alerts.value[key] = chanDraft.value;
    msg.success('已保存');
    chanOpen.value = false;
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    chanSaving.value = false;
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
  // 空期望 = 没有覆写，存下去也不会生效，列表又会把它过滤掉：
  // 让用户点了「新增覆写」却看到行凭空消失，比直接提示更糟。
  if (!probeDraft.value.expect.trim()) { msg.warning('先填期望状态码，留空等于不覆写'); return; }
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

onMounted(() => {
  mq.addEventListener('change', onMq);
  load();
});
onBeforeUnmount(() => mq.removeEventListener('change', onMq));
</script>

<template>
  <div>
    <PageHeader title="系统设置" sub="告警通道、防火墙状态与证书维护；用户与审计已升级为独立页面">
      <template #actions>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <PageTabBar :tabs="navTabs" :value="route.path" @update="(k: string) => router.push(k)" />

    <CpToolbarCard class="cp-card-gap">
      <section v-if="isAdmin">
        <div class="sec-head">
          <h2 class="sec-title">告警通知</h2>
          <NSpace :size="8">
            <NButton round size="small" tertiary :icon="ico('PulseOutline')" @click="runChecks">立即巡检</NButton>
            <NButton round size="small" tertiary :icon="ico('CloudUploadOutline')" @click="testAlert">发送测试通知</NButton>
          </NSpace>
        </div>
        <span class="cp-help">每 30 分钟自动巡检：磁盘、CPU、内存、负载、服务 failed、SSL 剩余天数，以及逐个应用的 HTTP 探活（只连 127.0.0.1 的端口，看它有没有应答）。触发后经 Telegram Bot 或 Webhook 推送，同类告警每天最多一次；资源类阈值要连续两轮超标才推送，历史里当轮就留痕。</span>
        <NForm class="cp-set-form" size="small" :label-width="isMobile ? undefined : 150" :label-placement="isMobile ? 'top' : 'left'" style="margin-top: var(--space-3)">
          <CpSettingRow label="Telegram Bot Token" :value="mask(alerts.alert_telegram_bot)" @edit="openChan('alert_telegram_bot')" />
          <CpSettingRow label="Telegram Chat ID" :value="mask(alerts.alert_telegram_chat)" @edit="openChan('alert_telegram_chat')" />
          <CpSettingRow label="通用 Webhook URL" :value="mask(alerts.alert_webhook_url)" @edit="openChan('alert_webhook_url')" />
          <CpSettingRow label="磁盘阈值 %">
            <template #control>
              <NInputNumber :value="alerts.alert_disk_pct" :min="50" :max="99" style="width: 118px" :input-props="{ 'aria-label': '磁盘使用率阈值百分比' }" @update:value="commitNum('alert_disk_pct', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="SSL 剩余天数">
            <template #control>
              <NInputNumber :value="alerts.alert_ssl_days" :min="0" :max="60" style="width: 118px" :input-props="{ 'aria-label': 'SSL 剩余天数阈值' }" @update:value="commitNum('alert_ssl_days', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="CPU 阈值 %">
            <template #control>
              <NInputNumber :value="alerts.alert_cpu_pct" :min="1" :max="99" style="width: 118px" :input-props="{ 'aria-label': 'CPU 使用率阈值百分比' }" @update:value="commitNum('alert_cpu_pct', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="内存阈值 %">
            <template #control>
              <NInputNumber :value="alerts.alert_mem_pct" :min="1" :max="99" style="width: 118px" :input-props="{ 'aria-label': '内存使用率阈值百分比' }" @update:value="commitNum('alert_mem_pct', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="load1 ×核数">
            <template #control>
              <NInputNumber :value="alerts.alert_load1_x" :min="1" :max="32" style="width: 118px" :input-props="{ 'aria-label': '一分钟负载阈值倍数' }" @update:value="commitNum('alert_load1_x', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="应用探活">
            <template #control>
              <NSwitch :value="String(alerts.alert_probe) !== '0'" size="small" aria-label="是否启用应用 HTTP 探活" @update:value="commitProbe" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="期望状态码" help="失焦或回车即保存">
            <template #control>
              <NInput :value="alerts.alert_probe_expect" size="small" style="width: 130px" placeholder="2xx/3xx" :input-props="{ 'aria-label': '全局探活期望状态码' }" @change="commitExpect" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="历史保留天数">
            <template #control>
              <NInputNumber :value="alerts.alert_keep_days" :min="1" :max="365" style="width: 118px" :input-props="{ 'aria-label': '告警历史保留天数' }" @update:value="commitNum('alert_keep_days', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="历史上限行">
            <template #control>
              <NInputNumber :value="alerts.alert_keep_rows" :min="100" :max="20000" :step="100" style="width: 118px" :input-props="{ 'aria-label': '告警历史最多保留行数' }" @update:value="commitNum('alert_keep_rows', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="按应用覆写期望状态码" help="探活只看端口是否应答：连不上或超时才算 crit，4xx/5xx 说明进程还在。覆写值留空即回落到上面的全局期望。">
            <template #control>
              <NSpace vertical :size="6" style="width: 100%">
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
            </template>
          </CpSettingRow>
        </NForm>
      </section>

      <div v-if="isAdmin" class="cp-divider"></div>
      <section v-if="isAdmin">
        <div class="sec-head">
          <h2 class="sec-title">防火墙</h2>
          <span class="st" :class="fw?.active ? 'ok' : 'err'"><span class="dot" :class="fw?.active ? 'ok' : 'err'"></span>{{ fw ? (fw.active ? 'ufw 已启用' : 'ufw 未启用') : '检测中…' }}</span>
        </div>
        <span class="cp-help">面板对防火墙只读；开启/改规则请 SSH 手动操作，以免误锁 SSH。</span>
        <NSpace vertical :size="12" style="margin-top: var(--space-3)">
          <div v-if="fw?.output" class="log-view" style="max-height: 220px">{{ fw.output }}</div>
          <EmptyBox v-else-if="fw" text="ufw 未返回规则输出：未安装时属正常，规则请用 SSH 手动维护" />
        </NSpace>
      </section>

      <div v-if="isAdmin" class="cp-divider"></div>
      <section v-if="isAdmin">
        <div class="sec-head">
          <h2 class="sec-title">证书维护</h2>
          <NButton round size="small" tertiary type="primary" :loading="renewing" :icon="ico('SyncOutline')" @click="renew">立即续期</NButton>
        </div>
        <span class="cp-help">certbot renew 检查所有已安装证书并自动续期。是否真的有人定时跑它，看安全自检页的「证书续期」一项（它会去查 certbot.timer 与 crontab，查不到就如实标 warn）；这里只用于手动触发验证。</span>
        <NSpace vertical :size="12" style="margin-top: var(--space-3)">
          <NText v-if="renewSummary" depth="3" style="font-size: var(--fs-xs)">{{ renewSummary }}</NText>
          <div v-if="renewLog" class="log-view" style="max-height: 220px">{{ renewLog }}</div>
          <EmptyBox v-else text="本次会话还没有执行过续期，点击上方按钮后即可看到 certbot 输出" />
        </NSpace>
      </section>
    </CpToolbarCard>

    <NDrawer v-model:show="chanOpen" :width="'30%'">
      <NDrawerContent :title="chanMeta.label" closable>
        <NForm label-placement="top" size="small">
          <NFormItem :label="chanMeta.label">
            <NInput v-model:value="chanDraft" :type="'password'" show-password-on="click" :placeholder="chanMeta.placeholder" :input-props="{ 'aria-label': chanMeta.label }" />
          </NFormItem>
        </NForm>
        <template #footer>
          <NSpace justify="end" :size="8">
            <NButton size="small" @click="chanOpen = false">取消</NButton>
            <NButton size="small" type="primary" :disabled="chanSaving" @click="saveChan">确定</NButton>
          </NSpace>
        </template>
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
/* 分区标题行：18px/400 左置，动作按钮右置（1Panel LayoutContent title 行） */
.sec-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
.sec-title { margin: 0; font-size: var(--fs-title); font-weight: 400; line-height: var(--lh-title); color: var(--cp-text-strong); }
</style>
