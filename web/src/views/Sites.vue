<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue';
import {
  NAlert, NCard, NDataTable, NIcon, NModal, NPopconfirm, NSelect, NSpace, NTag, NText, useMessage,
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
  // 纳管这三个动词只写面板自己的登记表：读一遍 nginx 配置（判定用）、落一行 SQLite，
  // 既不 run nginx -t 也不 reload，所以超时按普通接口给，不必像套用/续期那样放大。
  apps: () => req('/apps'),
  claim: (domain: string, appId?: number | null) =>
    req('/sites/claim', { body: { domain, app_id: appId ?? null }, timeout: 30000 }),
  unclaim: (domain: string) => req('/sites/unclaim', { body: { domain }, timeout: 30000 }),
  claimAll: () => req('/sites/claim-all', { body: {}, timeout: 60000 }),
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
/**
 * 两把单飞锁，按「动的是不是同一批对象」分，而不是按按钮长得像不像分：
 * - `busy`：nginx 写类（套用、移除、续期）。服务端都排在 `_nginx_section()` 那把锁后面，
 *   一次只有一份配置在改；前端 abort 也不会取消服务端已经排队的活儿，所以同类的连点必须挡。
 * - `regBusy`：登记表类（纳管、取消纳管、全部纳管、指定应用）。三个动词只往面板自己的
 *   SQLite 写一行，既不 nginx -t 也不 reload，跟 nginx 写类不碰同一个对象。
 * 合成一把的话，一次 `certbot renew`（前端超时 620s）会把纳管按钮锁死十分钟——
 * 那十分钟里登记表根本改不动，用户看到的就是"点了没反应"。
 * 同类里仍然只允许一个在飞：连点两次「续期」= 两个 certbot 并行抢 /etc/letsencrypt 的锁，
 * 第二个直接失败，而用户看到的第一条结果是成功的，以为无事发生；连点两次「纳管」撞的是
 * 同一行登记，第二次会把第一次的 note 覆盖掉，事后查审计看不出来发生过什么。
 */
const busy = ref<{ key: string; verb: string } | null>(null);
const regBusy = ref<{ key: string; verb: string } | null>(null);

function onKey(key: string, verb: string) {
  // 两把槽的 key 前缀不重叠（site-/cert- 对 claim-/unclaim-/claim-all），一个函数够用
  return (busy.value?.key === key && busy.value?.verb === verb)
    || (regBusy.value?.key === key && regBusy.value?.verb === verb);
}
const busyAny = () => busy.value !== null;
const regBusyAny = () => regBusy.value !== null;
const renewLog = ref('');
const renewSummary = ref('');
const showConfig = ref(false);
const cfg = ref<any>(null);
const cfgLoading = ref(false);
// 「指定应用」这一条路只在判不出归属的行上出现，选项来自 /api/apps（点开时才第一次拉）。
const apps = ref<any[]>([]);
const appsError = ref('');
const showAssign = ref(false);
const assignRow = ref<any>(null);
const assignId = ref<number | null>(null);

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

/* ------------------------------------------------------------------ 站点纳管 */
/**
 * 归属这件事有两个出处，页面只负责把它们分开摆：登记表（`claimed`）是人或批量纳管落下的
 * 那一条，判定（`suggest_*`）是面板按 nginx 现读出来的建议。以前这里只有「未纳管的手工配置」
 * 一句话，既不说是谁也没说为什么认不出；现在一行一行给依据，判不出的把"要怎么做才认得出"
 * 写在脸上（`suggest_reason`），按钮改成让人指定应用，而不是面板猜一个。
 * 三个动词都不写 nginx：纳管只落登记，取消纳管只删登记，域名照旧由原来那份配置服务。
 */
const claimedCount = computed(() => sites.value.filter((r: any) => r.claimed).length);
const unclaimedCount = computed(() => sites.value.length - claimedCount.value);
const undecidedCount = computed(() =>
  sites.value.filter((r: any) => !r.claimed && !r.suggest_kind).length);

const claimTone = computed(() => {
  if (!sites.value.length) return { type: 'default' as const, text: '没有站点' };
  if (!unclaimedCount.value) return { type: 'success' as const, text: `${claimedCount.value} 个域名全部已纳管` };
  const tail = undecidedCount.value ? `、${undecidedCount.value} 个判不出归属` : '';
  return { type: 'warning' as const, text: `${claimedCount.value}/${sites.value.length} 已纳管${tail}` };
});

/** 一行归属的来路要能逐条质疑：登记表写的、判定给的、判不出的原因，是三种不同的话。 */
function evidence(r: any) {
  if (r.claimed) return r.claim_note || '已登记归属（没有留说明）';
  if (r.suggest_note) return r.suggest_note;
  return r.suggest_reason || 'nginx 里查不到这个域名的归属线索';
}

/** 「建议」与「已登记」不许长成同一个样子：只有 claimed 才是不用再动的那一行。 */
function ownerLabel(r: any) {
  if (r.claimed) return r.app || '登记的应用已不存在';
  return r.app ? `建议 ${r.app}` : '未登记';
}

const tinyTag = (type: any, text: string) =>
  h(NTag, { size: 'tiny', bordered: false, type }, { default: () => text });

function claimTags(r: any) {
  const out: any[] = [];
  if (r.claimed) {
    out.push(tinyTag('success', '已纳管'));
    if (r.claim_source === 'manual') out.push(tinyTag('info', '手工指定'));
    if (r.claim_stale) out.push(tinyTag('warning', '认领的文件已不在'));
  } else if (r.suggest_kind) {
    out.push(tinyTag('default', '待纳管'));
  } else {
    out.push(tinyTag('warning', '判不出归属'));
  }
  // 差额由后端算（site_claim_service 是端口这件事的唯一出处），页面不做减法
  if (r.port_mismatch) out.push(tinyTag('error', `端口不符 ${r.port}≠登记 ${r.app_port}`));
  return out;
}

async function loadApps() {
  try {
    const r = await api.apps();
    apps.value = Array.isArray(r) ? r : [];
    appsError.value = '';
  } catch (e: any) {
    apps.value = [];
    // 读失败要留个痕：浮层里那句「面板里还没有登记任何应用」只有真的空清单时才成立，
    // 接口挂了也显示同一句，用户就会去新建一个其实已经存在的应用。
    appsError.value = e.message;
    msg.error(`应用清单读不到：${e.message}`);
  }
}

/** 返回 true 才让调用方收尾（关浮层），失败时浮层留着，用户还能改选择再试一次。 */
async function claimOne(r: any, appId?: number | null) {
  if (regBusyAny()) return false;
  regBusy.value = { key: `claim-${r.domain}`, verb: 'claim' };
  try {
    const res = await api.claim(r.domain, appId);
    msg.success(`已纳管 ${res.domain}：只写了登记表，nginx 配置未改动`);
    await loadSites();
    return true;
  } catch (e: any) {
    msg.error(e.message);
    return false;
  } finally {
    regBusy.value = null;
  }
}

async function unclaimOne(r: any) {
  if (regBusyAny()) return;
  regBusy.value = { key: `unclaim-${r.domain}`, verb: 'unclaim' };
  try {
    const res = await api.unclaim(r.domain);
    // removed=false 不粉饰成成功：按钮只给已纳管的行，点了却说"本就没有登记"，
    // 说明登记表和 nginx 已经对不上，这句话就是要人去看。
    msg[res.removed ? 'success' : 'warning'](
      res.removed ? `已取消纳管 ${res.domain}：nginx 配置一个字节都没动` : `${res.domain} 本就没有登记`,
    );
    await loadSites();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    regBusy.value = null;
  }
}

/** 批量纳管只带判定结果、不带 app_id：判不出的那些必须留在列表里当未纳管，而不是被猜走。 */
async function claimAllSites() {
  if (regBusyAny()) return;
  regBusy.value = { key: 'claim-all', verb: 'claim-all' };
  try {
    const res = await api.claimAll();
    const got = (res.claimed || []).length;
    const miss = (res.skipped || []).length;
    if (!got && !miss) msg.info('没有待纳管的域名：这些域名都已经登记过');
    else if (miss) msg.warning(`已纳管 ${got} 个，${miss} 个判不出归属（理由就在每行的依据里）——面板不替你猜应用`);
    else msg.success(`已纳管 ${got} 个域名：只写了登记表，nginx 配置未改动`);
    await loadSites();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    regBusy.value = null;
  }
}

function openAssign(r: any) {
  assignRow.value = r;
  assignId.value = null;
  showAssign.value = true;
  if (!apps.value.length) loadApps();
}

async function confirmAssign() {
  if (!assignRow.value || !assignId.value || regBusyAny()) return;
  if (await claimOne(assignRow.value, assignId.value)) showAssign.value = false;
}

const appOptions = computed(() => apps.value.map((a: any) => ({
  label: `${a.name}（id ${a.id}${a.port ? ` · 端口 ${a.port}` : ''}${a.domain ? ` · ${a.domain}` : ''}）`,
  value: a.id,
})));

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
    minWidth: 230,
    render: (r: any) => {
      const href = siteHref(r.domain);
      const why = evidence(r);
      return h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [h(NIcon, { component: icons.GlobeOutline, size: 16, color: 'var(--cp-brand-soft)' })]),
        h('div', { class: 'cell-txt' }, [
          href
            ? h('a', { class: 'site-link', href, target: '_blank', rel: 'noopener noreferrer', title: `在新标签页打开 ${href}` }, r.domain)
            : h('span', { class: 'cell-name', title: r.domain }, r.domain),
          // 这一行从「未纳管的手工配置」换成了归属的依据本身：同一句话既说清是谁、也说清
          // 凭什么，判不出的那行还能把"要怎么才认得出"直接读出来。
          h('div', { class: 'cell-sub cp-prose', title: why }, why),
        ]),
      ]);
    },
  },
  {
    title: '归属',
    key: 'claim',
    minWidth: 170,
    render: (r: any) => {
      const label = ownerLabel(r);
      return h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name cp-prose', title: label }, label),
        // 标签组合数不固定（已纳管/手工指定/文件已不在/端口不符），整项换行而不是 nowrap
        h(NSpace, { size: 4 }, () => claimTags(r)),
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
  // 标签行整项换行，不写 wrap:false：nowrap 时最先被挤扁的是尾部标签（它会缩到 1ch 再逐字断行），
  // 而这一列的标签组合数不固定（HTTP/HTTPS + 跳转 + WebSocket），宽度不该由装饰性标签决定。
  { title: '代理', key: 'flags', width: 210, render: (r: any) => h(NSpace, { size: 4 }, () => chips(r)) },
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
    width: 210,
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
      // 纳管这一组只碰登记表，紧挨着的套用/移除碰的是 nginx 本体：两句话必须写得不一样，
      // 否则点完「纳管」的人会以为自己的生产配置被重写过了。
      if (isAdmin.value && !r.claimed && r.suggest_kind) {
        btns.push(
          h(NPopconfirm, { onPositiveClick: () => claimOne(r) }, {
            trigger: () => h(NButton, { ...txt, type: 'primary', loading: onKey(`claim-${r.domain}`, 'claim'), disabled: regBusyAny(), title: `按面板判定登记归属（${r.suggest_note}）` }, { default: () => '纳管' }),
            default: () => `只往面板的登记表写这一行，并认领配置文件 ${r.file || '（未找到）'}；不写 nginx、不 reload，域名照旧由现在这份配置服务。判定依据：${r.suggest_note}`,
          }),
        );
      }
      if (isAdmin.value && !r.claimed && !r.suggest_kind) {
        btns.push(
          h(NButton, { ...txt, type: 'primary', onClick: () => openAssign(r), disabled: regBusyAny(), title: r.suggest_reason || '面板认不出这个域名归哪个应用' }, { default: () => '指定应用' }),
        );
      }
      if (isAdmin.value && r.claimed) {
        btns.push(
          h(NPopconfirm, { onPositiveClick: () => unclaimOne(r) }, {
            trigger: () => h(NButton, { ...txt, type: 'warning', loading: onKey(`unclaim-${r.domain}`, 'unclaim'), disabled: regBusyAny(), title: '只删这一行登记，nginx 的配置不动' }, { default: () => '取消纳管' }),
            default: () => `只删除 ${r.domain} 这一行登记。${r.file ? `${r.file} 一个字节都不动，` : ''}域名照旧在 nginx 里服务，只是面板重新把它当判不出归属的配置。`,
          }),
        );
      }
      if (isAdmin.value && r.app_id != null) {
        btns.push(
          h(NPopconfirm, { onPositiveClick: () => applySite(r) }, {
            trigger: () => h(NButton, { ...txt, type: 'primary', loading: onKey(`site-${r.app_id}`, 'apply'), disabled: busyAny(), title: '按应用当前的域名/端口重新渲染 vhost' }, { default: () => '套用' }),
            default: () => '会重写该域名的 vhost 并 reload nginx；nginx -t 不通过时面板自动回滚。',
          }),
          h(NPopconfirm, { onPositiveClick: () => removeSite(r) }, {
            trigger: () => h(NButton, { ...txt, type: 'error', loading: onKey(`site-${r.app_id}`, 'remove'), disabled: busyAny() }, { default: () => '移除' }),
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
      // 「即将到期」(56) + 「已吊销/过期」(76) + 间距 = 136，而 150 这一列只有 126 可用。
      // nowrap 时这一格的最小内容宽就是 136，浏览器只能把整列撑宽（实测 150→202）并挤掉覆盖域名列；
      // 整项换行后最小内容宽回到 76，列宽守住声明值，第二枚标签落到下一行。
      return h(NSpace, { size: 4 }, () => [
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
  const rows = link.targets || [];
  if (!rows.length) return [h('span', { class: 'sa-plain' }, '非反代（静态或 root）')];
  return rows.map((t: any) => {
    // 端口为空是这条反代地址根本没写端口（默认 80、命名上游、或者 unix 套接字）：
    // 它照样是一个反代目标，丢掉它就会把这行谎报成「非反代」。
    if (t.port === null || t.port === undefined) {
      return h('span', {
        class: 'sa-mark mute mono',
        title: '这条反代地址没写端口（默认 80、命名上游或 unix 套接字），本机监听表判不了它',
      }, `${t.host || '?'} 未确认`);
    }
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
          // 判词是三行封顶（.cp-prose），不是单行 ellipsis：结论不该等人点一下才露脸。
          // 但最长的 detail 会吃到第四行被裁掉半句，所以与同页其它 cp-prose 格一样把全文挂在 title 上。
          h('span', { class: 'cell-name cp-prose', title: r.title }, r.title),
          h('div', { class: 'cell-sub cp-prose', title: r.detail }, r.detail),
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
    <PageHeader title="网站与证书" :sub="`共 ${sites.length} 个域名 · ${claimedCount} 个已纳管 · ${certs.length} 张证书。归属以面板的纳管登记表为准，登记表没有的按 nginx 现读给出建议并写明依据；纳管不改动 nginx`">
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
        <div class="sa-head">
          <span class="section-title">
            <NIcon :component="icons.GlobeOutline" :size="15" color="var(--cp-brand-soft)" /> 网站列表
          </span>
          <NTag size="small" :bordered="false" :type="claimTone.type">{{ claimTone.text }}</NTag>
          <NPopconfirm v-if="isAdmin" @positive-click="claimAllSites">
            <template #trigger>
              <NButton size="small" tertiary :icon="ico('CheckmarkCircleOutline')"
                       :loading="onKey('claim-all', 'claim-all')" :disabled="regBusyAny() || !unclaimedCount"
                       title="逐条按面板判定登记归属；判不出归属的原样列出理由，不替你猜应用">全部纳管</NButton>
            </template>
            判得出归属的域名逐个落进登记表并认领配置文件；判不出的一个都不猜，会留在列表里写明理由。
            这一轮不写 nginx、不 reload、不签证书——线上流量不受影响。
          </NPopconfirm>
        </div>
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
      <div class="cp-help">
        「纳管」= 在面板里登记这个域名归哪个应用，并认领它所在的那份配置文件；它不改写 nginx 的内容，
        也不 reload，取消纳管同样只删这一行登记。「套用」「移除」才是重写 vhost 并 reload nginx 的那两个按钮。
        手工指定过的归属不会被「全部纳管」改回机器判的结果。
      </div>
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

    <NModal v-model:show="showAssign" preset="card" title="纳管并指定应用" style="width: 520px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NText depth="3" style="font-size: var(--fs-sm)">
          {{ assignRow?.domain }}：{{ assignRow?.suggest_reason || '面板认不出这个域名归哪个应用。' }}
        </NText>
        <div>
          <div class="assign-cap">指定为哪个应用</div>
          <NSelect v-model:value="assignId" :options="appOptions" placeholder="从应用清单里选一个" :disabled="regBusyAny()" />
          <div v-if="appsError" class="cp-help">
            应用清单这一次没读到（{{ appsError }}），下拉是空的并不等于面板里没有应用。
            关掉这个浮层重新点一次「指定应用」重试。
          </div>
          <div v-else-if="!appOptions.length" class="cp-help">
            面板里还没有登记任何应用。先把这个服务登记成应用（应用页 → 新建，端口按它实际监听的端口写），
            再回来纳管，归属信息才有地方挂。
          </div>
        </div>
        <div class="cp-help">
          指定之后面板就按这一条登记归属，并认领配置文件 {{ assignRow?.file || '（nginx 里没找到它的文件）' }}；
          面板自己的判定仍然作为「建议」显示，两边不一致会在归属那一列点出来。这一步不写 nginx、不 reload。
        </div>
        <NSpace justify="end" :size="8">
          <NButton size="small" tertiary @click="showAssign = false">取消</NButton>
          <NButton size="small" type="primary" :disabled="!assignId || regBusyAny()"
                   :loading="onKey('claim-' + (assignRow?.domain || ''), 'claim')" @click="confirmAssign">纳管</NButton>
        </NSpace>
      </NSpace>
    </NModal>

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

.assign-cap {
  margin-bottom: 6px;
  font-size: var(--fs-xs);
  color: var(--cp-text-mute);
}
</style>
