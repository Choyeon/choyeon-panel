<script setup lang="ts">
import { computed, ref, watch, h } from 'vue';
import { NDataTable, NInput, NSelect, NAlert, NTag, NText, useMessage, NIcon } from 'naive-ui';
import { NButton } from '../ui';
import { api, getRole } from '../api';
import { icon, icons } from '../icons';
import LogStream from '../components/LogStream.vue';
import PageTabBar from '../components/PageTabBar.vue';
import CpToolbarCard from '../components/CpToolbarCard.vue';
import CpRowActions, { type RowAction } from '../components/CpRowActions.vue';
import CpStatusTag from '../components/CpStatusTag.vue';
import ConfirmActionDialog from '../components/ConfirmActionDialog.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { useCachedFetch, invalidateCache } from '../composables/useCachedFetch';
import { useDelayedRefresh } from '../composables/useDelayedRefresh';
import { useTableHeight } from '../composables/useTableHeight';
import { useCopy } from '../composables/useCopy';
import { fmtDateTime } from '../composables/useFormat';

const msg = useMessage();
const isAdmin = getRole() === 'admin';
const tab = ref('services');
const filter = ref('');
const statusSel = ref<string[]>([]);
const showAll = ref(false);
const logUnit = ref<string | null>(null);
const busy = ref<{ unit: string; verb: string } | null>(null);
const unitBusy = (unit: string) => busy.value?.unit === unit;
const opRef = ref<InstanceType<typeof ConfirmActionDialog> | null>(null);

// systemd 的 unit 列表变化很快（启停之后 active 还要一会儿才落地），缓存给得很短：
// 秒开靠它，15s 后再进这一页仍会后台回源，不会读到半天的旧状态。
const { data, error, loading, refreshing, refresh } = useCachedFetch<any[]>({
  key: () => 'services',
  loader: () => api.services(),
  maxAge: 15000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });

// 「自启」状态不在 list-units 的输出里（那五个列是 UNIT/LOAD/ACTIVE/SUB/DESCRIPTION），
// 得单独走 list-unit-files。没有它之前，页面上那颗「自启」按钮既显示不出当前是否
// 已启用、也点不出"取消自启"，用户只能反复盲按 enable。
const { data: unitFiles } = useCachedFetch<Record<string, string>>({
  key: () => 'unit-files',
  loader: () => api.unitFiles(),
  maxAge: 30000,
});
// enable/disable 的响应里带服务端强制回读的新状态，先在本地生效；
// reload() 拿回整张表（服务端那份 30s 缓存已被同一次调用刷新）就让位给服务端值。
const enabledPatch = ref<Record<string, string>>({});
const enabledOf = (unit: string) => enabledPatch.value[unit] ?? unitFiles.value?.[unit] ?? '';

const rows = computed<any[]>(() => data.value ?? []);
// 延迟刷新不能拖过页面卸载：那是一次没人看的请求，失败还会把错误 toast 打到新页面上。
const { pending: pendingRefresh, later } = useDelayedRefresh();
const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

// 系统补丁是只读探测，服务端已有 30 分钟 TTL，这里再叠一层前端缓存：切走再回来秒开。
// 「刷新」读服务端缓存（快），「重新探测」带 refresh=1 强制回源（apt list 在包多的机器上
// 要跑几秒，所以它是用户主动点的按钮，不是每次进页都做）。
const forceProbe = ref(false);
const {
  data: patches, error: patchError, loading: patchLoading, refreshing: patchRefreshing, refresh: refreshPatches,
} = useCachedFetch<any>({
  key: () => 'updates',
  loader: () => api.updates(forceProbe.value),
  maxAge: 60000,
});
const probing = ref(false);
watch(patchError, (e) => { if (e) msg.error((e as Error).message || String(e)); });

async function repost() {
  forceProbe.value = true;
  probing.value = true;
  try {
    await refreshPatches();
  } catch {
    /* 失败已经通过 patchError 走上面的 toast */
  } finally {
    forceProbe.value = false;
    probing.value = false;
  }
}

