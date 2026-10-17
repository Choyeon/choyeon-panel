<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  NSpace, useMessage, NInput, NForm, NFormItem,
  NInputNumber, NSwitch, NDrawer, NDrawerContent, NText, NAlert,
} from 'naive-ui';
import { useRoute, useRouter } from 'vue-router';
import { NButton, useDrawerWidth } from '../ui';
import { api, getRole, getUsername } from '../api';
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
// 默认值和可填区间都不在这个文件里：它们跟着 /api/settings/alerts 的 `limits` 一起给出，
// 出处是 alerts_service.ALERT_LIMITS（巡检读取时用的就是那一份）。
// 过去这里抄了八个默认值、模板里又抄了八对 :min/:max，后端改任何一个数都不会带动它们，
// 于是页面显示 80、告警按 85 响；管理员按页面填的数被保存校验拒绝，或反过来存进去后被
// 读取侧悄悄钳掉。`critStep` 同理——要紧档比配置线高多少只在服务层算一次。
type AlertLimit = { default: number; min: number; max: number };
const alertLimits = ref<Record<string, AlertLimit>>({});
const critStep = ref(0);

function normalizeAlerts(v: any) {
  const out = { ...v };
  const limits = (v?.limits ?? {}) as Record<string, AlertLimit>;
  alertLimits.value = limits;
  critStep.value = Number(v?.crit_step ?? 0);
  Object.entries(limits).forEach(([k, lim]) => {
    if (out[k] === undefined || out[k] === null || out[k] === '') out[k] = lim.default;
    // NInputNumber 只认 number；库里存的是字符串，直接绑会渲染成空框，用户以为没配置。
    out[k] = Number(out[k]);
  });
  return out;
}

