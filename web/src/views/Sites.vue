<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue';
import {
  NAlert, NCard, NDataTable, NIcon, NModal, NPopconfirm, NSpace, NTag, NText, useMessage,
} from 'naive-ui';
import { NButton } from '../ui';
import { getRole, req, siteHref } from '../api';
import { icon, icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import CopyButton from '../components/CopyButton.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { fmtDateTime } from '../composables/useFormat';

/**
 * 接口封装放在页面里，而不是往 api.ts 里加方法：api.ts 是多个改动流共同的落点，
 * 收在自己文件内就不会在合并时互相覆盖。超时都 ≥ 服务端子进程上限，
 * 否则本地 abort 了远端 certbot 还在跑，用户重试会撞同一张证书。
 */
const api = {
  sites: () => req('/sites', { timeout: 30000 }),
  ssl: () => req('/ssl/certs', { timeout: 60000 }),
  // 对账一轮要扫 nginx 配置、跑一次 ss、读一遍证书清单，上限给到 120s：
  // 前端先 abort 的话，用户看到的是「对账失败」，而后端那轮探测其实照样跑完并写进缓存。
  audit: (refresh: boolean) => req('/site-audit' + (refresh ? '?refresh=1' : ''), { timeout: 120000 }),
  config: (domain: string) => req(`/sites/${encodeURIComponent(domain)}/config`),
  apply: (id: number) => req(`/sites/${id}/apply`, { body: {}, timeout: 60000 }),
  remove: (id: number) => req(`/sites/${id}/vhost`, { method: 'DELETE', timeout: 60000 }),
  // 服务端 certbot renew 上限 600s，前端必须更慢一点才能看到真实结果
  renew: (name: string) => req(`/ssl/certs/${encodeURIComponent(name)}/renew`, { body: {}, timeout: 620000 }),
};

const msg = useMessage();
const isAdmin = computed(() => getRole() === 'admin');

const sites = ref<any[]>([]);
const sitesReason = ref<string | null>(null);
const certs = ref<any[]>([]);
const certsInfo = ref<any>({ source: 'none', reason: null, timer: null, hint: '', earliest: null });
const loadingSites = ref(false);
const loadingCerts = ref(false);
const busy = ref<{ key: string; verb: string } | null>(null);
const renewLog = ref('');
const renewSummary = ref('');
const showConfig = ref(false);
const cfg = ref<any>(null);
const cfgLoading = ref(false);

const ico = (name: string, size = 14) => icon(name, size);

const SITE_STATUS: Record<string, { type: any; text: string }> = {
  ok: { type: 'success', text: '正常' },
  expiring: { type: 'warning', text: '即将到期' },
  expired: { type: 'error', text: '已过期' },
  unknown: { type: 'default', text: '未知' },
};

const TIMER_BANNER: Record<string, 'success' | 'warning' | 'error' | 'info'> = {
  installed: 'success',
  missing: 'warning',
  unknown: 'warning',
};

const TIMER_TEXT: Record<string, string> = {
  installed: '已检测到续期定时器',
  missing: '未检测到续期定时器',
  unknown: '无法确认续期定时器',
};

const earliest = computed(() => certsInfo.value.earliest || null);
const earliestType = computed(() => SITE_STATUS[earliest.value?.status]?.type || 'default');

async function loadSites() {
  loadingSites.value = true;
  try {
    const r = await api.sites();
    sites.value = r.sites || [];
    sitesReason.value = r.reason || null;
  } catch (e: any) {
    sitesReason.value = e.message;
    sites.value = [];
  } finally {
    loadingSites.value = false;
  }
}

async function loadCerts() {
  loadingCerts.value = true;
  try {
    const r = await api.ssl();
    certs.value = r.certs || [];
    certsInfo.value = r;
  } catch (e: any) {
    certsInfo.value = { source: 'none', reason: e.message, timer: null, hint: '', earliest: null };
    certs.value = [];
  } finally {
    loadingCerts.value = false;
  }
}

function onKey(id: number, verb: string) {
  return busy.value?.key === `site-${id}` && busy.value?.verb === verb;
}
// 套用/移除/续期共用 busy，但互不排斥地都写 nginx 或 certbot：一次只允许一个在飞。
// 连点两次「续期」= 两个 certbot renew 并行抢 /etc/letsencrypt 的锁，第二个直接失败，
// 而用户看到的第一条结果是成功的，以为无事发生。
const busyAny = () => busy.value !== null;

async function applySite(r: any) {
  if (busyAny()) return;
  busy.value = { key: `site-${r.app_id}`, verb: 'apply' };
  try {
    await api.apply(r.app_id);
    msg.success(`已套用 ${r.domain} 的反向代理`);
    loadSites();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}

async function removeSite(r: any) {
  if (busyAny()) return;
  busy.value = { key: `site-${r.app_id}`, verb: 'remove' };
  try {
    const res = await api.remove(r.app_id);
    msg.success(res.removed ? `已移除 ${r.domain} 的 vhost` : '该应用没有由面板生成的 vhost');
    loadSites();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}

async function openConfig(r: any) {
  cfg.value = { domain: r.domain, file: '', content: '' };
  showConfig.value = true;
  cfgLoading.value = true;
  try {
    cfg.value = await api.config(r.domain);
  } catch (e: any) {
    msg.error(e.message);
    showConfig.value = false;
  } finally {
    cfgLoading.value = false;
  }
}

async function renew(c: any) {
  if (busyAny()) return;
  busy.value = { key: `cert-${c.name}`, verb: 'renew' };
  try {
    const r = await api.renew(c.name);
    renewLog.value = r.log || '（certbot 无输出）';
    // 「续期已执行」不等于「续到了」：certbot 对未到期的证书同样正常退出，
    // 结论取后端按到期日变化算出来的 summary。
    renewSummary.value = r.summary || '';
    if (r.reloaded === false && r.renewed?.length) msg.warning(`${r.summary}（nginx 重载失败，请手动 reload）`);
    else msg.success(r.summary || 'certbot 已执行');
    loadCerts();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}

function chips(r: any) {
  const out: any[] = [];
  out.push(h(NTag, { size: 'tiny', bordered: false, type: r.https ? 'success' : 'default' },
    { default: () => (r.https ? 'HTTPS' : 'HTTP') }));
  if (r.redirect) out.push(h(NTag, { size: 'tiny', bordered: false, type: 'info' }, { default: () => '跳转' }));
  if (r.websocket) out.push(h(NTag, { size: 'tiny', bordered: false, type: 'warning' }, { default: () => 'WebSocket' }));
  return out;
}

const siteColumns: any[] = [
  {
    title: '站点',
    key: 'domain',
    minWidth: 220,
    render: (r: any) => {
      const href = siteHref(r.domain);
      return h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [h(NIcon, { component: icons.GlobeOutline, size: 16, color: 'var(--cp-brand-soft)' })]),
        h('div', { class: 'cell-txt' }, [
          href
            ? h('a', { class: 'site-link', href, target: '_blank', rel: 'noopener noreferrer', title: `在新标签页打开 ${href}` }, r.domain)
            : h('span', { class: 'cell-name', title: r.domain }, r.domain),
          h('div', { class: 'cell-sub cp-prose', title: r.app || 'nginx 手工配置，未纳管为面板应用' },
            r.app || '未纳管的手工配置'),
        ]),
      ]);
    },
  },
  { title: '端口', key: 'port', width: 90, render: (r: any) => (r.port ? h('span', { class: 'mono-dim' }, String(r.port)) : h('span', { class: 'cell-sub' }, '—')) },
  {
    title: '状态',
    key: 'status',
    width: 110,
    render: (r: any) =>
      r.active === null
        ? h('span', { class: 'cell-sub' }, '非面板应用')
        : h('span', { class: `st ${r.active ? 'ok' : 'idle'}` }, [h('span', { class: `dot ${r.active ? 'ok' : 'idle'}` }), r.active ? '运行中' : '已停止']),
  },
  { title: '代理', key: 'flags', width: 210, render: (r: any) => h(NSpace, { size: 4, wrap: false }, () => chips(r)) },
  {
    title: '配置文件',
    key: 'file',
    width: 180,
    // 路径也是要在手机上看得全的东西：折行（三行封顶）而不是等 hover。
    render: (r: any) => h('span', { class: 'mono-dim cp-prose', title: r.file || '' }, r.file || '—'),
  },
  {
    title: '操作',
    key: 'ops',
    width: 170,
    align: 'center',
    fixed: 'right' as const,
    render: (r: any) => {
      const href = siteHref(r.domain);
      const txt = { text: true, size: 'small', class: 'cp-op' } as const;
      const btns: any[] = [
        href
          ? h(NButton, { ...txt, type: 'primary', tag: 'a', href, target: '_blank', rel: 'noopener noreferrer', title: `在新标签页打开 ${href}` }, { default: () => '打开' })
          : h(NButton, { ...txt, type: 'primary', disabled: true, title: '域名不合法，无法生成外链' }, { default: () => '打开' }),
      ];
      if (isAdmin.value) btns.push(h(NButton, { ...txt, type: 'primary', onClick: () => openConfig(r) }, { default: () => '配置' }));
      if (isAdmin.value && r.app_id != null) {
        btns.push(
          h(NPopconfirm, { onPositiveClick: () => applySite(r) }, {
            trigger: () => h(NButton, { ...txt, type: 'primary', loading: onKey(r.app_id, 'apply'), disabled: busyAny(), title: '按应用当前的域名/端口重新渲染 vhost' }, { default: () => '套用' }),
            default: () => '会重写该域名的 vhost 并 reload nginx；nginx -t 不通过时面板自动回滚。',
          }),
          h(NPopconfirm, { onPositiveClick: () => removeSite(r) }, {
            trigger: () => h(NButton, { ...txt, type: 'error', loading: onKey(r.app_id, 'remove'), disabled: busyAny() }, { default: () => '移除' }),
            default: () => '移除此域名的 vhost（应用本身保留），nginx 校验通过后 reload。',
          }),
        );
      }
      return h('div', { class: 'cp-ops' }, btns);
    },
  },
];

const certColumns: any[] = [
  {
    title: '证书',
    key: 'name',
    minWidth: 200,
    render: (c: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: icons.ShieldCheckmarkOutline, size: 16, color: SITE_STATUS[c.status]?.type === 'error' ? 'var(--cp-err)' : 'var(--cp-ok)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name', title: c.certbot_id || c.name }, c.name),
          h('div', { class: 'cell-sub cp-prose', title: c.issuer || '签发者未知' }, c.issuer || '签发者未知'),
        ]),
      ]),
  },
  {
    title: '覆盖域名',
    key: 'domains',
    minWidth: 240,
    // 域名可能十几个，必须折行而不是把列撑开（mono + break-all）
    render: (c: any) => h('div', { class: 'cert-doms' }, (c.domains || []).join(' · ') || '—'),
  },
  {
    title: '剩余天数',
    key: 'days_left',
    width: 130,
    render: (c: any) => {
      const cls = c.status === 'expired' ? 'err' : c.status === 'expiring' ? 'warn' : c.status === 'ok' ? 'ok' : 'idle';
      return h('div', { class: 'cell-txt' }, [
        h('span', { class: `st ${cls}` }, c.days_left === null ? '未知' : `${c.days_left} 天`),
        // 原来这里自己 slice(0, 16)：把秒和 +00:00 一起砍掉，列头又没写时区，
        // 于是"到期时间"读起来像本地时间（本机 +8 就少看 8 小时）。
        // 现在走全站同一套 fmtDateTime（到秒），时区在列头写明，完整原值放 tooltip。
        h('div', { class: 'cell-sub cp-prose', title: c.expiry || '到期时间未知' }, c.expiry ? `${fmtDateTime(c.expiry)} UTC` : '到期时间未知'),
      ]);
    },
  },
  {
    title: '状态',
    key: 'status',
    width: 150,
    render: (c: any) => {
      const meta = SITE_STATUS[c.status] || SITE_STATUS.unknown;
      return h(NSpace, { size: 4, wrap: false }, () => [
        h(NTag, { size: 'small', bordered: false, type: meta.type }, { default: () => meta.text }),
        ...(c.invalid ? [h(NTag, { size: 'tiny', bordered: false, type: 'error' }, { default: () => '已吊销/过期' })] : []),
      ]);
    },
  },
  {
    title: '操作',
    key: 'ops',
    width: 90,
    align: 'center',
    fixed: 'right' as const,
    render: (c: any) =>
      isAdmin.value
        ? h(NPopconfirm, { onPositiveClick: () => renew(c) }, {
            trigger: () => h(NButton, {
              text: true, size: 'small', type: 'primary', class: 'cp-op',
              loading: busy.value?.key === `cert-${c.name}` && busy.value?.verb === 'renew',
              disabled: busyAny(),
              title: '执行 certbot renew（会检查全部证书，只续该续的）',
            }, { default: () => '续期' }),
            default: () => '调用 certbot renew：逐张检查并在 30 天窗口内续期，可能耗时数十秒到几分钟。',
          })
        : h('span', { class: 'cell-sub' }, '—'),
  },
];