const secCount = computed<number | null>(() => {
  const v = patches.value?.security_count;
  return typeof v === 'number' ? v : null;
});
const totalPending = computed(() => Number(patches.value?.pending_total) || 0);
const autoState = computed(() => patches.value?.auto_updates?.state || 'unknown');
const patchBusy = computed(() => patchLoading.value || patchRefreshing.value || probing.value);
// 判不出安全子集时不能说"没有安全更新"——那正是管理员最容易被劝走的一步。
const patchAlert = computed(() => {
  const p = patches.value;
  if (!p || patchError.value) return null;
  if (!p.supported) return { type: 'warning' as const, text: '未能识别这台机器的发行版或包管理器，面板不猜待装更新的数量。' };
  if (secCount.value === null) {
    return { type: totalPending.value ? 'warning' as const : 'info' as const, text: `${p.family} 判不出安全更新子集，只报待装总数 ${totalPending.value} 个。` };
  }
  if (secCount.value > 0) {
    return { type: 'warning' as const, text: `${secCount.value} 个待装安全更新（共 ${totalPending.value} 个待装）。安全更新是最常见的横向跳板，建议尽快到 SSH 里执行下方命令。` };
  }
  if (totalPending.value) return { type: 'info' as const, text: `没有待装安全更新，另有 ${totalPending.value} 个功能更新。` };
  return { type: 'success' as const, text: '补丁已全部装上（按本地包索引判断，索引过旧时结论会偏旧）。' };
});

const { copy } = useCopy();

function reload() {
  invalidateCache('services');
  invalidateCache('unit-files');
  // 启停/自启都会写一条审计：审计页的缓存同样作废，切过去不会看到旧记录
  invalidateCache('audit:');
  return refresh()
    .catch(() => undefined)
    .then(() => { enabledPatch.value = {}; });
}

const important = ['nginx', 'postgresql', 'redis', 'rosetta', 'choyeon', 'ssh', 'panel-'];
const services = computed(() => rows.value.filter((r) => r.unit.endsWith('.service')));
const activeCount = computed(() => services.value.filter((r) => r.active === 'active').length);
const failedCount = computed(() => services.value.filter((r) => r.active === 'failed').length);
const filtered = computed(() => {
  let list = services.value;
  if (!showAll.value) list = list.filter((r) => important.some((k) => r.unit.toLowerCase().includes(k)) || r.active === 'active');
  if (statusSel.value.length) list = list.filter((r) => statusSel.value.includes(r.active));
  if (filter.value) list = list.filter((r) => (r.unit + r.desc).toLowerCase().includes(filter.value.toLowerCase()));
  return list;
});

const tabs = computed(() => [
  { key: 'services', label: '系统服务' },
  // 有待装安全更新时页签上点一颗红点：这一页平时没人看，而它恰恰是最需要被看见的。
  { key: 'patches', label: '系统补丁', badge: (secCount.value ?? 0) > 0 },
  // 单元日志走 /api/system/services/{unit}/logs，服务端要求 admin（journal 是全机内容）。
  // 不给只读账号留这个页签，否则他点进来的是一片 403。
  ...(isAdmin ? [{ key: 'logs', label: '单元日志', badge: !!logUnit.value }] : []),
]);

// 选项从实际返回的 active 值里取，systemd 状态枚举（如 reloading）出现也不至于漏
const STATE_LABELS: Record<string, string> = {
  active: '运行中', inactive: '已停止', failed: '异常', activating: '启动中', deactivating: '停止中',
  reloading: '重新加载中', maintenance: '维护中',
};
const statusLabel = (s: string) => STATE_LABELS[s] || s;
// masked（被 systemctl mask 掉）在 list-units 里 ACTIVE 也是 inactive，照原样翻译就成了
// 「已停止」——那是"能启动但没启动"，而 masked 是"根本不许启动"，得区分开。
const isMasked = (r: any) => r.load === 'masked' || enabledOf(r.unit) === 'masked';
const statusText = (r: any) => (isMasked(r) ? '已屏蔽' : statusLabel(r.active));
const statusOptions = computed(() =>
  [...new Set(services.value.map((r) => r.active))].sort().map((v) => ({ label: statusLabel(v), value: v })),
);

