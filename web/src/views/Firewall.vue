<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  NAlert, NCard, NDataTable, NIcon, NInput, NModal, NPopconfirm, NSpace, NSwitch, NTag, NText, useMessage,
} from 'naive-ui';
import { NButton } from '../ui';
import { getRole, req } from '../api';
import { icon, icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

/**
 * 超时都 ≥ 服务端子进程上限：add/remove 是「改规则 + reload」两次调用（各 60s），
 * enable/disable 单次 90s。本地先 abort 会让用户以为没执行而重试，
 * 而远端那条 ufw/firewall-cmd 还在跑，重试就是并发改防火墙。
 */
const api = {
  state: () => req('/firewall'),
  add: (body: any) => req('/firewall/rules', { body, timeout: 130000 }),
  remove: (num: number, sig: string) =>
    req(`/firewall/rules/${num}?expect=${encodeURIComponent(sig)}`, { method: 'DELETE', timeout: 130000 }),
  setEnabled: (active: boolean) => req('/firewall/state', { body: { active }, timeout: 100000 }),
};

const msg = useMessage();
const isAdmin = computed(() => getRole() === 'admin');
const ico = (name: string, size = 14) => icon(name, size);

const st = ref<any>(null);
const loadError = ref('');
const loading = ref(false);
const busy = ref('');
const showAdd = ref(false);
const showRaw = ref(false);
const narrow = ref(window.innerWidth < 900);

// 与后端 validate_rule 同一套上限（MAX_RANGE_SPAN=1000、MAX_COMMENT=40）：
// 前端拦不住的边界，用户要等一次往返才知道，而写歪的边界在服务端只是拒绝，不是截断。
const MAX_SPAN = 1000;
const form = ref({
  port: '',
  port_to: '',
  proto: 'tcp',
  action: 'allow',
  rate: false,
  source: '',
  comment: '',
});

const ACTION_META: Record<string, { type: any; text: string }> = {
  allow: { type: 'success', text: '放行' },
  deny: { type: 'error', text: '拒绝' },
  limit: { type: 'warning', text: '限速放行' },
};

/**
 * 「读不到」是第三种状态，既不能画成「未运行」，也不能画成「0 条规则」。
 * 判定与后端 firewall_service.unread / rules_unread 用同一套条件：active 为 null 表示
 * 状态探测本身失败（退出码非 0、超时、正文读不懂），rules_ok 为 false 表示那份放行表没拿到。
 * tool 为 none 时两者都留空——没有防火墙这件事本来就是个确定的答案，不是探测失败。
 */
const stateBad = computed(() => {
  const s = st.value;
  if (!s || !s.tool || s.tool === 'none') return '';
  return s.active === null ? String(s.reason || '状态没读到') : '';
});
const rulesBad = computed(() => {
  const s = st.value;
  if (!s || !s.tool || s.tool === 'none') return '';
  return s.rules_ok === false ? String(s.reason || '规则表没读到') : '';
});
const blindNote = computed(() => {
  if (stateBad.value) {
    return `防火墙现在到底开没开，面板读不出来（${stateBad.value}）。这种情况下页面不会给你「未运行」的绿灯，`
      + '也不会替你按下启用或停用——一个读不到状态的防火墙，停它和开它都可能把 SSH 关在门外。';
  }
  if (rulesBad.value) {
    return `放行规则这一份表没读到（${rulesBad.value}）。表里没有行不等于机器上没有放行规则，`
      + '所以删除、按编号操作一律拒绝：编号无从核对，删完还剩谁在保护 22 也判不了。';
  }
  return '';
});
// firewalld 的 service 规则只逐条核对前若干条端口定义（上限只在服务层定义一次），
// 触顶后没查的那些按「读不到」处理：面板不据此说「端口都核对过了」，如实报出没查的条数。
const enrichSkipped = computed(() => Number(st.value?.service_ports_skipped ?? 0));
// 核对上限由服务层给（前端不抄写那个数字）：只在真要显示"核对到第几条"时读一次。
const enrichMax = computed(() => Number(st.value?.service_ports_enrich_max ?? 0));
// 启用 ufw 前，后端要拿这份表确认 22 与面板入口仍在放行；表没读到时它一定拒绝，
// 按钮就跟着一起置灰，别让人点一次才知道。firewalld 的启用看的是 permanent 那份表，
// 与运行期表无关，所以只有 ufw 需要在这里跟着 rulesBad 走。
const toggleBlocked = computed(() => {
  const s = st.value;
  if (!s || !s.tool || s.tool === 'none') return true;
  if (s.active === null) return true;
  return s.active === false && s.tool === 'ufw' && rulesBad.value !== '';
});
const toggleBlockedWhy = computed(() => {
  if (stateBad.value) return `状态没读到（${stateBad.value}），面板判不了按下之后 SSH 还进不进得来。`;
  if (rulesBad.value) return `放行表没读到（${rulesBad.value}），启用之前没法确认 22 和面板入口端口还在放行名单里。`;
  return '';
});
const headStatus = computed(() => {
  const s = st.value;
  if (!s || !s.tool || s.tool === 'none') return '';
  if (s.active === null) return '状态没读到';
  return s.active ? '运行中' : '未运行';
});
const headType = computed(() => {
  if (stateBad.value) return 'warning';
  return st.value?.active ? 'success' : 'default';
});
const stateText = computed(() => {
  const s = st.value;
  if (!s) return '检测中…';
  if (!s.tool || s.tool === 'none') return '未检测到工具';
  if (s.active === null) return '状态没读到';
  return s.active ? '已启用' : '未启用';
});

const toolLabel = computed(() => ({ ufw: 'ufw', firewalld: 'firewalld', none: '未检测到防火墙' }[st.value?.tool] || '检测中…'));
const rules = computed<any[]>(() => st.value?.rules || []);
const isFirewalld = computed(() => st.value?.tool === 'firewalld');
const protectedList = computed(() =>
  (st.value?.protected || []).map((p: number) => ({ port: p, why: st.value?.protected_reasons?.[String(p)] || '' })),
);
const openPorts = computed(() =>
  rules.value
    .filter((r: any) => r.action !== 'deny' && r.direction !== 'out' && !r.protected)
    .map((r: any) => r.port)
    .filter((p: any) => typeof p === 'number')
    .length,
);

function onResize() {
  narrow.value = window.innerWidth < 900;
}
onMounted(() => {
  window.addEventListener('resize', onResize);
  load();
});
onBeforeUnmount(() => window.removeEventListener('resize', onResize));

async function load() {
  loading.value = true;
  loadError.value = '';
  try {
    st.value = await api.state();
  } catch (e: any) {
    loadError.value = e.message;
    st.value = null;
  } finally {
    loading.value = false;
  }
}

async function submitAdd() {
  // busy 同时是这三处写操作的互斥锁：ufw 的一条规则要「改 + reload」两次调用，
  // 连点第二次会在第一次的 reload 期间下手，留下半套规则。
  if (busy.value) return;
  busy.value = 'add';
  const f = form.value;
  const body: any = { port: f.port.trim(), action: f.action, proto: f.proto };
  if (f.port_to.trim()) body.port_to = f.port_to.trim();
  if (f.rate && f.action === 'allow') body.rate = true;
  if (f.source.trim()) body.source = f.source.trim();
  if (f.comment.trim()) body.comment = f.comment.trim();
  try {
    const r = await api.add(body);
    // reloaded===false：规则只进了 permanent 表，面板为防自锁故意没替用户跑 --reload，
    // 这条放行此刻还没生效。同页的删除路径早就按这一条分黄字，添加路径不能只看 200 就报绿。
    if (r.reloaded === false) msg.warning(`${r.detail || '规则已写入，但未重载（尚未生效）'}`, { duration: 8000 });
    else msg.success(r.detail || '规则已添加');
    showAdd.value = false;
    form.value = { port: '', port_to: '', proto: 'tcp', action: 'allow', rate: false, source: '', comment: '' };
    await load();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = '';
  }
}

async function removeRule(r: any) {
  if (busy.value) return;
  busy.value = `del-${r.num}`;
  try {
    const res = await api.remove(r.num, r.sig);
    if (res.reloaded === false) msg.warning(`已删除 ${res.removed}；${res.detail}`);
    // ufw 的 allow 不带来源时一次写 v4+v6 两条，而删除只删一条编号。
    // 不把剩下的那条说出来，用户会以为这个端口已经关了。
    else if (res.still_open?.length)
      msg.warning(`已删除 ${res.removed}，但 ${res.still_open.join('、')} 仍在放行同一端口，需再删一次`);
    else msg.success(`已删除 ${res.removed || ''}`);
    await load();
  } catch (e: any) {
    msg.error(e.message);
    // 「规则表已经变化」是删除前签名比对的结果，这时候必须重新列表，
    // 否则用户会拿着旧编号一直点一直失败。
    if (/刷新/.test(e.message)) await load();
  } finally {
    busy.value = '';
  }
}

async function toggle(active: boolean) {
  if (busy.value) return;
  busy.value = 'state';
  try {
    const r = await api.setEnabled(active);
    msg.success(r.detail || (active ? '防火墙已启用' : '防火墙已关闭'));
    await load();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = '';
  }
}

function portText(r: any) {
  if (r.kind === 'service') return `服务 ${r.token}`;
  if (r.port === null || r.port === undefined) return r.raw || '任意端口';
  const span = r.port_to && r.port_to !== r.port ? `${r.port}-${r.port_to}` : `${r.port}`;
  const proto = r.proto && r.proto !== 'both' ? `/${r.proto}` : '';
  return `${span}${proto}`;
}

function actionText(r: any) {
  const meta = ACTION_META[r.action] || { type: 'default', text: r.action };
  return h(NTag, { size: 'small', bordered: false, type: meta.type }, { default: () => r.direction === 'out' ? `${meta.text}(出站)` : meta.text });
}

const columns = computed(() => {
  const base: any[] = [
    { title: '端口/服务', key: 'to', minWidth: 150, render: (r: any) => h('span', { class: 'mono' }, portText(r)) },
    { title: '动作', key: 'action', width: narrow.value ? 92 : 116, render: actionText },
    { title: '来源', key: 'source', minWidth: 140, render: (r: any) => h('span', { class: 'mono' }, r.source || '任意地址') },
  ];
  if (!narrow.value) {
    base.push({
      title: '地址族',
      key: 'family',
      width: 84,
      render: (r: any) => h('span', { class: 'cell-sub' }, r.family === 'ipv6' ? 'IPv6' : 'IPv4'),
    });
    base.push({ title: '备注', key: 'comment', minWidth: 120, render: (r: any) => h('span', { class: 'cell-sub' }, r.comment || '—') });
  }
  if (isFirewalld.value && !narrow.value) {
    base.push({
      title: '持久化',
      key: 'persisted',
      width: 96,
      render: (r: any) =>
        r.scope === 'runtime'
          ? h(NTag, { size: 'tiny', bordered: false, type: r.persisted ? 'default' : 'warning' }, { default: () => (r.persisted ? '已写入' : '仅运行期') })
          : h('span', { class: 'cell-sub' }, '—'),
    });
  }
  base.push({
    title: '操作',
    key: 'ops',
    width: 88,
    align: 'center' as const,
    fixed: 'right' as const,
    render: (r: any) => {
      if (!isAdmin.value) return h('span', { class: 'cell-sub' }, '—');
      if (r.protected) {
        return h('span', { class: 'cell-sub', title: '删除后 SSH 或面板入口就不通了，面板不允许这种操作' }, '受保护');
      }
      return h(NPopconfirm, { onPositiveClick: () => removeRule(r) }, {
        trigger: () =>
          h(NButton, {
            text: true, size: 'small', type: 'error', class: 'cp-op',
            loading: busy.value === `del-${r.num}`,
          }, { default: () => '删除' }),
        default: () => `将从 ${toolLabel.value} 移除 ${portText(r)}（来源 ${r.source || '任意地址'}）。`,
      });
    },
  });
  return base;
});

const rangeHint = computed(() => {
  const lo = Number(form.value.port);
  const hi = Number(form.value.port_to);
  if (form.value.port_to && form.value.port && hi >= lo) {
    return `区间共 ${hi - lo + 1} 个端口，服务端上限是跨度 ${MAX_SPAN}`;
  }
  return `结束端口留空表示单个端口；一条规则最多跨 ${MAX_SPAN} 个端口，避免一次打开过大缺口`;
});
</script>

<template>
  <div>
    <PageHeader title="防火墙" sub="直接管理本机的 ufw / firewalld 规则。面板会守住 SSH、面板自己的入口端口和你当前正在使用的端口，删掉它们会立刻把自己关在门外，这类操作一律被拒绝。">
      <template #actions>
        <NTag v-if="st" size="small" :bordered="false" :type="headType">
          {{ headStatus ? `${toolLabel} · ${headStatus}` : toolLabel }}
        </NTag>
        <NButton size="small" tertiary :disabled="!st" @click="showRaw = true">原始输出</NButton>
        <NButton v-if="isAdmin" size="small" type="primary" :icon="ico('AddOutline')"
                 :disabled="!!busy || !st || st.tool === 'none'" @click="showAdd = true">新增规则</NButton>
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">刷新</NButton>
      </template>
    </PageHeader>

    <NAlert v-if="loadError" type="error" :bordered="false" style="margin-bottom: var(--space-4)">
      读不到防火墙状态：{{ loadError }}
    </NAlert>
    <NAlert v-else-if="st && st.tool === 'none'" type="warning" :bordered="false" style="margin-bottom: var(--space-4)">
      这台机器上既没有 ufw 也没有 firewalld，面板不做自动安装：装防火墙属于会改变主机可达性的操作，请你自己选包、配默认策略再回来。
    </NAlert>
    <NAlert v-else-if="blindNote" type="warning" :bordered="false" style="margin-bottom: var(--space-4)">
      <div class="blind-title">{{ stateBad ? '防火墙状态读不到' : '放行规则表读不到' }}</div>
      <div class="blind-body cp-break">{{ blindNote }}</div>
      <div class="blind-body cp-break">原因：{{ stateBad || rulesBad }}</div>
      <NButton size="tiny" tertiary style="margin-top: var(--space-3)" :loading="loading"
               :icon="ico('RefreshOutline')" @click="load">重新读取</NButton>
    </NAlert>

    <NCard size="small" style="margin-bottom: var(--space-4)">
      <template #header>
        <span class="section-title">
          <NIcon :component="icons.ShieldCheckmarkOutline" :size="15" color="var(--cp-brand-soft)" /> 状态
        </span>
      </template>
      <NSpace :size="20" wrap>
        <div class="kv"><span class="k">工具</span><span class="v mono">{{ st?.tool || '—' }}</span></div>
        <div class="kv"><span class="k">运行状态</span><span class="v" :class="{ 'v-unknown': stateBad }">{{ stateText }}</span></div>
        <div v-if="st?.default_incoming" class="kv"><span class="k">默认入站</span><span class="v mono">{{ st.default_incoming }}</span></div>
        <div v-if="st?.zone" class="kv"><span class="k">默认区域</span><span class="v mono">{{ st.zone }}</span></div>
        <div v-if="st?.logging" class="kv"><span class="k">日志</span><span class="v mono">{{ st.logging }}</span></div>
        <div class="kv"><span class="k">规则条数</span><span class="v" :class="{ 'v-unknown': rulesBad }">{{ rulesBad ? '没读到' : rules.length }}</span></div>
      </NSpace>
      <div v-if="protectedList.length" class="prot-row">
        <NText depth="3" style="font-size: var(--fs-xs)">受保护端口（面板不会删除或拒绝这些端口的放行）：</NText>
        <NSpace :size="6" wrap style="margin-top: var(--space-2)">
          <NTag v-for="p in protectedList" :key="p.port" size="small" :bordered="false" type="info">
            {{ p.port }} · {{ p.why }}
          </NTag>
        </NSpace>
      </div>
      <div v-if="isAdmin && st && st.tool !== 'none'" style="margin-top: var(--space-4)">
        <NPopconfirm v-if="!toggleBlocked" @positive-click="toggle(!st.active)">
          <template #trigger>
            <NButton size="small" :type="st.active ? 'error' : 'success'" tertiary :loading="busy === 'state'" :disabled="!!busy">
              {{ st.active ? `停用 ${toolLabel}` : `启用 ${toolLabel}` }}
            </NButton>
          </template>
          <div v-if="st.active" class="confirm-txt">
            停用后 {{ toolLabel }} 不再拦任何入站连接，所有监听端口直接暴露到公网（{{ openPorts }} 条业务放行规则会一起失效）。
            面板不会替你重新开启，请确认你确实需要排障。
          </div>
          <div v-else class="confirm-txt">
            启用会立刻按当前规则拦接入站连接。SSH 端口与面板入口端口必须已经在放行列表里，否则面板会拒绝执行并把缺哪个端口告诉你。
          </div>
        </NPopconfirm>
        <template v-else>
          <NButton size="small" tertiary disabled>
            {{ st.active ? `停用 ${toolLabel}` : `启用 ${toolLabel}` }}
          </NButton>
          <div class="cell-sub cp-break" style="margin-top: var(--space-2)">{{ toggleBlockedWhy }}</div>
        </template>
      </div>
    </NCard>

    <NCard size="small" style="margin-bottom: var(--space-4)">
      <template #header>
        <span class="section-title">
          <NIcon :component="icons.LayersOutline" :size="15" color="var(--cp-ok)" /> 规则
          <NText depth="3" style="font-size: var(--fs-2xs); margin-left: 6px">
            {{ rulesBad ? '这一份表没读到，空表不等于「机器上没有放行规则」'
               : (isFirewalld ? '运行期配置；新增会同时写入 permanent 并重载' : '来自 ufw 的编号表，编号会随删除前移') }}
          </NText>
        </span>
      </template>
      <NDataTable
        v-if="!narrow"
        :columns="columns"
        :data="rules"
        :loading="loading"
        :bordered="false"
        size="small"
        :scroll-x="'auto'"
        :row-key="(r: any) => r.num"
      >
        <template #empty>
          <EmptyBox v-if="rulesBad" text="规则表没读到，这里列不出来"
                    :hint="rulesBad" />
          <EmptyBox v-else :text="st?.tool === 'none' ? '这台机器上没有可管理的防火墙' : '还没有任何入站规则；默认入站策略为拒绝时，新端口需要显式放行'" />
        </template>
      </NDataTable>
      <NSpace v-else vertical :size="8">
        <div v-for="r in rules" :key="r.num" class="rule-card">
          <div class="rule-main">
            <div class="mono rule-to">{{ portText(r) }}</div>
            <div class="cell-sub cp-break">{{ r.source || '任意地址' }}{{ r.comment ? ` · ${r.comment}` : '' }}</div>
          </div>
          <div class="rule-side">
            <NTag size="small" :bordered="false" :type="ACTION_META[r.action]?.type || 'default'">
              {{ ACTION_META[r.action]?.text || r.action }}
            </NTag>
            <span v-if="r.protected" class="cell-sub">受保护</span>
            <NButton v-else-if="isAdmin" text size="small" type="error" class="cp-op"
                    :loading="busy === `del-${r.num}`" :disabled="!!busy" @click="removeRule(r)">删除</NButton>
          </div>
        </div>
        <EmptyBox v-if="!rules.length && !loading && rulesBad" text="规则表没读到，这里列不出来" :hint="rulesBad" />
        <EmptyBox v-else-if="!rules.length && !loading" text="当前没有入站规则" />
        <NText v-if="enrichSkipped" depth="3" class="cp-break" style="font-size: var(--fs-2xs); display: block">
          其中 {{ enrichSkipped }} 条 service 规则的端口定义本轮没查（只核对前 {{ enrichMax }} 条）：
          按读不到处理，删它会拒绝，也不会据此断定它的端口通不通。
        </NText>
      </NSpace>
    </NCard>

    <NModal v-model:show="showAdd" preset="card" title="新增规则" style="width: 480px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NAlert type="info" :bordered="false" size="small">
          面板只执行不会把你关在门外的操作：拒绝 SSH / 面板入口端口的放行、删掉这些端口唯一的放行来源，都会被直接拒绝。
        </NAlert>
        <NAlert v-if="blindNote" type="warning" :bordered="false" size="small">{{ blindNote }}</NAlert>
        <div class="field">
          <span class="lab">端口</span>
          <NSpace :size="8" align="center">
            <NInput v-model:value="form.port" type="text" placeholder="8080" maxlength="5" style="width: 110px; max-width: 100%" />
            <NText depth="3" style="font-size: var(--fs-xs)">到</NText>
            <NInput v-model:value="form.port_to" type="text" placeholder="可空" maxlength="5" style="width: 110px; max-width: 100%" />
          </NSpace>
        </div>
        <div class="field"><span class="lab"></span><NText depth="3" style="font-size: var(--fs-2xs)">{{ rangeHint }}</NText></div>
        <div class="field">
          <span class="lab">协议</span>
          <NSpace :size="6">
            <NButton v-for="p in ['tcp', 'udp', 'both']" :key="p" size="tiny" :tertiary="form.proto !== p"
                    :type="form.proto === p ? 'primary' : 'default'" @click="form.proto = p">{{ p }}</NButton>
          </NSpace>
        </div>
        <div class="field">
          <span class="lab">动作</span>
          <NSpace :size="6">
            <NButton v-for="a in [['allow', '放行'], ['deny', '拒绝']]" :key="a[0]" size="tiny"
                    :tertiary="form.action !== a[0]" :type="form.action === a[0] ? (a[0] === 'deny' ? 'error' : 'success') : 'default'"
                    @click="form.action = a[0]">{{ a[1] }}</NButton>
          </NSpace>
        </div>
        <div v-if="form.action === 'allow'" class="field">
          <span class="lab">限速</span>
          <NSpace :size="8" align="center">
            <NSwitch v-model:value="form.rate" size="small" />
            <NText depth="3" style="font-size: var(--fs-2xs)">ufw 的 limit：连接频率受限但不断开已有会话</NText>
          </NSpace>
        </div>
        <div class="field">
          <span class="lab">来源</span>
          <NInput v-model:value="form.source" placeholder="留空表示任意地址；或 10.0.0.0/8" style="width: 240px; max-width: 100%" />
        </div>
        <div class="field">
          <span class="lab">备注</span>
          <NInput v-model:value="form.comment" placeholder="例如 office vpn" maxlength="40" style="width: 240px; max-width: 100%" />
        </div>
        <NText v-if="isFirewalld && !form.source" depth="3" style="font-size: var(--fs-2xs)">
          firewalld 上无来源的放行会写成端口规则；带来源则写成 rich rule（面板按来源的地址族选 IPv4/IPv6）。
        </NText>
      </NSpace>
      <template #footer>
        <NSpace justify="end" :size="8">
          <NButton size="small" tertiary @click="showAdd = false">取消</NButton>
          <NButton size="small" type="primary" :loading="busy === 'add'" :disabled="!!busy" @click="submitAdd">添加</NButton>
        </NSpace>
      </template>
    </NModal>

    <NModal v-model:show="showRaw" preset="card" :title="`${toolLabel} 原始输出`" style="width: 640px; max-width: 94vw">
      <div class="log-view" style="max-height: 420px">{{ st?.output || '（无输出）' }}</div>
      <NText v-if="blindNote" depth="3" style="font-size: var(--fs-2xs); display: block; margin-top: var(--space-2)">
        这份原文不完整：命令没跑成的那几次不会留下任何正文，空白不等于防火墙里没有内容。
      </NText>
      <NText depth="3" style="font-size: var(--fs-2xs); display: block; margin-top: var(--space-2)">
        原文来自 {{ toolLabel }} 自身，用于排障对照；规则操作请看上表，编号在两次刷新之间会变。
      </NText>
    </NModal>
  </div>
</template>

<style scoped>
:deep(.mono) {
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--cp-text);
}
.kv { display: flex; flex-direction: column; gap: 2px; min-width: 92px; }
.kv .k { font-size: var(--fs-2xs); color: var(--cp-text-mute); }
.kv .v { font-size: var(--fs-sm); color: var(--cp-text); }
.v-unknown { color: var(--cp-warn); }
.blind-title { font-size: var(--fs-sm); font-weight: 600; }
.blind-body { margin-top: 4px; font-size: var(--fs-xs); line-height: 1.6; }
.prot-row {
  margin-top: var(--space-4);
  padding-top: var(--space-3);
  border-top: 1px dashed var(--cp-line);
}
.confirm-txt { max-width: 380px; font-size: var(--fs-xs); line-height: 1.6; }
.field { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.field .lab { width: 56px; flex: 0 0 56px; font-size: var(--fs-xs); color: var(--cp-text-dim); }
.rule-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--cp-line);
  border-radius: var(--radius-sm);
  background: var(--cp-bg-2);
}
.rule-main { min-width: 0; }
.rule-to { font-size: var(--fs-sm); font-weight: 550; }
.rule-side { display: flex; align-items: center; gap: var(--space-2); flex: 0 0 auto; }
@media (max-width: 900px) {
  /* .cp-op 把横向内边距清成 0，两字的删除按钮实测 28x44，拇指点不准。
     行右侧有整行余量，补宽不会像工具条那样把按钮顶出视口。 */
  .rule-side .cp-op { padding-inline: 12px; }
}
</style>