/* ---------------------------------------------------------------- 站点链路对账 */
/**
 * 上面两张表各答一段：站点列表说「nginx 里有这个 server_name」，证书表说「这张证书还剩几天」。
 * 这一卡答中间那段——域名被访问时到底拿不拿得到响应。判词全部来自后端
 * `site_audit_service.audit()`（与自检项「站点链路」、`choyeonctl site-audit` 同一份），
 * 页面不解析 nginx、不数端口、不算证书天数：同一个事实两处判断，阈值一改就分叉。
 */
const auditData = ref<any | null>(null);
const auditErr = ref<string | null>(null);
const loadingAudit = ref(false);
// 「知道就好」那一档（异地目标、面板外 vhost）与要紧项混在同一个 findings 里，默认收起：
// 一屏黄条会让人把真正的 502 也当成背景噪音。
const showInfo = ref(false);

async function loadAudit(refresh = false) {
  loadingAudit.value = true;
  try {
    auditData.value = await api.audit(refresh);
    auditErr.value = null;
  } catch (e: any) {
    auditErr.value = e.message;
  } finally {
    loadingAudit.value = false;
  }
}

const SEVERITY: Record<string, { text: string; type: any; icon: string }> = {
  risk: { text: '要紧', type: 'error', icon: 'ShieldOutline' },
  warn: { text: '值得处理', type: 'warning', icon: 'WarningOutline' },
  info: { text: '知道就好', type: 'default', icon: 'InformationCircleOutline' },
};
const sevOf = (k: string) => SEVERITY[k] || SEVERITY.info;