async function act(unit: string, verb: string, ok?: string) {
  busy.value = { unit, verb };
  try {
    const res: any = await api.serviceAction(unit, verb);
    if (verb === 'enable' || verb === 'disable') enabledPatch.value = { ...enabledPatch.value, [unit]: String(res?.enabled ?? '') };
    // 走确认弹窗的路径由弹窗报成功，这里只给行内直调（启动/自启）补反馈，
    // 否则用户点完什么也没发生，只能猜 systemctl 到底下发了没有。
    if (ok) msg.success(ok);
    later(reload, 400);
  } finally {
    busy.value = null;
  }
}

const CONFIRM: Record<'stop' | 'restart', { title: string; label: string }> = {
  stop: { title: '停止服务', label: '停止' },
  restart: { title: '重启服务', label: '重启' },
};
function askConfirm(unit: string, verb: 'stop' | 'restart') {
  const v = CONFIRM[verb];
  opRef.value?.acceptParams({
    title: v.title,
    names: [unit],
    msg: `此操作将${v.label}该 systemd 服务，是否继续？`,
    api: () => act(unit, verb),
    successMsg: `${unit} 已${v.label}`,
  });
}

function openLog(unit: string) {
  if (!isAdmin) return;
  logUnit.value = unit;
  tab.value = 'logs';
}

function rowButtons(r: any): RowAction[] {
  const running = r.active === 'active';
  const dis = unitBusy(r.unit);
  const masked = isMasked(r);
  const list: RowAction[] = isAdmin ? [{ label: '查看', run: () => openLog(r.unit) }] : [];
  // 服务端对全部 MUTATION 拦 403，只读账号既没有「查看」（日志要 admin）也没有按钮，
  // 不把必然失败的入口摆出来
  if (!isAdmin) return list;
  // 屏蔽中的 unit 连 start 都会被 systemd 拒绝，按钮不摆（比摆了报错更诚实）
  if (!masked) {
    list.push(running
      ? { label: '停止', danger: true, disabled: dis, run: () => askConfirm(r.unit, 'stop') }
      : { label: '启动', disabled: dis, run: () => act(r.unit, 'start', `已下发启动：${r.unit}`).catch((e: any) => msg.error(e.message)) });
    if (running) list.push({ label: '重启', disabled: dis, run: () => askConfirm(r.unit, 'restart') });
  }
  const uf = enabledOf(r.unit);
  if (uf === 'enabled') {
    list.push({ label: '取消自启', disabled: dis, run: () => act(r.unit, 'disable', `已取消开机自启：${r.unit}`).catch((e: any) => msg.error(e.message)) });
  } else if (uf === 'disabled' || uf === '') {
    // 取不到状态时保留「自启」入口（enable 是幂等的），masked/static/generated 则不给按钮
    list.push({ label: '自启', disabled: dis, run: () => act(r.unit, 'enable', `已设置开机自启：${r.unit}`).catch((e: any) => msg.error(e.message)) });
  }
  return list;
}

const columns: any[] = [
  {
    title: '服务', key: 'unit', width: 400,
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: icons.ServerOutline, size: 17, color: r.active === 'active' ? 'var(--cp-ok)' : r.active === 'failed' ? 'var(--cp-err)' : 'var(--cp-text-mute)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          // 服务名的点击行为就是"看它的实时日志"，只读账号拿不到日志，
          // 所以这里渲染成普通文本，不给一个点了只会 403 的链接样式
          isAdmin
            ? h(NButton, { text: true, type: 'primary', class: 'cell-name', title: r.unit, onClick: () => openLog(r.unit) }, () => r.unit)
            : h('span', { class: 'cell-name', title: r.unit }, r.unit),
          h('div', { class: 'cell-sub', title: r.desc || '—' }, r.desc || '—'),
        ]),
      ]),
  },
  {
    // 状态不只用颜色：CpStatusTag 的文字（运行中/异常/已停止）始终在场
    title: '状态', key: 'active', width: 110,
    render: (r: any) => h(CpStatusTag, { status: isMasked(r) ? 'blocked' : r.active, text: statusText(r) }),
  },
  {
    // 1Panel 的服务列表里「开机自启」是独立一列：它和"现在在不在跑"是两件事，
    // 合成一列会让人以为已停止 = 未自启。
    title: '自启', key: 'enabled', width: 96,
    render: (r: any) => {
      const uf = enabledOf(r.unit);
      const text = uf || (r.unit.endsWith('.service') ? '未知' : '');
      const cls = uf === 'enabled' ? 'ok' : uf === 'disabled' ? 'idle' : uf === 'masked' ? 'err' : 'idle';
      return h('span', {
        class: `st ${cls}`,
        title: uf ? `systemctl 状态：${uf}` : 'list-unit-files 未返回这个 unit 的自启状态',
      }, [h('span', { class: `dot ${cls}` }), text]);
    },
  },
  {
    title: '操作', key: 'ops', width: 209, align: 'center', fixed: 'right' as const,
    render: (r: any) => h(CpRowActions, { buttons: rowButtons(r) }),
  },
];

