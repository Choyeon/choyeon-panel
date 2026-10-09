<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue';
import {
  NAlert, NCard, NDataTable, NIcon, NModal, NPopconfirm, NSpace, NTag, NText, useMessage,
} from 'naive-ui';
import { NButton } from '../ui';
import { getRole, req, siteHref } from '../api';
import { icon, icons } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';

/**
 * 接口封装放在页面里，而不是往 api.ts 里加方法：api.ts 是多个改动流共同的落点，
 * 收在自己文件内就不会在合并时互相覆盖。超时都 ≥ 服务端子进程上限，
 * 否则本地 abort 了远端 certbot 还在跑，用户重试会撞同一张证书。
 */
const api = {
  sites: () => req('/sites', { timeout: 30000 }),
  ssl: () => req('/ssl/certs', { timeout: 60000 }),
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

async function applySite(r: any) {
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
  busy.value = { key: `cert-${c.name}`, verb: 'renew' };
  try {
    const r = await api.renew(c.name);
    renewLog.value = r.log || '（certbot 无输出）';
    msg.success('续期已执行');
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
          h('div', { class: 'cell-sub', title: r.app || 'nginx 手工配置，未纳管为面板应用' },
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
    ellipsis: { tooltip: true },
    render: (r: any) => h('span', { class: 'mono-dim' }, r.file || '—'),
  },
  {
    title: '操作',
    key: 'ops',
    width: 170,
    align: 'center',
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
            trigger: () => h(NButton, { ...txt, type: 'primary', loading: onKey(r.app_id, 'apply'), title: '按应用当前的域名/端口重新渲染 vhost' }, { default: () => '套用' }),
            default: () => '会重写该域名的 vhost 并 reload nginx；nginx -t 不通过时面板自动回滚。',
          }),
          h(NPopconfirm, { onPositiveClick: () => removeSite(r) }, {
            trigger: () => h(NButton, { ...txt, type: 'error', loading: onKey(r.app_id, 'remove') }, { default: () => '移除' }),
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
          h('div', { class: 'cell-sub', title: c.issuer || '签发者未知' }, c.issuer || '签发者未知'),
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
        h('div', { class: 'cell-sub' }, c.expiry ? String(c.expiry).replace('T', ' ').slice(0, 16) : '到期时间未知'),
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
    render: (c: any) =>
      isAdmin.value
        ? h(NPopconfirm, { onPositiveClick: () => renew(c) }, {
            trigger: () => h(NButton, {
              text: true, size: 'small', type: 'primary', class: 'cp-op',
              loading: busy.value?.key === `cert-${c.name}` && busy.value?.verb === 'renew',
              title: '执行 certbot renew（会检查全部证书，只续该续的）',
            }, { default: () => '续期' }),
            default: () => '调用 certbot renew：逐张检查并在 30 天窗口内续期，可能耗时数十秒到几分钟。',
          })
        : h('span', { class: 'cell-sub' }, '—'),
  },
];

function refresh() {
  loadSites();
  loadCerts();
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
        :scroll-x="1120"
        :row-key="(r: any) => r.domain"
      >
        <template #empty>
          <EmptyBox :text="sitesReason ? '当前无法列出站点，原因见上方提示' : 'nginx 配置里还没有对外站点；到应用页为应用配置域名即可生成反代'" />
        </template>
      </NDataTable>
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

        <NDataTable
          :columns="certColumns"
          :data="certs"
          :loading="loadingCerts"
          :bordered="false"
          size="small"
          :scroll-x="960"
          :row-key="(c: any) => c.name"
        >
          <template #empty>
            <EmptyBox :text="certsInfo.source === 'none' ? '读不到任何证书，原因见上方提示' : '本机目前没有证书；为应用申请 HTTPS 后这里会出现到期与续期信息'" />
          </template>
        </NDataTable>

        <div v-if="renewLog">
          <NText depth="3" style="font-size: var(--fs-xs)">最近一次续期输出（已隐去本机路径）</NText>
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
.timer-detail {
  margin-top: var(--space-1);
  font-size: var(--fs-2xs);
  color: var(--cp-text-mute);
  word-break: break-all;
}
</style>