const auditFindings = computed<any[]>(() => auditData.value?.findings ?? []);
const auditLinks = computed<any[]>(() => auditData.value?.links ?? []);
const auditCounts = computed(() => auditData.value?.counts ?? { risk: 0, warn: 0, info: 0 });
const shownFindings = computed(() =>
  (showInfo.value ? auditFindings.value : auditFindings.value.filter((f: any) => f.severity !== 'info')));

/** 三条判据各自的来路：任何一条读不到，靠它的那类判词就只能标未确认，必须显出来。 */
const auditFacts = computed(() => {
  const d: any = auditData.value;
  if (!d) return [];
  const ng = d.nginx || {};
  const li = d.listen || {};
  const ce = d.certs || {};
  return [
    {
      k: '配置',
      ok: !!ng.ok,
      good: `${ng.files} 份配置 · ${ng.domains} 个域名`,
      bad: ng.reason || '读不到 nginx 配置',
    },
    {
      k: '监听表',
      ok: !!li.ok,
      good: `${li.ports} 个端口在听`,
      bad: `${li.reason || '监听表读不到'}——这一轮反代目标全部标未确认，不等于都在听`,
    },
    {
      k: '证书清单',
      ok: !!ce.ok,
      good: `覆盖 ${ce.domains} 个域名（到期天数以下方证书表为准）`,
      bad: `${ce.reason || '证书清单读不到'}——这一轮覆盖关系判不了`,
    },
  ];
});
const auditBlind = computed(() => auditFacts.value.filter((f: any) => !f.ok));