// unit 数动辄几百行，一次全渲染会让首屏卡顿；分页只渲染当前页。
// 不传 page/pageSize 之外的受控字段：分页状态交给 naive 自己维护。
const paging = { pageSize: 50, showSizePicker: false, showQuickJumper: false };

// 补丁表：包名带 suite（stable-security / baseos 之类），版本差是这一页唯一要看的东西，
// 所以两列版本都给足宽度，窄屏靠 scroll-x 横滚而不是把版本号截成 "2.41-1…".
const patchColumns: any[] = [
  {
    title: '包名', key: 'name', minWidth: 240,
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, {
            component: r.security ? icons.ShieldCheckmarkOutline : icons.CubeOutline,
            size: 17,
            color: r.security ? 'var(--cp-warn)' : 'var(--cp-text-mute)',
          }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name', title: r.name }, r.name),
          h('div', { class: 'cell-sub', title: r.suite || '—' }, r.suite || '—'),
        ]),
      ]),
  },
  { title: '当前版本', key: 'current', width: 200, render: (r: any) => h('span', { class: 'mono' }, r.current || '—') },
  { title: '更新到', key: 'candidate', width: 200, render: (r: any) => h('span', { class: 'mono' }, r.candidate || '?') },
  {
    title: '类别', key: 'security', width: 92,
    render: (r: any) => h(NTag, { size: 'small', type: r.security ? 'warning' : 'default', bordered: false }, () => (r.security ? '安全' : '功能')),
  },
];

const patchPaging = { pageSize: 20, showSizePicker: false, showQuickJumper: false };
// 通告号只在 dnf 那一支有：列出来能让管理员去查 CVE，但不为它单开一张表。
const advisoryLine = computed(() => (patches.value?.advisories || []).slice(0, 8).map((a: any) => a.id).join('、'));
</script>