const lim = (k: string): AlertLimit => alertLimits.value[k] ?? { default: 0, min: 0, max: 0 };
// 三档「要紧档」提示：配置线是「该发通知了」，页面上的红要再高一档才有意义。
// 数字来自后端，不在这儿写死，否则这条说明会变成第四份出处。
const critHint = (k: string) => {
  const line = Number(alerts.value?.[k] ?? 0);
  if (!line || !critStep.value) return '';
  return `警戒线就是这条配置（当前 ${line}%），要紧档在它上面 ${critStep.value} 个点`
    + '——自检的红档、概览仪表的变色按要紧档，通知按警戒线。';
};

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
  // 两步验证是每个账号自己的事，只读账号也要能开：这一条不在 isAdmin 里面。
  await grab(() => api.twofa(), (v) => (twofa.value = v));
  if (isAdmin.value) await grab(() => api.alertSettings(), (v) => (alerts.value = normalizeAlerts(v)));
  // 防火墙规则表（开了哪些端口、来源地址）按 admin 收口，与后端 /api/firewall 同一口径；
  // 少了这个判断，只读账号一进设置页就吃到一条 403 报错。
  if (isAdmin.value) await grab(() => api.firewall(), (v) => (fw.value = v));
}
async function renew() {
  // 连点会并发拉起两个 certbot，第二个直接被它自己的锁挡下、报一条看不懂的错。
  if (renewing.value) return;
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
// 阈值是"改了就存"的输入框/开关：按住步进器或连点开关会打出多条 PUT，落点却是同一个键。
// 同值直接丢掉；不同值记成"当前这条写完还要再写一次"，于是既不并发写同一个键，
// 也不会把用户最后一次改动吞掉（早退在这里是错的，被吞的那一次正好是他想要的值）。
const alertInflight = new Map<string, string>();
const alertQueued = new Map<string, string>();

async function saveOne(key: string, value: string, rollback: any) {
  if (alertInflight.has(key)) {
    if (alertQueued.get(key) === value || alertInflight.get(key) === value) return;
    alertQueued.set(key, value);
    return;
  }
  alertInflight.set(key, value);
  try {
    let v = value;
    let rb = rollback;
    for (;;) {
      alertQueued.delete(key);
      try {
        await api.saveAlerts({ [key]: v });
        msg.success('已保存');
      } catch (e: any) {
        alerts.value[key] = rb;
        msg.error(e.message);
      }
      const next = alertQueued.get(key);
      if (next === undefined) break;
      // 排队期间显示值又被改了一次，回滚基准取现在显示的这一个
      rb = alerts.value[key];
      v = next;
      alertInflight.set(key, v);
    }
  } finally {
    alertInflight.delete(key);
    alertQueued.delete(key);
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
const chanDrawerW = useDrawerWidth(0.3);
const twDrawerW = useDrawerWidth(0.32);
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
  if (chanSaving.value) return;
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

// 探活期望状态码按应用覆写：键名由后端给的前缀 + 应用名拼出，服务端会核对应用是否存在，
// 所以这里不做本地白名单，写错了会被 400 挡回来并显示原因。
// 三个动作各自要认「在哪一行上」：行内保存/删除按 name 记账，新增按整个草稿记账，
// 否则连点会在同一个键上叠两次写、弹两条「已保存」。
const probeRowBusy = ref<string | null>(null);
const probeAdding = ref(false);
const checking = ref(false);
const testing = ref(false);
async function saveProbeRow(entry: { name: string; expect: string }) {
  if (probeRowBusy.value !== null) return false;
  probeRowBusy.value = entry.name;
  try {
    // 前缀由后端随载荷给出（probe_prefix）：页面自己拼一份的话，改名后写进去的键
    // 在服务端一律查不到，而未知的告警键是静默忽略的——保存回 200、覆写永不生效。
    const pfx = String(alerts.value?.probe_prefix ?? '');
    if (!pfx) {
      msg.error('后端没有给出覆写键前缀，已拒绝保存（否则这条覆写永远读不回来）');
      return false;
    }
    await api.saveAlerts({ [`${pfx}${entry.name}`]: (entry.expect || '').trim() });
    msg.success(`已保存 ${entry.name} 的期望状态码`);
    await load();
    return true;
  } catch (e: any) {
    msg.error(e.message);
    return false;
  } finally {
    probeRowBusy.value = null;
  }
}

async function addProbeRow() {
  const name = probeDraft.value.name.trim();
  if (probeAdding.value) return;
  if (!name) { msg.warning('先填应用名'); return; }
  // 空期望 = 没有覆写，存下去也不会生效，列表又会把它过滤掉：
  // 让用户点了「新增覆写」却看到行凭空消失，比直接提示更糟。
  if (!probeDraft.value.expect.trim()) { msg.warning('先填期望状态码，留空等于不覆写'); return; }
  if (probeList.value.some((r) => r.name === name)) { msg.warning('这个应用已经有覆写了，直接改上面那行'); return; }
  probeAdding.value = true;
  try {
    await saveProbeRow({ name, expect: probeDraft.value.expect });
    probeDraft.value = { name: '', expect: '' };
  } finally {
    probeAdding.value = false;
  }
}

async function dropProbeRow(name: string) {
  // 空值就是"取消覆写"：服务端存空串，巡检侧回落全局期望，不为删一行配置新增端点。
  await saveProbeRow({ name, expect: '' });
}
async function runChecks() {
  // 巡检一轮要跑几十次探活，连点等于让同机同时并跑两遍；成功后还会各推一条通知。
  if (checking.value) return;
  checking.value = true;
  try {
    await api.alertRunChecks();
    msg.success('巡检已执行，若触发阈值会立即推送');
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    checking.value = false;
  }
}
async function testAlert() {
  if (testing.value) return;
  testing.value = true;
  try {
    await api.saveAlerts(alerts.value);
    await api.alertTest();
    msg.success('测试通知已发送（若未收到请检查配置/通道）');
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    testing.value = false;
  }
}

// ---------- 两步验证（只管当前登录的这个账号） ----------
// digits/period/issuer 全部来自 GET /api/auth/2fa，出处是 backend/app/totp.py：
// 这里抄一个"6"，改参数那天就是"输入框只让填 6 位、校验要 8 位"这种没人看得懂的坏法。
const twofa = ref<any>(null);
const twOpen = ref(false);
const twMode = ref<'enable' | 'disable'>('enable');
const twStep = ref<'password' | 'scan'>('password');
const twBusy = ref(false);
const twDraft = ref({ password: '', code: '' });
// setup 下发的密钥只活在这个抽屉的内存里：不落 localStorage、不进日志、关掉即清空。
const twSecret = ref({ secret: '', otpauth: '' });
const twQr = ref('');

function openTwofa(mode: 'enable' | 'disable') {
  twMode.value = mode;
  twStep.value = 'password';
  twBusy.value = false;
  twDraft.value = { password: '', code: '' };
  twSecret.value = { secret: '', otpauth: '' };
  twQr.value = '';
  twOpen.value = true;
}
watch(twOpen, (v) => {
  // 抽屉还能被 ESC 和遮罩关掉，所以清空只写在这一处：离开这个流程就把口令、密钥、二维码一起丢掉。
  if (v) return;
  twDraft.value = { password: '', code: '' };
  twSecret.value = { secret: '', otpauth: '' };
  twQr.value = '';
  twBusy.value = false;
});

async function renderQr(otpauth: string) {
  // 二维码库只在绑定这一步用得上：静态 import 会把它算进首屏 chunk，
  // 而绝大多数账号一辈子不会打开这个抽屉。动态 import 让它单独一块，点开才下载。
  try {
    const { default: QRCode } = await import('qrcode');
    twQr.value = await QRCode.toDataURL(otpauth, { margin: 1, width: 220 });
  } catch {
    // 库没加载成也不挡流程：下面还给了能手工录入的密钥与 otpauth 文本
    twQr.value = '';
  }
}

async function startTwofa() {
  if (twBusy.value) return;
  if (!twDraft.value.password) return msg.warning('先输入本账号口令');
  twBusy.value = true;
  try {
    const r = await api.twofaSetup(twDraft.value.password);
    twSecret.value = { secret: String(r.secret ?? ''), otpauth: String(r.otpauth ?? '') };
    twStep.value = 'scan';
    await renderQr(twSecret.value.otpauth);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    twBusy.value = false;
  }
}

async function commitTwofa() {
  // enable 连点会打到"该账号已开启"；disable 同理。twBusy 在两个函数里都是同步先置位。
  if (twBusy.value) return;
  if (!twDraft.value.password) return msg.warning('先输入本账号口令');
  if (!twDraft.value.code) return msg.warning('先填验证器里的动态口令');
  twBusy.value = true;
  try {
    if (twMode.value === 'enable') {
      await api.twofaEnable(twDraft.value.password, twSecret.value.secret, twDraft.value.code);
      msg.success('两步验证已开启，下次登录起需要动态口令');
    } else {
      await api.twofaDisable(twDraft.value.password, twDraft.value.code);
      msg.success('两步验证已关闭');
    }
    twOpen.value = false;
    twofa.value = await api.twofa();
  } catch (e: any) {
    // 失败要重读一次状态：enable 服务端可能已经写库而响应没送到（比如反向代理超时），
    // 界面停在"未开启"会让人再点一次，而第二次会被告知"该账号已开启"，更看不懂。
    await grab(() => api.twofa(), (v) => (twofa.value = v));
    msg.error(e.message);
  } finally {
    twBusy.value = false;
  }
}

async function copyTw(text: string, label: string) {
  try {
    await navigator.clipboard.writeText(text);
    msg.success(`${label}已复制`);
  } catch {
    msg.warning(`浏览器不允许直接复制${label}，请手工选中`);
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
    <PageHeader title="系统设置" sub="本账号两步验证、告警通道、防火墙状态与证书维护；用户与审计已升级为独立页面">
      <template #actions>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <PageTabBar :tabs="navTabs" :value="route.path" @update="(k: string) => router.push(k)" />

    <CpToolbarCard class="cp-card-gap">
      <section>
        <div class="sec-head">
          <h2 class="sec-title">两步验证</h2>
          <NButton
            v-if="!twofa?.enabled"
            round
            size="small"
            type="primary"
            tertiary
            :icon="ico('KeyOutline')"
            @click="openTwofa('enable')"
          >开启</NButton>
          <NButton v-else round size="small" tertiary type="warning" :icon="ico('ShieldOutline')" @click="openTwofa('disable')">关闭</NButton>
        </div>
        <span class="cp-help">
          给这个账号加一枚登录动态口令：口令正确之后，还要验证器 App（Google Authenticator、Microsoft Authenticator、FreeOTP 都能扫）里当时那一串。
          开启与关闭都要再输一次本账号口令，关闭还要一枚当前有效的动态口令——只攥着一枚偷来的令牌的人不该能顺手把主人的第二因子关掉。
          密钥在服务端只有 totp_secret 这一列，任何页面与审计都不会再回显它；生成一次就要当场扫完，关掉这个抽屉密钥即失效。
          管理员在「用户管理」页只看得到有没有开、不能替别人关：验证器丢了，请由 root 在服务器上执行
          <span class="mono-dim cp-break">choyeonctl user 2fa-disable &lt;用户名&gt; --yes</span>，它会同时吊销该账号的全部会话。
        </span>
        <NSpace vertical :size="8" style="margin-top: var(--space-3)">
          <NText v-if="twofa?.enabled" depth="3" style="font-size: var(--fs-xs)">
            已开启：签发方 {{ twofa.issuer }}，每 {{ twofa.period }} 秒换一枚 {{ twofa.digits }} 位口令。
          </NText>
          <NText v-else depth="3" style="font-size: var(--fs-xs)">未开启：这个账号目前只要口令正确就能登录。</NText>
        </NSpace>
      </section>

      <div v-if="isAdmin" class="cp-divider"></div>
      <section v-if="isAdmin">
        <div class="sec-head">
          <h2 class="sec-title">告警通知</h2>
          <NSpace :size="8">
            <NButton round size="small" tertiary :icon="ico('PulseOutline')" :loading="checking" :disabled="checking" @click="runChecks">立即巡检</NButton>
            <NButton round size="small" tertiary :icon="ico('CloudUploadOutline')" :loading="testing" :disabled="testing" @click="testAlert">发送测试通知</NButton>
          </NSpace>
        </div>
        <span class="cp-help">每 30 分钟自动巡检：磁盘、CPU、内存、负载、服务 failed、SSL 剩余天数，以及逐个应用的 HTTP 探活（只连 127.0.0.1 的端口，看它有没有应答）。触发后经 Telegram Bot 或 Webhook 推送，同类告警每天最多一次；资源类阈值要连续两轮超标才推送，历史里当轮就留痕。</span>
        <NForm class="cp-set-form" size="small" :label-width="isMobile ? undefined : 150" :label-placement="isMobile ? 'top' : 'left'" style="margin-top: var(--space-3)">
          <CpSettingRow label="Telegram Bot Token" :value="mask(alerts.alert_telegram_bot)" @edit="openChan('alert_telegram_bot')" />
          <CpSettingRow label="Telegram Chat ID" :value="mask(alerts.alert_telegram_chat)" @edit="openChan('alert_telegram_chat')" />
          <CpSettingRow label="通用 Webhook URL" :value="mask(alerts.alert_webhook_url)" @edit="openChan('alert_webhook_url')" />
          <CpSettingRow label="磁盘阈值 %" :help="critHint('alert_disk_pct')">
            <template #control>
              <NInputNumber :value="alerts.alert_disk_pct" :min="lim('alert_disk_pct').min" :max="lim('alert_disk_pct').max" style="width: 118px; max-width: 100%" :input-props="{ 'aria-label': '磁盘使用率阈值百分比' }" @update:value="commitNum('alert_disk_pct', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="SSL 剩余天数">
            <template #control>
              <NInputNumber :value="alerts.alert_ssl_days" :min="lim('alert_ssl_days').min" :max="lim('alert_ssl_days').max" style="width: 118px; max-width: 100%" :input-props="{ 'aria-label': 'SSL 剩余天数阈值' }" @update:value="commitNum('alert_ssl_days', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="CPU 阈值 %" :help="critHint('alert_cpu_pct')">
            <template #control>
              <NInputNumber :value="alerts.alert_cpu_pct" :min="lim('alert_cpu_pct').min" :max="lim('alert_cpu_pct').max" style="width: 118px; max-width: 100%" :input-props="{ 'aria-label': 'CPU 使用率阈值百分比' }" @update:value="commitNum('alert_cpu_pct', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="内存阈值 %" :help="critHint('alert_mem_pct')">
            <template #control>
              <NInputNumber :value="alerts.alert_mem_pct" :min="lim('alert_mem_pct').min" :max="lim('alert_mem_pct').max" style="width: 118px; max-width: 100%" :input-props="{ 'aria-label': '内存使用率阈值百分比' }" @update:value="commitNum('alert_mem_pct', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="load1 ×核数" help="这一条只决定告警。概览的负载环按物理饱和度变色（满一格是每核一个排队任务），不会跟着这个倍数走。">
            <template #control>
              <NInputNumber :value="alerts.alert_load1_x" :min="lim('alert_load1_x').min" :max="lim('alert_load1_x').max" style="width: 118px; max-width: 100%" :input-props="{ 'aria-label': '一分钟负载阈值倍数' }" @update:value="commitNum('alert_load1_x', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="应用探活">
            <template #control>
              <NSwitch :value="String(alerts.alert_probe) !== '0'" size="small" aria-label="是否启用应用 HTTP 探活" @update:value="commitProbe" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="期望状态码" help="失焦或回车即保存">
            <template #control>
              <NInput :value="alerts.alert_probe_expect" size="small" style="width: 130px; max-width: 100%" placeholder="2xx/3xx" :input-props="{ 'aria-label': '全局探活期望状态码' }" @change="commitExpect" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="历史保留天数">
            <template #control>
              <NInputNumber :value="alerts.alert_keep_days" :min="lim('alert_keep_days').min" :max="lim('alert_keep_days').max" style="width: 118px; max-width: 100%" :input-props="{ 'aria-label': '告警历史保留天数' }" @update:value="commitNum('alert_keep_days', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="历史上限行">
            <template #control>
              <NInputNumber :value="alerts.alert_keep_rows" :min="lim('alert_keep_rows').min" :max="lim('alert_keep_rows').max" :step="100" style="width: 118px; max-width: 100%" :input-props="{ 'aria-label': '告警历史最多保留行数' }" @update:value="commitNum('alert_keep_rows', $event)" />
            </template>
          </CpSettingRow>
          <CpSettingRow label="按应用覆写期望状态码" help="探活只看端口是否应答：连不上或超时才算 crit，4xx/5xx 说明进程还在。覆写值留空即回落到上面的全局期望。">
            <template #control>
              <NSpace vertical :size="6" style="width: 100%">
                <NSpace v-for="row in probeList" :key="row.name" align="center" :size="8">
                  <span class="mono-dim cp-break" style="min-width: 148px">{{ row.name }}</span>
                  <NInput :value="row.expect" size="tiny" style="width: 132px; max-width: 100%" placeholder="200 204" :input-props="{ 'aria-label': `${row.name} 的期望状态码` }" @update:value="(v: string) => { row.expect = v; }" />
                  <NButton size="tiny" tertiary :icon="ico('SaveOutline', 12)" :loading="probeRowBusy === row.name" :disabled="probeRowBusy !== null || probeAdding" @click="saveProbeRow(row)">保存</NButton>
                  <NButton size="tiny" tertiary :icon="ico('TrashOutline', 12)" aria-label="删除该覆写" :loading="probeRowBusy === row.name" :disabled="probeRowBusy !== null || probeAdding" @click="dropProbeRow(row.name)">删除</NButton>
                </NSpace>
                <EmptyBox v-if="!probeList.length" text="还没有按应用覆写，默认全部走全局期望状态码" />
                <NSpace align="center" :size="8">
                  <NInput v-model:value="probeDraft.name" size="tiny" style="width: 148px; max-width: 100%" placeholder="应用名" :input-props="{ 'aria-label': '新增覆写的应用名' }" @keyup.enter="addProbeRow" />
                  <NInput v-model:value="probeDraft.expect" size="tiny" style="width: 132px; max-width: 100%" placeholder="200 204" :input-props="{ 'aria-label': '新增覆写的期望状态码' }" @keyup.enter="addProbeRow" />
                  <NButton size="tiny" tertiary :icon="ico('AddOutline', 12)" :loading="probeAdding" :disabled="probeAdding || probeRowBusy !== null" @click="addProbeRow">新增覆写</NButton>
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
          <span class="st" :class="fw?.active ? 'ok' : 'err'"><span class="dot" :class="fw?.active ? 'ok' : 'err'"></span>{{ fw ? `${fw.tool === 'firewalld' ? 'firewalld' : fw.tool === 'ufw' ? 'ufw' : '未检测到'} ${fw.active ? '已启用' : '未启用'}` : '检测中…' }}</span>
        </div>
        <span class="cp-help">规则的增删与启停在侧栏「运维工具 → 防火墙」页做，这里只留状态摘要，避免两个入口改同一份配置。那一页会拒绝删掉 SSH、面板入口与你当前访问所用端口的唯一放行来源，所以不必担心一次误删把自己关在门外。</span>
        <NSpace vertical :size="12" style="margin-top: var(--space-3)">
          <div v-if="fw?.output" class="log-view" style="max-height: 220px">{{ fw.output }}</div>
          <EmptyBox v-else-if="fw" text="既没有 ufw 也没有 firewalld；装好任意一个后到「防火墙」页管理规则" />
        </NSpace>
      </section>

      <div v-if="isAdmin" class="cp-divider"></div>
      <section v-if="isAdmin">
        <div class="sec-head">
          <h2 class="sec-title">证书维护</h2>
          <NButton round size="small" tertiary type="primary" :loading="renewing" :disabled="renewing" :icon="ico('SyncOutline')" @click="renew">立即续期</NButton>
        </div>
        <span class="cp-help">certbot renew 检查所有已安装证书并自动续期。是否真的有人定时跑它，看安全自检页的「证书续期」一项（它会去查 certbot.timer 与 crontab，查不到就如实标 warn）；这里只用于手动触发验证。</span>
        <NSpace vertical :size="12" style="margin-top: var(--space-3)">
          <NText v-if="renewSummary" depth="3" style="font-size: var(--fs-xs)">{{ renewSummary }}</NText>
          <div v-if="renewLog" class="log-view" style="max-height: 220px">{{ renewLog }}</div>
          <EmptyBox v-else text="本次会话还没有执行过续期，点击上方按钮后即可看到 certbot 输出" />
        </NSpace>
      </section>
    </CpToolbarCard>

    <NDrawer v-model:show="chanOpen" :width="chanDrawerW">
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
    <NDrawer v-model:show="twOpen" :width="twDrawerW">
      <NDrawerContent :title="twMode === 'enable' ? '开启两步验证' : '关闭两步验证'" closable>
        <NForm label-placement="top" size="small">
          <NAlert v-if="twMode === 'disable'" type="warning" :bordered="false" size="small" class="tw-alert">
            关闭之后这个账号只用口令就能登录。验证器不在手边就别在这里绕：让 root 在服务器上执行
            choyeonctl user 2fa-disable {{ getUsername() }} --yes，它会同时吊销该账号的全部会话。
          </NAlert>
          <NFormItem label="本账号口令">
            <NInput
              v-model:value="twDraft.password"
              type="password"
              show-password-on="click"
              placeholder="再核对一次"
              :input-props="{ 'aria-label': '本账号口令', autocomplete: 'current-password' }"
            />
          </NFormItem>
          <template v-if="twMode === 'enable' && twStep === 'scan'">
            <div class="tw-qr">
              <img v-if="twQr" :src="twQr" width="220" height="220" style="max-width: 100%; height: auto" alt="两步验证绑定二维码" />
              <NText v-else depth="3" style="font-size: var(--fs-xs)">二维码没出来，用验证器的手工录入填下面两行。</NText>
            </div>
            <div class="tw-key">
              <span class="tw-key-label">密钥（手工录入）</span>
              <code class="mono">{{ twSecret.secret }}</code>
              <NButton size="tiny" tertiary :icon="ico('CopyOutline', 12)" aria-label="复制密钥" @click="copyTw(twSecret.secret, '密钥')">复制</NButton>
            </div>
            <div class="tw-key">
              <span class="tw-key-label">otpauth 链接</span>
              <code class="mono">{{ twSecret.otpauth }}</code>
              <NButton size="tiny" tertiary :icon="ico('CopyOutline', 12)" aria-label="复制 otpauth 链接" @click="copyTw(twSecret.otpauth, '链接')">复制</NButton>
            </div>
          </template>
          <!-- 动态口令只在"能填了"的时候出现：enable 要等密钥生成完（否则框在、验证器里却还没有
               这个账号，填什么都错），disable 则要立刻填。 -->
          <NFormItem
            v-if="twMode === 'disable' || twStep === 'scan'"
            :label="`动态口令（每 ${twofa?.period || 30} 秒换一枚）`"
          >
            <NInput
              v-model:value="twDraft.code"
              :maxlength="twofa?.digits"
              placeholder="验证器里当前那一枚"
              :input-props="{ 'aria-label': '动态口令', inputmode: 'numeric', autocomplete: 'one-time-code' }"
              @keyup.enter="commitTwofa"
            />
          </NFormItem>
          <NText v-if="twMode === 'enable' && twStep === 'scan'" depth="3" class="tw-note">
            这一刻请让验证器和这个页面同时开着：密钥只在本次响应里给一次，没扫完就关掉抽屉只能重新生成。
          </NText>
        </NForm>
        <template #footer>
          <NSpace justify="end" :size="8">
            <NButton size="small" @click="twOpen = false">取消</NButton>
            <NButton
              v-if="twMode === 'enable' && twStep === 'password'"
              size="small"
              type="primary"
              :loading="twBusy"
              :disabled="twBusy"
              @click="startTwofa"
            >生成密钥</NButton>
            <NButton v-else size="small" type="primary" :loading="twBusy" :disabled="twBusy" @click="commitTwofa">
              {{ twMode === 'enable' ? '开启' : '确认关闭' }}
            </NButton>
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
.tw-alert { margin-bottom: var(--space-3); }
/* 二维码要留够边距且别被抽屉挤扁：220px 是验证器在 30cm 外能稳定扫出的下限附近，再小就要凑近镜头 */
.tw-qr { display: flex; justify-content: center; padding: var(--space-3) 0; }
.tw-qr img { width: 220px; height: 220px; max-width: 100%; border-radius: var(--radius-md); }
.tw-key { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; margin: 0 0 var(--space-3); }
.tw-key-label { flex: 0 0 auto; font-size: var(--fs-xs); color: var(--cp-text-dim); }
/* user-select: all 点一下就整段选中：密钥和 otpauth 都是要一次抄走的串，逐字符选反而麻烦 */
.tw-key code { flex: 1 1 auto; min-width: 0; font-size: var(--fs-xs); line-height: 1.6; color: var(--cp-text-strong); word-break: break-all; user-select: all; }
.tw-note { display: block; margin-top: var(--space-2); font-size: var(--fs-xs); }
</style>