const auditTone = computed(() => {
  if (!auditData.value) return { type: 'default' as const, text: auditErr.value ? '对账失败' : '还没对上' };
  const c = auditCounts.value;
  if (c.risk) return { type: 'error' as const, text: `${c.risk} 处链路要紧` };
  if (c.warn) return { type: 'warning' as const, text: `${c.warn} 处值得处理` };
  if (c.info) return { type: 'info' as const, text: `链路对得上 · ${c.info} 条只是让你知道` };
  return { type: 'success' as const, text: '链路对得上' };
});

/** 反代目标一格三种答案：有人听 / 没人听 / 判不了。「判不了」绝不折成「没人听」。 */
function targetMarks(link: any) {
  const rows = (link.targets || []).filter((t: any) => t.port !== null && t.port !== undefined);
  if (!rows.length) return [h('span', { class: 'sa-plain' }, '非反代（静态或 root）')];
  return rows.map((t: any) => {
    const label = `${t.host}:${t.port}`;
    if (t.listening === true) return h('span', { class: 'sa-mark ok mono' }, label);
    if (t.listening === false) return h('span', { class: 'sa-mark err mono' }, `${label} 没人听`);
    // 本机但监听表没读到，与目标根本不在这台机器：两种「判不了」原因不同，tooltip 分开写。
    return h('span', {
      class: 'sa-mark mute mono',
      title: t.local ? '监听表这一轮读不到，没有判它' : '这个目标不在本机，本机的监听表对不上它',
    }, `${label} 未确认`);
  });
}