<template>
  <div>
    <PageTabBar :tabs="tabs" :value="tab" @update="(k: string) => (tab = k)">
      <template #extra>
        <!-- 这三颗都是"系统服务"页签的工具：留在补丁页上会变成两个「刷新」并排，
             而上面那颗刷的是服务表，用户按字面理解只会以为点了没反应。 -->
        <template v-if="tab === 'services'">
          <NText v-if="pendingRefresh" depth="3" style="font-size: var(--fs-xs)">状态回读中…</NText>
          <NButton size="small" :quaternary="!showAll" :type="showAll ? 'primary' : 'default'" @click="showAll = !showAll">
            {{ showAll ? '只看关键服务' : '查看全部单元' }}
          </NButton>
          <NButton size="small" tertiary :loading="loading || refreshing || pendingRefresh" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
        </template>
      </template>
    </PageTabBar>

    <CpToolbarCard v-if="tab === 'services'" title="系统服务" style="margin-top: 10px">
      <template #right>
        <div class="svc-toolbar">
          <NSelect
            v-model:value="statusSel" :options="statusOptions" multiple :max-tag-count="4"
            size="small" placeholder="状态筛选" class="svc-filter" clearable
          />
          <NInput :value="filter" @update:value="(v: string) => (filter = v)" placeholder="搜索 unit / 描述…" size="small" clearable class="svc-search">
            <template #prefix><NIcon :component="icons.SearchOutline" /></template>
          </NInput>
        </div>
      </template>

      <NAlert type="info" :bordered="false" style="margin-bottom: var(--space-3)">
        systemd 服务 {{ services.length }} 个 · {{ activeCount }} 个运行中<template v-if="failedCount"> · {{ failedCount }} 个异常</template><template v-if="isAdmin">，点击服务名可看实时日志</template>
      </NAlert>

      <NDataTable
        ref="tableAnchor"
        :columns="columns" :data="filtered" size="small" :bordered="false" :loading="loading"
        :scroll-x="810" :max-height="tableH" :pagination="paging" :row-key="(r: any) => r.unit"
        class="cp-tbl tbl-services"
      >
        <!-- 拉取失败不能显示成「没有匹配的服务」：那是"数据是空的"，而实际是"没拿到数据" -->
        <template #empty><EmptyBox :text="error ? '服务列表获取失败，点上方「刷新」重试' : '没有匹配的服务'" /></template>
      </NDataTable>
    </CpToolbarCard>

    <CpToolbarCard v-else-if="tab === 'patches'" title="系统补丁" style="margin-top: 10px">
      <template #right>
        <div class="pt-toolbar">
          <NText v-if="patches" depth="3" class="pt-time">
            检查于 {{ fmtDateTime(patches.checked_at) }}<template v-if="patches.cached">（缓存）</template>
          </NText>
          <NButton size="small" tertiary :loading="patchBusy" :icon="icon('RefreshOutline', 14)" @click="refreshPatches">刷新</NButton>
          <NButton size="small" :loading="probing" :icon="icon('SyncOutline', 14)" @click="repost">重新探测</NButton>
        </div>
      </template>

      <NAlert v-if="!patches && patchError" type="error" :bordered="false" style="margin-bottom: var(--space-3)">
        补丁探测失败：{{ (patchError as Error).message || '未知错误' }}。点上方「重新探测」重试。
      </NAlert>
      <NAlert v-else-if="patchAlert" :type="patchAlert.type" :bordered="false" style="margin-bottom: var(--space-3)">
        {{ patchAlert.text }}
      </NAlert>

      <div v-if="patches" class="pt-chips">
        <span class="pt-chip"><i>发行版</i>{{ patches.distro || patches.family || '—' }}</span>
        <span class="pt-chip"><i>待装</i>{{ totalPending }} 个</span>
        <span class="pt-chip">
          <i>安全更新</i>
          <template v-if="secCount === null">判不出</template>
          <template v-else>{{ secCount }} 个</template>
        </span>
        <span class="pt-chip">
          <i>自动安全更新</i>
          <template v-if="autoState === 'enabled'">已开启</template>
          <template v-else-if="autoState === 'disabled'">未开启</template>
          <template v-else>该发行版不适用</template>
        </span>
        <span v-if="patches.reboot_required" class="pt-chip warn"><i>重启</i>上次更新需重启生效</span>
      </div>

      <div v-if="patches && (patches.notes || []).length" class="pt-notes">
        <div v-for="(n, i) in patches.notes" :key="i" class="pt-note">{{ n }}</div>
      </div>
      <NText v-if="advisoryLine" depth="3" class="pt-advisories">安全通告：{{ advisoryLine }}</NText>

      <div v-if="patches && (totalPending || secCount)" class="pt-cmds">
        <div v-if="patches.commands?.apply_security" class="pt-cmd">
          <NText depth="3" class="pt-cmd-label">只装安全更新</NText>
          <code class="mono pt-cmd-text">{{ patches.commands.apply_security }}</code>
          <NButton size="tiny" quaternary :icon="icon('CopyOutline', 13)" @click="copy(patches.commands.apply_security, '已复制，请到 SSH 会话里执行')">复制</NButton>
        </div>
        <div v-if="patches.commands?.apply_all" class="pt-cmd">
          <NText depth="3" class="pt-cmd-label">全部升级</NText>
          <code class="mono pt-cmd-text">{{ patches.commands.apply_all }}</code>
          <NButton size="tiny" quaternary :icon="icon('CopyOutline', 13)" @click="copy(patches.commands.apply_all, '已复制，请到 SSH 会话里执行')">复制</NButton>
        </div>
        <NText depth="3" class="pt-cmd-hint">面板只读出清单，不会替你装包：装包会顺带重启别的服务、可能换内核，失败要人盯着。</NText>
      </div>

      <NDataTable
        :columns="patchColumns" :data="patches?.packages || []" size="small" :bordered="false"
        :loading="patchBusy && !patches" :scroll-x="620"
        :max-height="(patches?.packages || []).length ? tableH : undefined" :pagination="patchPaging"
        :row-key="(r: any) => r.name" class="cp-tbl tbl-patches"
      >
        <template #empty>
          <EmptyBox :text="patchError && !patches ? '补丁清单获取失败，点上方「重新探测」重试' : '按本地包索引，没有待装更新'" />
        </template>
      </NDataTable>
    </CpToolbarCard>

    <CpToolbarCard v-else :title="logUnit ? `${logUnit} · journalctl 实时日志` : '单元日志'" style="margin-top: 10px">
      <!-- LogStream 只在挂载时开流、不 watch urlPath：换 unit 必须重挂载，否则旧 SSE 还在推上一个服务的日志 -->
      <LogStream v-if="logUnit" :key="logUnit" :url-path="`/system/services/${encodeURIComponent(logUnit)}/logs`" class="svc-log" />
      <EmptyBox v-else text="在「系统服务」页点击服务名查看实时日志" />
    </CpToolbarCard>

    <ConfirmActionDialog ref="opRef" />
  </div>
</template>

<style scoped>
.svc-toolbar { display: flex; align-items: center; gap: 8px; min-width: 0; }
.svc-filter { width: 300px; }
.svc-search { width: 180px; }
/* 系统补丁：chip 摘要 + 可复制的升级命令。命令一定给横向滚动条，
   `apt update && apt upgrade -y` 在 375px 上换行会把两行命令粘成一坨。 */
.pt-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.pt-time { font-size: var(--fs-xs); }
.pt-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: var(--space-3); }
.pt-chip {
  display: inline-flex; align-items: baseline; gap: 6px; padding: 4px 10px;
  border-radius: var(--radius-md); background: var(--cp-bg-soft); font-size: var(--fs-sm); color: var(--cp-text);
}
.pt-chip i { font-style: normal; font-size: var(--fs-xs); color: var(--cp-text-mute); }
.pt-chip.warn { color: var(--cp-warn); }
.pt-notes { margin: -4px 0 var(--space-3); }
.pt-note { font-size: var(--fs-xs); color: var(--cp-text-dim); line-height: 1.7; }
.pt-advisories { display: block; margin: -4px 0 var(--space-3); font-size: var(--fs-xs); word-break: break-all; }
.pt-cmds { display: grid; gap: 6px; margin-bottom: var(--space-3); }
.pt-cmd { display: flex; align-items: center; gap: 8px; min-width: 0; }
.pt-cmd-label { flex: 0 0 76px; font-size: var(--fs-xs); }
.pt-cmd-text {
  flex: 1 1 auto; min-width: 0; overflow-x: auto; white-space: nowrap;
  padding: 4px 8px; border-radius: var(--radius-sm); background: var(--cp-bg-soft); font-size: var(--fs-sm);
}
.pt-cmd-hint { font-size: var(--fs-xs); }
@media (max-width: 767px) {
  .pt-cmd { flex-wrap: wrap; }
  .pt-cmd-label { flex: 0 0 100%; }
  .pt-cmd-text { flex: 1 1 auto; max-width: 100%; }
  .pt-toolbar { justify-content: flex-start; }
}
@media (max-width: 1024px) {
  .svc-toolbar { flex-wrap: wrap; justify-content: flex-start; gap: 12px; }
  .svc-filter, .svc-search { flex: 1 1 220px; max-width: 300px; width: auto; }
}
/* 1Panel 日志盒参数压过 LogStream 的行内高度（组件根上的类，:deep 到 .log-view） */
.svc-log :deep(.log-view) {
  height: calc(100vh - 320px) !important;
  min-height: 420px;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
}
</style>