function certCell(r: any) {
  if (!r.ssl) return h('span', { class: 'sa-plain' }, '不涉 https');
  if (!auditData.value?.certs?.ok) return h('span', { class: 'st idle' }, '覆盖未确认');
  const bad = r.uncovered || [];
  if (!bad.length) return h('span', { class: 'st ok' }, '证书已覆盖');
  return h('span', { class: 'st err' }, `${bad.length} 个域名不在证书里`);
}

function plainCell(r: any) {
  if (r.no_redirect) return h('span', { class: 'st warn' }, '明文可达');
  if (r.redirect) return h('span', { class: 'st ok' }, '已跳转 https');
  const l: number[] = r.listen || [];
  if (!l.includes(80)) return h('span', { class: 'sa-plain' }, '不接 80');
  return h('span', { class: 'sa-plain' }, '80 在听但没跳转');
}

const linkColumns: any[] = [
  {
    title: '域名 / 配置文件',
    key: 'domains',
    minWidth: 220,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name cp-break' }, (r.domains || []).join(' · ') || '（无 server_name）'),
      h('div', { class: 'cell-sub mono-dim cp-break' }, r.file),
    ]),
  },
  { title: '反代目标', key: 'targets', minWidth: 230, render: (r: any) => h('div', { class: 'sa-marks' }, targetMarks(r)) },
  {
    title: '本块监听',
    key: 'listen',
    minWidth: 104,
    render: (r: any) => h('span', { class: 'mono-dim cp-break' }, (r.listen || []).join(' / ') || '—'),
  },
  { title: '证书覆盖', key: 'cert', minWidth: 150, render: certCell },
  { title: '明文 80', key: 'plain', minWidth: 150, render: plainCell },
  {
    title: '认领',
    key: 'app',
    minWidth: 130,
    // 认领不上不是错误：手工 vhost、面板自己的反代都长这样，只是面板的启停够不到它。
    render: (r: any) => (r.app
      ? h('span', { class: 'cell-name', title: r.app }, r.app)
      : h('span', { class: 'sa-plain' }, '面板外配置')),
  },
];

const findingColumns: any[] = [
  {
    title: '判词',
    key: 'title',
    minWidth: 320,
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, {
            component: icons[sevOf(r.severity).icon],
            size: 17,
            color: r.severity === 'risk' ? 'var(--cp-err)' : r.severity === 'warn' ? 'var(--cp-warn)' : 'var(--cp-text-mute)',
          }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name cp-prose' }, r.title),
          h('div', { class: 'cell-sub cp-prose' }, r.detail),
        ]),
      ]),
  },
  {
    title: '档位', key: 'severity', width: 96,
    render: (r: any) => h(NTag, { size: 'small', type: sevOf(r.severity).type, bordered: false }, () => sevOf(r.severity).text),
  },
  {
    // 这一列后端给的就是能当场粘贴的只读命令（ss / certbot / curl），所以配复制按钮。
    // 面板不替你执行：reload nginx、改 vhost、签证书这三件事改错了是全站域名一起不可用。
    title: '自己核对', key: 'fix', minWidth: 260,
    render: (r: any) =>
      h('div', { class: 'sa-fix' }, [
        h('code', { class: 'sa-cmd' }, r.fix || '—'),
        r.fix ? h(CopyButton, { text: String(r.fix), label: `复制「${r.title}」的核对命令` }) : null,
      ]),
  },
];

function refresh() {
  loadSites();
  loadCerts();
  loadAudit();
}

onMounted(refresh);
</script>

<template>
  <div>
    <PageHeader title="网站与证书" :sub="`共 ${sites.length} 个站点 · ${certs.length} 张证书。站点信息来自 nginx 配置与应用记录的对照结果，面板不额外维护一份域名表`">
      <template #actions>
        <NTag v-if="earliest" size="small" :bordered="false" :type="earliestType">
          最早到期 {{ earliest.name }} · {{ earliest.days_left == null ? '未知' : `${earliest.days_left} 天` }}
        </NTag>
        <NTag v-else-if="certsInfo.reason" size="small" :bordered="false" type="default">证书最早到期：未知</NTag>
        <NButton size="small" tertiary :loading="loadingSites || loadingCerts" :icon="ico('RefreshOutline')" @click="refresh">刷新</NButton>
      </template>
    </PageHeader>

    <NCard size="small" style="margin-bottom: var(--space-4)">
      <template #header>
        <span class="section-title">
          <NIcon :component="icons.GlobeOutline" :size="15" color="var(--cp-brand-soft)" /> 网站列表
        </span>
      </template>

      <NAlert v-if="sitesReason" type="warning" :bordered="false" size="small" style="margin-bottom: var(--space-3)">
        {{ sitesReason }}
      </NAlert>
      <NDataTable
        :columns="siteColumns"
        :data="sites"
        :loading="loadingSites"
        :bordered="false"
        size="small"
        :scroll-x="'auto'"
        :row-key="(r: any) => r.domain"
      >
        <template #empty>
          <EmptyBox :text="sitesReason ? '当前无法列出站点，原因见上方提示' : 'nginx 配置里还没有对外站点；到应用页为应用配置域名即可生成反代'" />
        </template>
      </NDataTable>
    </NCard>

    <NCard size="small" style="margin-bottom: var(--space-4)">
      <template #header>
        <div class="sa-head">
          <span class="section-title">
            <NIcon :component="icons.GitNetworkOutline" :size="15" color="var(--cp-brand-soft)" /> 站点链路对账
          </span>
          <NTag size="small" :bordered="false" :type="auditTone.type">{{ auditTone.text }}</NTag>
          <NButton size="small" tertiary :loading="loadingAudit" :icon="ico('SyncOutline')"
                   title="重扫 nginx 配置、监听端口与证书清单（只读：不 reload、不改 vhost、不签证书）"
                   @click="loadAudit(true)">重新对账</NButton>
        </div>
      </template>

      <NSpace vertical :size="12">
        <NAlert v-if="auditErr && !auditData" type="error" :bordered="false" size="small">
          链路对账失败：{{ auditErr }}。上下两张表都不受影响，这一卡只是判不了。
        </NAlert>
        <NAlert v-else-if="auditBlind.length" type="warning" :bordered="false" size="small">
          <div>这一轮有 {{ auditBlind.length }} 条判据读不到，相关结论只能标「未确认」而不是「正常」：</div>
          <div v-for="f in auditBlind" :key="f.k" class="sa-blind-line">{{ f.k }}：{{ f.bad }}</div>
        </NAlert>

        <div v-if="auditFacts.length" class="sa-chips">
          <span v-for="f in auditFacts" :key="f.k" class="sa-chip" :class="f.ok ? '' : 'warn'">
            <b>{{ f.k }}</b>{{ f.ok ? f.good : f.bad }}
          </span>
        </div>

        <NDataTable
          :columns="findingColumns" :data="shownFindings" size="small" :bordered="false"
          :loading="loadingAudit && !auditData" :scroll-x="'auto'" :row-key="(r: any) => r.id"
          class="cp-tbl"
        >
          <template #empty>
            <EmptyBox :text="auditErr && !auditData
              ? '链路对账没跑出来，原因见上方提示'
              : (auditFindings.length && !shownFindings.length
                ? `只有「知道就好」那一档（${auditCounts.info} 条），要点上方那颗按钮才看得见`
                : '反代目标都在监听、证书都覆盖、明文都跳转')" />
          </template>
        </NDataTable>

        <div v-if="auditCounts.info" class="sa-toggle">
          <NButton text size="small" type="primary" class="cp-op" @click="showInfo = !showInfo">
            {{ showInfo ? '收起' : '展开' }}「知道就好」那一档（{{ auditCounts.info }} 条）
          </NButton>
          <NText depth="3" class="sa-hint">
            异地反代目标与面板外的 vhost 判不了，也不代表它们坏了，只是让你知道该以哪个地址为准。
          </NText>
        </div>

        <div>
          <div class="sa-block">
            <span class="section-title">
              <NIcon :component="icons.LayersOutline" :size="15" color="var(--cp-brand-soft)" /> 逐条链路
            </span>
            <NText depth="3" class="sa-src">
              {{ auditData ? `共 ${auditLinks.length} 个 server 块 · 检查时间 ${auditData.as_of}` : '读不到就不列，这里不猜' }}
            </NText>
          </div>
          <NDataTable
            :columns="linkColumns" :data="auditLinks" size="small" :bordered="false"
            :loading="loadingAudit && !auditData" :scroll-x="'auto'"
            :row-key="(r: any) => `${r.file}|${(r.domains || []).join(',')}`" class="cp-tbl"
          >
            <template #empty>
              <EmptyBox :text="auditErr && !auditData ? '链路对账没跑出来，原因见上方提示' : 'nginx 加载的配置里一个 server 块都没有'" />
            </template>
          </NDataTable>
        </div>

        <div v-if="(auditData?.notes || []).length" class="sa-notes">
          <div v-for="(n, i) in auditData.notes" :key="i" class="sa-note">{{ n }}</div>
        </div>
      </NSpace>
    </NCard>

    <NCard size="small">
      <template #header>
        <span class="section-title">
          <NIcon :component="icons.ShieldCheckmarkOutline" :size="15" color="var(--cp-ok)" /> 证书
        </span>
      </template>
      <NSpace vertical :size="12">
        <NAlert :type="TIMER_BANNER[certsInfo.timer?.status] || 'warning'" :bordered="false" size="small">
          <div>{{ TIMER_TEXT[certsInfo.timer?.status] || '无法确认续期定时器' }}：{{ certsInfo.hint || certsInfo.reason || '接口未返回定时器状态' }}</div>
          <div v-if="certsInfo.timer?.detail" class="timer-detail">{{ certsInfo.timer.detail }}</div>
        </NAlert>

        <NAlert v-if="certsInfo.source === 'openssl'" type="info" :bordered="false" size="small">
          本机没有可用的 certbot，以下清单由 openssl 直接读取证书文件得到（{{ certsInfo.reason }}）。
        </NAlert>
        <NAlert v-else-if="certsInfo.source === 'none'" type="error" :bordered="false" size="small">
          证书清单不可用：{{ certsInfo.reason }}。此处不伪造到期信息。
        </NAlert>

        <!-- 这条线只由后端给（来自告警配置 alert_ssl_days），页面不自己写数字：
             以前这里按写死的 21 天标"即将到期"，而通知按配置发，两个答案能差出一周。 -->
        <div class="cp-help">
          「即将到期」按设置里那条 SSL 剩余天数判：{{ Number(certsInfo.expiring_days) > 0
            ? `剩余 ≤ ${certsInfo.expiring_days} 天算即将到期`
            : '该提醒已关闭（配 0），这里只判有没有过期' }}；巡检发通知用的也是同一条。
        </div>

        <NDataTable
          :columns="certColumns"
          :data="certs"
          :loading="loadingCerts"
          :bordered="false"
          size="small"
          :scroll-x="'auto'"
          :row-key="(c: any) => c.name"
        >
          <template #empty>
            <EmptyBox :text="certsInfo.source === 'none' ? '读不到任何证书，原因见上方提示' : '本机目前没有证书；为应用申请 HTTPS 后这里会出现到期与续期信息'" />
          </template>
        </NDataTable>

        <div v-if="renewLog">
          <NText depth="3" style="font-size: var(--fs-xs)">{{ renewSummary || '最近一次续期输出' }}（输出已隐去本机路径）</NText>
          <div class="log-view" style="max-height: 240px; margin-top: var(--space-2)">{{ renewLog }}</div>
        </div>
      </NSpace>
    </NCard>

    <NModal v-model:show="showConfig" preset="card" :title="`vhost 配置：${cfg?.file || cfg?.domain || ''}`" style="width: 760px; max-width: 94vw">
      <NSpace vertical :size="10">
        <NText depth="3" style="font-size: var(--fs-xs)">
          只读预览。要改内容请到应用详情的 nginx 编辑器；面板只列出文件名，不回显本机绝对路径。
        </NText>
        <div v-if="cfgLoading" class="log-view" style="max-height: 420px">读取中…</div>
        <div v-else class="log-view" style="max-height: 420px">{{ cfg?.content || '（空文件）' }}</div>
      </NSpace>
    </NModal>
  </div>
</template>

<style scoped>
/* 域名链接：沿用外链的克制配色，hover 才提亮，避免列表里一排高亮链接 */
:deep(.site-link) {
  font-size: var(--fs-sm);
  font-weight: 550;
  color: var(--cp-text);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: color var(--dur) var(--ease);
}
:deep(.site-link:hover) { color: var(--cp-brand); text-decoration: underline; }
/* 列渲染函数产出的节点不带本组件的 scope id，普通 scoped 选择器匹配不到，必须走 :deep() */
:deep(.cert-doms) {
  font-family: 'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
  font-size: var(--fs-2xs);
  color: var(--cp-text-dim);
  line-height: 1.5;
  word-break: break-all;
}
/* 链路对账这一卡：判词、目标标记、可复制命令都长在 h() 生成的格子里，那些节点不带本组件的
   data-v 作用域，纯 class 的 scoped 规则一条都命中不了，必须从模板上的表级类 .cp-tbl 走 :deep
   （与服务器管理页同一套打法）。形态也刻意与那一页对齐，两页对照时不会看着像两种东西。 */
.cp-tbl :deep(.sa-marks) { display: flex; flex-direction: column; gap: 3px; }
.cp-tbl :deep(.sa-mark) { font-size: var(--fs-xs); line-height: 1.6; word-break: break-all; }
.cp-tbl :deep(.sa-mark.ok) { color: var(--cp-ok); }
.cp-tbl :deep(.sa-mark.err) { color: var(--cp-err); font-weight: 550; }
.cp-tbl :deep(.sa-mark.mute), .cp-tbl :deep(.sa-plain) { color: var(--cp-text-mute); }
.cp-tbl :deep(.sa-fix) { display: flex; align-items: center; gap: 6px; min-width: 0; }
.cp-tbl :deep(.sa-cmd) {
  flex: 0 1 auto; min-width: 0; padding: 3px 7px; border-radius: var(--radius-sm);
  background: var(--cp-bg-soft); font-size: var(--fs-xs); overflow-x: auto; white-space: nowrap;
}
.sa-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.sa-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.sa-chip {
  display: inline-flex; align-items: baseline; gap: 6px; max-width: 100%; padding: 4px 10px;
  border-radius: var(--radius-md); background: var(--cp-bg-soft); font-size: var(--fs-sm); color: var(--cp-text);
}
.sa-chip b { font-weight: 550; font-size: var(--fs-xs); color: var(--cp-text-mute); }
.sa-chip.warn { color: var(--cp-warn); }
.sa-blind-line { font-size: var(--fs-xs); color: var(--cp-text-dim); line-height: 1.7; word-break: break-word; }
.sa-block { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 6px; }
.sa-src, .sa-hint { font-size: var(--fs-xs); }
.sa-toggle { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.sa-notes { margin-top: -4px; }
.sa-note { font-size: var(--fs-xs); color: var(--cp-text-dim); line-height: 1.7; }

.timer-detail {
  margin-top: var(--space-1);
  font-size: var(--fs-2xs);
  color: var(--cp-text-mute);
  word-break: break-all;
}
</style>
