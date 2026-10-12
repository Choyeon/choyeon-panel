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
import { fmtBytes, fmtDateTime, fmtDuration } from '../composables/useFormat';

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

// 端口暴露面也是只读对账（ss + 防火墙规则，服务端有 5 分钟 TTL）。前端再缓存一层，
// 切走再回来秒开；「重新对账」带 refresh=1 强制回源，用于刚改完 ufw 规则想立刻确认。
const forceAudit = ref(false);
const {
  data: portData, error: portError, loading: portLoading, refreshing: portRefreshing, refresh: refreshPorts,
} = useCachedFetch<any>({
  key: () => 'ports',
  loader: () => api.ports(forceAudit.value),
  maxAge: 60000,
});
const auditing = ref(false);
watch(portError, (e) => { if (e) msg.error((e as Error).message || String(e)); });

async function reaudit() {
  forceAudit.value = true;
  auditing.value = true;
  try {
    await refreshPorts();
  } catch {
    /* 失败已经通过 portError 走上面的 toast */
  } finally {
    forceAudit.value = false;
    auditing.value = false;
  }
}

// 登录安全同样是只读对账（sshd -T 有效配置 + 24h 认证日志 + authorized_keys 权限，服务端 5 分钟 TTL）。
// 这一层没有任何修复动作：SSH 配置猜错就是把人锁在门外，而唯一能改回来的入口正是 SSH 本身。
// 页面只做一件事——把判词、判词的依据、以及"回去自己执行"的命令摆在一起。
const forceLogin = ref(false);
const probingLogin = ref(false);
const {
  data: loginData, error: loginError, loading: loginLoading, refreshing: loginRefreshing, refresh: refreshLogin,
} = useCachedFetch<any>({
  key: () => 'ssh-audit',
  loader: () => api.sshAudit(forceLogin.value),
  maxAge: 60000,
});
watch(loginError, (e) => { if (e) msg.error((e as Error).message || String(e)); });

async function recheckLogin() {
  forceLogin.value = true;
  probingLogin.value = true;
  try {
    await refreshLogin();
  } catch {
    /* 失败已经通过 loginError 走上面的 toast */
  } finally {
    forceLogin.value = false;
    probingLogin.value = false;
  }
}

const loginBusy = computed(() => loginLoading.value || loginRefreshing.value || probingLogin.value);
const loginCounts = computed(() => loginData.value?.counts ?? { risk: 0, warn: 0, note: 0 });
const loginFindings = computed<any[]>(() => loginData.value?.findings ?? []);
const loginSettings = computed<any[]>(() => loginData.value?.settings ?? []);
const flaggedSettings = computed(() => loginSettings.value.filter((r: any) => r.flag));
// 15 行有效配置里通常只有两三行需要处理，默认就只列那几行；要看证据再展开。
const onlyFlagged = ref(true);
const shownSettings = computed(() => (onlyFlagged.value ? flaggedSettings.value : loginSettings.value));
const keyFiles = computed<any[]>(() => loginData.value?.keys?.files ?? []);
const loginAttempts = computed<any>(() => loginData.value?.attempts ?? {});
const guardRunning = computed(() => (loginData.value?.guard ?? []).some((g: any) => g.active));
const guardLine = computed(() => {
  const g = loginData.value?.guard ?? [];
  const on = g.filter((u: any) => u.active).map((u: any) => u.name);
  if (on.length) return `${on.join('、')} 在跑`;
  if (g.some((u: any) => u.installed)) return '装了但没在跑';
  return '都没在跑';
});
const acceptedLine = computed(() => {
  const list = loginAttempts.value.accepted ?? [];
  return list.length ? list.map((a: any) => `${a.method} ${a.count}`).join('、') : '无成功记录';
});
const sudoLine = computed(() => {
  const g = loginData.value?.accounts?.sudo_groups ?? {};
  const pairs = Object.entries(g).filter(([, m]) => (m as any[]).length);
  return pairs.length ? pairs.map(([n, m]) => `${n}: ${(m as any[]).join('、')}`).join(' · ') : '空（没人能 sudo）';
});
const reachLine = computed(() => {
  const r = loginData.value?.sshd?.reachable;
  return r === true ? '外部可达' : r === false ? '外部不可达' : '可达性未探到';
});

const SEVERITY: Record<string, { text: string; type: any; icon: string }> = {
  risk: { text: '要紧', type: 'error', icon: 'ShieldOutline' },
  warn: { text: '值得处理', type: 'warning', icon: 'WarningOutline' },
  note: { text: '知道就好', type: 'default', icon: 'InformationCircleOutline' },
};
const sevOf = (k: string) => SEVERITY[k] || SEVERITY.note;

const loginAlert = computed(() => {
  const d = loginData.value;
  if (!d || loginError.value) return null;
  if (d.sshd.source === 'missing' || d.sshd.source === 'unreadable') {
    return { type: 'error' as const, text: `读不到 sshd 配置（${d.sshd.source === 'missing' ? '文件不存在' : '没有读权限'}），这一页的判定都不可信。` };
  }
  if (loginCounts.value.risk) {
    const first = loginFindings.value[0];
    return { type: 'error' as const, text: `${loginCounts.value.risk} 项要紧：${first ? first.title : ''}。面板不会替你改 SSH，下面的命令要你自己 SSH 进去执行。` };
  }
  if (loginCounts.value.warn) {
    return { type: 'warning' as const, text: `没有要紧项，另有 ${loginCounts.value.warn} 项值得处理。` };
  }
  const a = d.attempts || {};
  if (!a.ok) {
    return { type: 'info' as const, text: `有效配置核对通过（读自 ${d.sshd.source}），但认证日志读不到：${a.note || '原因未知'}。` };
  }
  return {
    type: 'success' as const,
    text: `登录方式核对通过：过去 24 小时 ${a.failed} 次失败认证、来自 ${a.distinct_ips} 个地址，成功登录 ${acceptedLine.value}。`,
  };
});

// 存储占用也是只读对账（走盘扫目录 + journalctl --disk-usage，服务端 5 分钟 TTL）。
// 这一页一个修复按钮都没有：磁盘上的清理动作每一条都是"删了就没了"，面板只把占用摆清楚、
// 把命令写清楚，按下去的那只手得是人的。
const forceScan = ref(false);
const scanning = ref(false);
const {
  data: storageData, error: storageError, loading: storageLoading, refreshing: storageRefreshing, refresh: refreshStorage,
} = useCachedFetch<any>({
  key: () => 'storage',
  loader: () => api.storage(forceScan.value),
  maxAge: 60000,
});
watch(storageError, (e) => { if (e) msg.error((e as Error).message || String(e)); });

async function rescan() {
  forceScan.value = true;
  scanning.value = true;
  try {
    await refreshStorage();
  } catch {
    /* 失败已经通过 storageError 走上面的 toast */
  } finally {
    forceScan.value = false;
    scanning.value = false;
  }
}

const storageBusy = computed(() => storageLoading.value || storageRefreshing.value || scanning.value);
const storageCounts = computed(() => storageData.value?.counts
  ?? { risk: 0, warn: 0, info: 0, roots: 0, bytes_total: 0, bytes_total_human: '0B' });
const storageRoots = computed<any[]>(() => storageData.value?.roots ?? []);
const allFindings = computed<any[]>(() => storageData.value?.findings ?? []);
const bigFiles = computed<any[]>(() => storageData.value?.big_files ?? []);
const diskInfo = computed<any>(() => storageData.value?.disk ?? {});
const tightDisk = computed(() => Number(diskInfo.value?.used_pct ?? 0) >= 90);
// 挂载点这一张表回答的是「别的盘呢」：chip 上的分区余量说的是数据目录所在的那块盘，
// 而写满的可能是一个和数据目录毫无关系的挂载点，或者它的 inode 先耗尽。
const mountData = computed<any>(() => storageData.value?.mounts ?? {});
const mountRows = computed<any[]>(() => mountData.value?.items ?? []);
const tightMounts = computed(() => mountRows.value.filter(
  (m: any) => Number(m.used_pct ?? 0) >= 90 || Number(m.inode_pct ?? 0) >= 90,
));
// 「知道就好」那几档（没扫完、点名几个大文件）默认收着：一进来先看见一堆无关痛痒的行，
// 真正的风险反而被埋在下面。
const onlySerious = ref(true);
const shownFindings = computed(() => (onlySerious.value
  ? allFindings.value.filter((f: any) => f.severity !== 'info')
  : allFindings.value));
const infoFindings = computed(() => allFindings.value.filter((f: any) => f.severity === 'info'));
const backupLine = computed(() => {
  const b = storageRoots.value.find((r: any) => r.backups?.count)?.backups;
  return b ? `${b.count} 份（最早 ${b.oldest}，最新 ${b.newest}）` : '';
});
// 条形按最大的那个根归一化：一眼看出谁在吃磁盘，而不是让人在心里除十进制。
const maxRootBytes = computed(() => Math.max(1, ...storageRoots.value.map((r: any) => Number(r.bytes) || 0)));
const shareOf = (r: any) => Math.max(2, Math.round(((Number(r.bytes) || 0) * 100) / maxRootBytes.value));
// 占比的分母只用最外层那一份：登记的根可以互相包含（数据目录就落在应用根里面），
// 拿全部根相加会让最外层那行显示成 60% 而实际上它装着另外两行。
const outerBytes = computed(() => storageRoots.value
  .filter((r: any) => r.exists && !r.nested_in)
  .reduce((s: number, r: any) => s + (Number(r.bytes) || 0), 0));
const shareLabel = (r: any) => (!r.exists
  // 读不到的那一行没有"占多少"可言：给它算个 0% 会让人以为磁盘上真有这么一块空地。
  ? '读不到'
  : r.nested_in
    ? '与外层目录重叠，不重复计入合计'
    : `占外层 ${Math.round(((Number(r.bytes) || 0) * 100) / (outerBytes.value || 1))}%`);

const storageAlert = computed(() => {
  const d = storageData.value;
  if (!d || storageError.value) return null;
  const c = storageCounts.value;
  if (!c.roots) {
    return { type: 'error' as const, text: '一个目录都没读到，这一页的数字都不可信。先确认数据目录、备份目录与 CP_FILE_ROOTS 登记的根还在不在原处。' };
  }
  if (c.risk) {
    const first = allFindings.value.find((f: any) => f.severity === 'risk');
    return { type: 'error' as const, text: `${c.risk} 项要紧：${first ? first.title : ''}。面板不会替你删东西，下面的命令要你自己 SSH 进去执行。` };
  }
  if (c.warn) return { type: 'warning' as const, text: `没有要紧项，另有 ${c.warn} 项值得处理。` };
  const disk = d.disk || {};
  return {
    type: 'success' as const,
    text: `盘点了 ${c.roots} 个目录、合计 ${c.bytes_total_human}，没有异常占用${disk.ok ? `，分区已用 ${disk.used_pct}%` : '（分区总量读不到）'}。`,
  };
});

const rootColumns: any[] = [
  {
    title: '目录', key: 'label', minWidth: 240,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name' }, r.label),
      h('div', { class: 'cell-sub mono ls-wrap' }, r.path),
    ]),
  },
  {
    title: '占用', key: 'bytes_human', width: 136,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name mono' }, r.exists ? r.bytes_human : '—'),
      h('div', { class: 'st-bar' }, [h('i', { style: `width:${r.exists ? shareOf(r) : 0}%` })]),
      h('div', { class: 'cell-sub' }, shareLabel(r)),
    ]),
  },
  {
    title: '一级子目录构成', key: 'subdirs', minWidth: 280,
    render: (r: any) => {
      const pieces: any[] = (r.subdirs || []).map((s: any) =>
        h('span', { class: 'st-piece' }, [h('code', { class: 'mono' }, s.name), h('b', s.bytes_human)]));
      // 「未列出」的个数是一级分支里没进榜的那些，不是全部层级的目录数。
      const rest = (r.branch_count || 0) - (r.subdirs || []).length;
      if (rest > 0) pieces.push(h('span', { class: 'st-piece rest' }, `另有 ${rest} 个`));
      if (!pieces.length) return h('span', { class: 'cell-sub' }, r.exists ? '没有成规模的子目录' : '—');
      return h('div', { class: 'st-pieces' }, pieces);
    },
  },
  {
    title: '这一行的可信度', key: 'marks', minWidth: 220,
    render: (r: any) => {
      const marks: any[] = [];
      if (!r.exists) marks.push(r.note || '目录不存在');
      if (r.partial) marks.push('没扫完，数字是下界');
      if (r.errors) marks.push(`${r.errors} 处读不到`);
      if (r.nested_in) marks.push(`与 ${r.nested_in} 是同一份数据`);
      if (!marks.length) return h('span', { class: 'cell-sub' }, '完整扫描');
      return h('div', { class: 'st-marks' }, marks.map((m) => h('span', { class: 'st-mark' }, m)));
    },
  },
];

const storageFindingColumns: any[] = [
  {
    title: '判词', key: 'title', minWidth: 300,
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
          h('span', { class: 'cell-name ls-wrap' }, r.title),
          h('div', { class: 'cell-sub ls-wrap' }, r.detail),
        ]),
      ]),
  },
  {
    title: '档位', key: 'severity', width: 96,
    render: (r: any) => h(NTag, { size: 'small', type: sevOf(r.severity).type, bordered: false }, () => sevOf(r.severity).text),
  },
  {
    // 存储这里的 fix 是一行能直接粘贴的命令（journalctl --vacuum-size=500M），所以给复制按钮；
    // 按钮旁边写明面板不会替你执行——删文件的命令被人一键跑掉，是这个页面上最贵的一种方便。
    title: '处置', key: 'fix', minWidth: 300,
    render: (r: any) => (r.fix
      ? h('div', { class: 'st-fix' }, [
        h('code', { class: 'mono st-fix-cmd' }, r.fix),
        h(NButton, {
          size: 'tiny',
          quaternary: true,
          onClick: () => copy(r.fix, '已复制：面板不会替你执行，请 SSH 进去自己跑'),
        }, { default: () => '复制', icon: () => h(NIcon, { component: icons.CopyOutline, size: 13 }) }),
      ])
      : h('span', { class: 'cell-sub' }, '这一条不用动手')),
  },
];

const bigFileColumns: any[] = [
  {
    title: '文件', key: 'path', minWidth: 340,
    render: (r: any) => h('span', { class: 'mono ls-wrap' }, r.path),
  },
  {
    title: '占用', key: 'bytes_human', width: 110,
    render: (r: any) => h('span', { class: 'mono' }, r.bytes_human),
  },
];
/**
 * 行数装不满一页就不挂分页条：空表底下孤零零一个「1」既不是分页也不是导航，
 * 只是噪声（判词表在「没有异常」时也会挂出这个 1）。返回的必须是传进来的
 * 那个对象本身或 false，不能在模板里现造新对象——那样 naive 每次重渲染都会
 * 把用户已经翻到的页码重置回第一页。
 */
function pager(count: number, cfg: { pageSize: number }) {
  return count > cfg.pageSize ? cfg : false;
}
const storagePaging = { pageSize: 10, showSizePicker: false, showQuickJumper: false };

const hotPct = (v: any) => typeof v === 'number' && v >= 90;
const barStyle = (v: any) => `width:${Math.min(100, Math.max(0, Number(v) || 0))}%`;
const mountColumns: any[] = [
  {
    title: '挂载点', key: 'path', minWidth: 196,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name mono ls-wrap' }, r.path),
      h('div', { class: 'cell-sub mono ls-wrap' }, `${r.fstype} · ${r.device || '?'}`),
    ]),
  },
  {
    // 百分比与上面「分区已用」那颗 chip 同一个算法（写不进去的都算已用，含 ext4 预留块），
    // 所以根分区这一行的数字和 chip 上的必须一致；df 的 Use% 会低几个点，别拿来对。
    title: '容量', key: 'used_pct', width: 150,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name mono' }, typeof r.used_pct === 'number' ? `已用 ${r.used_pct}%` : '—'),
      h('div', { class: 'st-bar' }, [h('i', { class: hotPct(r.used_pct) ? 'hot' : '', style: barStyle(r.used_pct) })]),
      h('div', { class: 'cell-sub' }, r.unknown ? '挂着却读不到' : `剩 ${r.free_human} / 共 ${r.total_human}`),
    ]),
  },
  {
    // inode 单独一列，因为它是这块面板过去唯一的盲区：容量还剩几十 GB 而 inode 用尽时，
    // 任何新建文件都报错，只看余量的地方一律说"盘很空"。
    title: 'inode', key: 'inode_pct', width: 132,
    render: (r: any) => (typeof r.inode_pct !== 'number'
      ? h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name mono' }, '—'),
        h('div', { class: 'cell-sub ls-wrap' }, r.unknown ? '读不到' : '这类文件系统没有 inode 表'),
      ])
      : h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name mono' }, `${r.inode_pct}%`),
        h('div', { class: 'st-bar' }, [h('i', { class: hotPct(r.inode_pct) ? 'hot' : '', style: barStyle(r.inode_pct) })]),
        ...(hotPct(r.inode_pct)
          ? [h('div', { class: 'cell-sub ls-wrap' }, '容量看着还够，新文件已经建不出来')]
          : []),
      ])),
  },
  {
    title: '这块盘', key: 'marks', minWidth: 176,
    render: (r: any) => {
      const marks: { t: string; bad: boolean }[] = [];
      if (r.unknown) marks.push({ t: '挂着却读不到', bad: true });
      // snapshot 是后端给的：squashfs 这类只读镜像天生带 ro，那是性质不是故障，不能报成红。
      if (r.ro) marks.push({ t: r.snapshot ? '只读镜像，不是故障' : '只读挂载', bad: !r.snapshot });
      if (r.fstype === 'tmpfs') marks.push({ t: 'RAM 盘，重启即清', bad: false });
      // 同一块盘挂两处时的第二行（面板自己的命名空间里就有个 /var/tmp）：不标出来会像两块盘。
      if (r.same_dev_as) marks.push({ t: `与 ${r.same_dev_as} 是同一块盘`, bad: false });
      if (r.panel) marks.push({ t: '面板数据在这', bad: false });
      if (!marks.length) return h('span', { class: 'cell-sub' }, '读写正常');
      return h('div', { class: 'st-marks' }, marks.map((m) =>
        h('span', { class: m.bad ? 'st-mark' : 'st-mark mute' }, m.t)));
    },
  },
];
const findingColumns: any[] = [
  {
    title: '判词', key: 'title', minWidth: 320,
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
          h('span', { class: 'cell-name ls-wrap' }, r.title),
          h('div', { class: 'cell-sub ls-wrap' }, r.detail),
        ]),
      ]),
  },
  {
    title: '档位', key: 'severity', width: 96,
    render: (r: any) => h(NTag, { size: 'small', type: sevOf(r.severity).type, bordered: false }, () => sevOf(r.severity).text),
  },
  {
    // 建议是"该怎么改"的中文，不是一行能粘贴的命令（"先确认自己的钥匙能登进来，再把
    // PermitRootLogin 改成 prohibit-password"粘进 shell 只会得到一条报错）。所以这里给文本、
    // 不给复制按钮：按钮会让人以为粘贴过去就算修完了。
    title: '怎么修', key: 'fix', minWidth: 300,
    render: (r: any) => h('div', { class: 'cell-txt' }, [h('div', { class: 'px-reason' }, r.fix || '—')]),
  },
];

const settingColumns: any[] = [
  {
    title: '配置项', key: 'label', width: 200,
    render: (r: any) =>
      h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name' }, r.label),
        h('div', { class: 'cell-sub mono' }, r.key),
      ]),
  },
  {
    title: '此刻生效值', key: 'value', width: 180,
    render: (r: any) => h('span', { class: 'mono ls-value', title: r.value }, r.value || '—'),
  },
  {
    title: '判定', key: 'flag', width: 96,
    render: (r: any) =>
      (r.flag
        ? h(NTag, { size: 'small', type: sevOf(r.flag).type, bordered: false }, () => sevOf(r.flag).text)
        : h('span', { class: 'st idle' }, '合规')),
  },
  { title: '建议', key: 'fix', minWidth: 300, render: (r: any) => h('span', { class: 'px-fix' }, r.fix || '—') },
];

const keyColumns: any[] = [
  {
    title: '文件', key: 'path', minWidth: 240,
    render: (r: any) =>
      h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name mono', title: r.path }, r.path),
        h('div', { class: 'cell-sub ls-wrap' }, `${r.user} · ${r.mode} · 属主 ${r.owner} · 改于 ${fmtDateTime(r.modified_at)}`),
      ]),
  },
  {
    // 只有指纹：公钥正文不进面板，页面上能复制走的就只剩这串哈希。
    title: '公钥指纹', key: 'keys', minWidth: 300,
    render: (r: any) =>
      h('div', { class: 'cell-txt' },
        r.keys.length
          ? r.keys.map((k: any) =>
              h('div', { class: 'ls-key' }, [
                h('span', { class: 'mono ls-fp', title: k.fp }, k.fp),
                h('span', { class: 'ls-key-name' }, k.comment || '无备注'),
                k.weak ? h(NTag, { size: 'tiny', type: 'warning', bordered: false }, () => '算法偏弱') : null,
                k.unparsed ? h(NTag, { size: 'tiny', bordered: false }, () => '无法解析') : null,
              ]))
          : h('span', { class: 'cell-sub' }, r.symlink ? '是指向别处的符号链接，面板没有跟进去读' : '没有公钥')),
  },
  {
    title: '权限异常', key: 'flags', width: 220,
    render: (r: any) =>
      (r.flags.length
        ? h('div', { class: 'cell-txt' }, r.flags.map((f: string) => h('div', { class: 'ls-flag' }, f)))
        : h('span', { class: 'st idle' }, '权限正常')),
  },
];

// 内存压力也是只读对账（vmstat 双快照测速率 + PSI + cgroup v2 单元榜 + 按 RSS 的进程榜，服务端 90 秒 TTL）。
// 概览那张内存图回答的是「此刻占了多少」，这一页回答管理员紧接着要问的三句话：这台机器有没有因为缺内存而变慢
// （直接回收、换页、PSI 停顿在总量图上完全隐身）、内存被谁拿着（单元榜把进程认领回面板管理的应用）、内核有没有
// 已经杀过进程（vmstat 的 oom_kill 是自启动以来的一个整数，非 0 才去 kernel 日志点受害者）。
// 这一页一个修复动作都没有：给某个服务加 MemoryMax、加到多少，是只有管理员知道业务峰值的容量决策，面板替它写进
// unit 不是自动化，是赌博。处置一律是能复制走的只读命令。
const forceSample = ref(false);
const sampling = ref(false);
const {
  data: memData, error: memError, loading: memLoading, refreshing: memRefreshing, refresh: refreshMemory,
} = useCachedFetch<any>({
  key: () => 'memory',
  loader: () => api.memory(forceSample.value),
  maxAge: 60000,
});
watch(memError, (e) => { if (e) msg.error((e as Error).message || String(e)); });

async function resample() {
  forceSample.value = true;
  sampling.value = true;
  try {
    await refreshMemory();
  } catch {
    /* 失败已经通过 memError 走上面的 toast */
  } finally {
    forceSample.value = false;
    sampling.value = false;
  }
}

const memBusy = computed(() => memLoading.value || memRefreshing.value || sampling.value);
const memInfo = computed<any>(() => memData.value?.mem ?? {});
const pressInfo = computed<any>(() => memData.value?.pressure ?? {});
const rateInfo = computed<any>(() => memData.value?.rates ?? {});
const tunInfo = computed<any>(() => memData.value?.tunables ?? {});
const unitBlock = computed<any>(() => memData.value?.units ?? {});
const procBlock = computed<any>(() => memData.value?.procs ?? {});
const oomBlock = computed<any>(() => memData.value?.oom ?? {});
const unitRows = computed<any[]>(() => unitBlock.value.items ?? []);
const procRows = computed<any[]>(() => procBlock.value.items ?? []);
const oomRows = computed<any[]>(() => oomBlock.value.events ?? []);
const swapDevs = computed<any[]>(() => memInfo.value.devices ?? []);
const memFindings = computed<any[]>(() => memData.value?.findings ?? []);
const memCounts = computed(() => memData.value?.counts ?? { risk: 0, warn: 0, info: 0, procs: 0, units: 0 });
const unitKills = computed<Record<string, number>>(() => oomBlock.value.unit_kills ?? {});
const killTotal = computed(() => Number(oomBlock.value.kernel_total) || 0);
// OOM 不设阈值：内核计数是一个整数，非 0 就是要紧，没有「杀得还不多」这一档。
const memKilled = computed(() => killTotal.value > 0 || Object.keys(unitKills.value).length > 0);
// 「知道就好」那档（顶到上限被拦过、承诺超过启发式上限）默认收着，和存储那页同一条理由：
// 一进来先看见一堆无关痛痒的行，真正的风险反而被埋在下面。
const memOnlySerious = ref(true);
const memShownFindings = computed(() => (memOnlySerious.value
  ? memFindings.value.filter((f: any) => f.severity !== 'info')
  : memFindings.value));
const memInfoFindings = computed(() => memFindings.value.filter((f: any) => f.severity === 'info'));

// chip 与条子的深浅一律跟着后端判词走，前端不再抄一遍 MEM_RISK_PCT/CAP_RISK_PCT：阈值只改在后端时，
// 颜色不会和判词各说一套。
const sevOfFinding = (id: string) => {
  const hit = memFindings.value.find(
    (f: any) => String(f.id) === id || String(f.id).startsWith(`${id}:`),
  );
  return hit ? String(hit.severity) : '';
};
const tone = (sev: string) => (sev === 'risk' ? 'err' : sev === 'warn' ? 'warn' : '');
const usedTone = computed(() => tone(sevOfFinding('mem-used')));
const swapTone = computed(() => tone(sevOfFinding('swap-full')));
const psiTone = computed(() => tone(sevOfFinding('psi-active')));
const rateTone = computed(() => tone(sevOfFinding('direct-reclaim')) || tone(sevOfFinding('swapping'))
  || tone(sevOfFinding('commit')));
const unitTone = (unit: string) => sevOfFinding(`cap:${unit}`) || sevOfFinding(`pids:${unit}`);

// 测不到就是「没采到」，不是 0：0 会被读成「没有在换页」，而真实情况是这个窗口没取到数（计数器倒退、
// 读不到 /proc/vmstat、或者 refresh 之外的路径压根没开窗）。
const rateNum = (v: unknown, suffix = '') => (typeof v === 'number' ? `${v}${suffix}` : '没采到');
const rateBytes = (v: unknown) => (typeof v === 'number' ? `${fmtBytes(v)}/秒` : '没采到');
const avgOf = (k: string) => {
  const v = (pressInfo.value.some || {})[k];
  return typeof v === 'number' ? v.toFixed(1) : '—';
};
const psiStopped = computed(() => {
  const t = Number((pressInfo.value.some || {}).total) || 0;
  return t > 0 ? fmtDuration(t / 1e6) : '';
});
const hostTotal = computed(() => Number(memInfo.value.total) || 0);
const pctOfTotal = (n: unknown) => (typeof n === 'number' && hostTotal.value > 0
  ? Math.round((n * 100) / hostTotal.value)
  : null);

const UNIT_KIND: Record<string, string> = {
  service: '服务单元',
  scope: '一次性会话（登录会话、手工起的进程都算）',
  slice: '分组（占用已含组内所有子单元）',
  other: '认不出后缀',
  none: '读不到 cgroup',
  root: '根 cgroup',
};

const memAlertText = computed(() => {
  const d = memData.value;
  if (!d) return '';
  if (!d.mem?.ok) return `读不到 /proc/meminfo（${d.mem?.reason || '原因未知'}），这一页的数字都不可信。`;
  const c = d.counts || {};
  if (c.risk) {
    const first = (d.findings || []).find((f: any) => f.severity === 'risk');
    return `${c.risk} 项要紧：${first ? first.title : ''}。面板不会替你 kill 进程，也不会替你改 unit 的上限，`
      + '下面每条处置都是只读命令，要你自己 SSH 进去执行。';
  }
  if (c.warn) return `没有要紧项，另有 ${c.warn} 项值得处理。`;
  const p = d.pressure || {};
  const psi = p.ok
    ? `PSI some 近 10 秒 ${avgOf('avg10')}%`
    : `压力指数读不到（${p.reason || '原因未知'}）`;
  const r = d.rates || {};
  const rate = r.ok
    ? `${r.secs} 秒窗口里直接回收 ${rateNum(r.direct_reclaim)} 页/秒、换出 ${rateBytes(r.swap_out)}`
    : '速率窗口没采到，这几项不作数';
  return `已用 ${d.mem.used_pct}%（还能腾出 ${fmtBytes(d.mem.available)}），${psi}，${rate}；`
    + '内核没有 OOM 杀过进程，也没有单元被自己的上限杀掉。';
});
const memAlert = computed(() => {
  const d = memData.value;
  if (!d) return null;
  const c = memCounts.value;
  const type = !d.mem?.ok || c.risk ? ('error' as const) : c.warn ? ('warning' as const) : ('success' as const);
  return { type, text: memAlertText.value };
});

const memPaging = { pageSize: 10, showSizePicker: false, showQuickJumper: false };

const unitColumns: any[] = [
  {
    title: '单元', key: 'unit', minWidth: 250,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('div', { class: 'cell-name mono ls-wrap mm-break' }, [
        r.unit,
        r.self ? h('span', { class: 'mm-tag self' }, '面板自己') : null,
        r.app ? h('span', { class: 'mm-tag app' }, r.app) : null,
      ]),
      h('div', { class: 'cell-sub' }, UNIT_KIND[r.kind] || '认不出后缀'),
    ]),
  },
  {
    title: '此刻占用', key: 'current', width: 150,
    render: (r: any) => (r.readable
      ? h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name mono' }, fmtBytes(r.current)),
        h('div', { class: 'st-bar' }, [h('i', { style: barStyle(r.pct_of_host) })]),
        h('div', { class: 'cell-sub' },
          typeof r.pct_of_host === 'number' ? `整机 ${r.pct_of_host}%` : '整机总量读不到'),
      ])
      // 读不到不折成 0：0 会被读成「这个单元没占内存」，而真实情况是它刚退出、或者压根不上报。
      : h('span', { class: 'cell-sub' }, '读不到 memory.current')),
  },
  {
    title: '自己声明的上限', key: 'cap', width: 170,
    render: (r: any) => {
      if (!r.max) {
        return h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name mono' }, '没设上限'),
          h('div', { class: 'cell-sub' }, r.high ? `只有软上限 ${fmtBytes(r.high)}` : '顶到整机才处理'),
        ]);
      }
      const hot = unitTone(r.unit) === 'risk';
      return h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name mono' }, fmtBytes(r.max)),
        h('div', { class: 'st-bar' }, [h('i', { class: hot ? 'hot' : '', style: barStyle(r.pct_of_cap) })]),
        h('div', { class: 'cell-sub' },
          typeof r.pct_of_cap === 'number' ? `用到 ${r.pct_of_cap}%` : '整机总量读不到，算不出百分比'),
      ]);
    },
  },
  {
    title: '峰值', key: 'peak', width: 116,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name mono' }, r.peak ? fmtBytes(r.peak) : '—'),
      ...(r.peak ? [h('div', { class: 'cell-sub' }, '自单元启动以来')] : []),
    ]),
  },
  {
    title: '线程', key: 'pids', width: 124,
    render: (r: any) => {
      if (typeof r.pids !== 'number') return h('span', { class: 'cell-sub' }, '—');
      const hot = sevOfFinding(`pids:${r.unit}`) === 'risk';
      return h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name mono' }, `${r.pids} / ${r.pids_max ?? '不限'}`),
        ...(typeof r.pct_of_pids === 'number'
          ? [h('div', { class: 'st-bar' }, [h('i', { class: hot ? 'hot' : '', style: barStyle(r.pct_of_pids) })]),
            h('div', { class: 'cell-sub' }, `TasksMax 用了 ${r.pct_of_pids}%`)]
          : []),
      ]);
    },
  },
  {
    title: '这一行的可信度', key: 'marks', minWidth: 236,
    render: (r: any) => {
      const marks: { t: string; bad: boolean }[] = [];
      if (!r.readable) marks.push({ t: '没有内存统计：刚退出或该单元不上报', bad: true });
      if (r.kind === 'slice') marks.push({ t: '分组，别把它和自己的子单元再加一次', bad: false });
      if (r.kind === 'scope') marks.push({ t: '不是面板管的服务，内存是真占了', bad: false });
      if (r.self) marks.push({ t: '这一行是面板自己：被上限杀掉就是整个面板没了', bad: false });
      const killed = Number((r.events || {}).oom_kill) || Number(unitKills.value[r.unit]) || 0;
      if (killed) marks.push({ t: `被自己的上限杀过 ${killed} 次`, bad: true });
      const throttled = Number((r.events || {}).max) || 0;
      if (throttled) marks.push({ t: `顶到上限被拦过 ${throttled} 次`, bad: false });
      if (!marks.length) return h('span', { class: 'cell-sub' }, '常规服务单元');
      return h('div', { class: 'st-marks' }, marks.map((m) =>
        h('span', { class: m.bad ? 'st-mark' : 'st-mark mute' }, m.t)));
    },
  },
];

const procColumns: any[] = [
  {
    title: '进程', key: 'comm', minWidth: 210,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name ls-wrap' }, r.comm),
      h('div', { class: 'cell-sub mono' }, `pid ${r.pid}`),
    ]),
  },
  {
    title: 'RSS', key: 'rss', width: 116,
    render: (r: any) => h('span', { class: 'mono' }, fmtBytes(r.rss)),
  },
  {
    title: '占整机', key: 'pct', width: 130,
    render: (r: any) => {
      const pct = pctOfTotal(r.rss);
      return h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name mono' }, typeof pct === 'number' ? `${pct}%` : '—'),
        ...(typeof pct === 'number'
          ? [h('div', { class: 'st-bar' }, [h('i', { style: barStyle(pct) })])]
          : []),
      ]);
    },
  },
  {
    // 归不到单元是常态（容器外的守护进程、内核工作队列、非 systemd 拉起来的进程），
    // 如实写出来比硬凑一个名字有用：这一列的用途是判断「能不能顺着单元找回应用」。
    title: '归属', key: 'unit', minWidth: 260,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'mono ls-wrap mm-break' }, r.unit || (r.kind === 'root' ? '根 cgroup' : '归不到单元')),
      ...(r.app
        ? [h('div', { class: 'cell-sub' }, `面板管理的应用：${r.app}`)]
        : r.unit
          ? [h('div', { class: 'cell-sub' }, UNIT_KIND[r.kind] || '认不出后缀')]
          : [h('div', { class: 'cell-sub' }, '按 cgroup 找不到对应单元')]),
    ]),
  },
];

const oomColumns: any[] = [
  {
    title: '时间', key: 'at', width: 150,
    render: (r: any) => h('span', { class: 'mono ls-wrap' }, r.at || '—'),
  },
  {
    title: '受害者', key: 'who', minWidth: 190,
    render: (r: any) => (r.comm
      ? h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name ls-wrap' }, r.comm),
        h('div', { class: 'cell-sub mono' }, `pid ${r.pid ?? '?'}`),
      ])
      : h('span', { class: 'cell-sub' }, '这行认不出进程名，只留原文')),
  },
  {
    title: 'kernel 日志原文', key: 'line', minWidth: 320,
    render: (r: any) => h('div', { class: 'cell-sub mono ls-wrap mm-break' }, r.line || '—'),
  },
];

const swapColumns: any[] = [
  {
    title: '设备', key: 'name', minWidth: 220,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name mono ls-wrap mm-break' }, r.name),
      h('div', { class: 'cell-sub' }, `${r.type || '?'} · 优先级 ${r.priority ?? '?'}`),
    ]),
  },
  {
    title: '总量', key: 'total', width: 110,
    render: (r: any) => h('span', { class: 'mono' }, fmtBytes(r.total)),
  },
  {
    title: '已用', key: 'used', width: 150,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name mono' }, `${fmtBytes(r.used)}（${r.used_pct ?? 0}%）`),
      h('div', { class: 'st-bar' }, [h('i', { class: swapTone.value === 'err' ? 'hot' : '', style: barStyle(r.used_pct) })]),
    ]),
  },
];

const memFindingColumns: any[] = [
  {
    title: '判词', key: 'title', minWidth: 300,
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
          h('span', { class: 'cell-name ls-wrap' }, r.title),
          h('div', { class: 'cell-sub ls-wrap' }, r.detail),
        ]),
      ]),
  },
  {
    title: '档位', key: 'severity', width: 96,
    render: (r: any) => h(NTag, { size: 'small', type: sevOf(r.severity).type, bordered: false }, () => sevOf(r.severity).text),
  },
  {
    // 和存储那页一样给复制按钮：这里的 fix 全是只读命令（systemctl status、cat memory.events），
    // 复制走不会出事。面板自己不去执行任何一条——加上限、重启服务都是容量决策，不是维修动作。
    title: '处置', key: 'fix', minWidth: 300,
    render: (r: any) => (r.fix
      ? h('div', { class: 'st-fix' }, [
        h('code', { class: 'mono st-fix-cmd' }, r.fix),
        h(NButton, {
          size: 'tiny',
          quaternary: true,
          onClick: () => copy(r.fix, '已复制：面板不会替你执行，请 SSH 进去自己跑'),
        }, { default: () => '复制', icon: () => h(NIcon, { component: icons.CopyOutline, size: 13 }) }),
      ])
      : h('span', { class: 'cell-sub' }, '这一条不用动手')),
  },
];


// 「改动未生效」也是只读对账，管的是别处都不显示的那一层：补丁装了、conf 改了、unit 编辑了，而进程
// 里跑的还是旧的那份。这台机器的实测样本是 liblzma5 u2 于 10-08 装上、postgres 的 26 个后端仍映射旧
// inode，而同期「系统补丁」页写着没有待装更新。判据在后端（inode 与 ctime），这一页只负责把它画出来。
// 颜色与文案一律反查后端判词的 id，前端不重抄一遍安全库名单和阈值：两处各写一份，改一处就会悄悄分叉。
// 这一页一个按钮都不按：restart / reload / daemon-reload / reboot 都会断连接或杀业务，
// 什么时候能断这批连接只有管理员知道，面板准备的每条处置都是给人复制走、自己 SSH 进去执行的。
const pendForce = ref(false);
const rescanning = ref(false);
const {
  data: pendData, error: pendError, loading: pendLoading, refreshing: pendRefreshing, refresh: refreshPending,
} = useCachedFetch<any>({
  key: () => 'pending',
  loader: () => api.pending(pendForce.value),
  maxAge: 60000,
});
watch(pendError, (e) => { if (e) msg.error((e as Error).message || String(e)); });

async function rescanPending() {
  pendForce.value = true;
  rescanning.value = true;
  try {
    await refreshPending();
  } catch {
    /* 失败已经通过 pendError 走上面的 toast */
  } finally {
    pendForce.value = false;
    rescanning.value = false;
  }
}

const pendBusy = computed(() => pendLoading.value || pendRefreshing.value || rescanning.value);
const kernelInfo = computed<any>(() => pendData.value?.kernel ?? {});
const staleInfo = computed<any>(() => pendData.value?.stale ?? {});
const reloadInfo = computed<any>(() => pendData.value?.reloads ?? {});
const aptInfo = computed<any>(() => pendData.value?.apt ?? {});
const staleRows = computed<any[]>(() => staleInfo.value.units ?? []);
const pendFindings = computed<any[]>(() => pendData.value?.findings ?? []);
const pendCounts = computed(() => pendData.value?.counts ?? { risk: 0, warn: 0, info: 0 });
const pendOnlySerious = ref(true);
const pendShownFindings = computed(() => (pendOnlySerious.value
  ? pendFindings.value.filter((f: any) => f.severity !== 'info')
  : pendFindings.value));
const pendInfoFindings = computed(() => pendFindings.value.filter((f: any) => f.severity === 'info'));

// 判词列的形状和内存页一模一样（判词 / 档位 / 处置），直接复用同一套列定义：
// 两处各写一份的话，「处置」那一列的复制文案迟早会分叉。
const pendFindingColumns = memFindingColumns;
const pendPaging = { pageSize: 10, showSizePicker: false, showQuickJumper: false };

const pendSevOf = (id: string) => String((pendFindings.value.find((f: any) => f.id === id) || {}).severity || '');
const pendFixOf = (id: string) => String((pendFindings.value.find((f: any) => f.id === id) || {}).fix || '');
const pendTone = (id: string) => {
  const s = pendSevOf(id);
  return s === 'risk' ? 'err' : s === 'warn' ? 'warn' : '';
};

const RELOAD_KIND: Record<string, string> = {
  nginx: 'nginx 配置',
  sshd: 'sshd 配置',
  systemd: 'systemd unit 文件',
};
const RELOAD_FINDING_ID: Record<string, string> = {
  nginx: 'nginx-reload',
  sshd: 'sshd-reload',
  systemd: 'daemon-reload',
};
const reloadRows = computed(() => ['nginx', 'sshd', 'systemd'].map((kind) => ({
  kind,
  finding: RELOAD_FINDING_ID[kind],
  ...(reloadInfo.value[kind] || { ok: false, reason: '后端没返回这一节' }),
})));

const staleUnitColumns: any[] = [
  {
    title: '单元', key: 'unit', minWidth: 230,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('div', { class: 'cell-name mono ls-wrap mm-break' }, [
        r.unit || '不归 systemd 管',
        r.self ? h('span', { class: 'mm-tag self' }, '面板自己') : null,
      ]),
      h('div', { class: 'cell-sub mono ls-wrap mm-break' }, r.cmd || '命令行读不到'),
    ]),
  },
  {
    title: '几个进程', key: 'procs', width: 108,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name' }, String(r.procs)),
      h('div', { class: 'cell-sub mono ls-wrap mm-break' },
        (r.pids || []).length
          ? `pid ${(r.pids || []).join('、')}${r.procs > (r.pids || []).length ? ' 等' : ''}`
          : 'pid 读不到'),
    ]),
  },
  {
    title: '已经跑了', key: 'age', width: 150,
    render: (r: any) => h('div', { class: 'cell-txt' }, [
      h('span', { class: 'cell-name' }, r.age_human || '启动时间读不到'),
      h('div', { class: 'cell-sub' }, r.started_iso ? fmtDateTime(r.started_iso) : '—'),
    ]),
  },
  {
    // 路径整行给出来，不省略号：这一列的用途就是让人核对「到底是哪一份文件被换掉了」，
    // 截断之后 node_modules 里那几个同名文件根本分不开。
    title: '踩着的是哪份文件', key: 'files', minWidth: 330,
    render: (r: any) => h('div', { class: 'cell-txt' },
      (r.files || []).map((f: any) => h('div', { class: 'pd-file' }, [
        h('div', { class: 'mono ls-wrap mm-break' }, f.path),
        h('div', { class: 'cell-sub ls-wrap' }, [
          f.pkg ? `${f.pkg}${f.pkg_version ? ` ${f.pkg_version}` : ''}` : '不是 dpkg 装的文件',
          f.security ? h('span', { class: 'mm-tag app' }, '安全相关') : null,
          h('span', null, ` 换成新版：${f.ctime_iso ? fmtDateTime(f.ctime_iso) : '时间读不到'}`),
          f.gone ? h('span', null, '；原路径已经不在了') : null,
        ]),
      ]))),
  },
];

const reloadColumns: any[] = [
  {
    title: '哪一层', key: 'kind', width: 150,
    render: (r: any) => h('span', { class: 'cell-name' }, RELOAD_KIND[r.kind] || r.kind),
  },
  {
    title: '对上了没有', key: 'verdict', minWidth: 230,
    render: (r: any) => {
      if (!r.ok) return h('span', { class: 'st idle' }, r.reason || '读不到');
      if (r.kind === 'systemd') {
        return r.need_reload?.length
          ? h('div', { class: 'cell-txt' }, [
            h('span', { class: 'st warn' }, `${r.need_reload.length} 个 unit 改过还没 daemon-reload`),
            h('div', { class: 'cell-sub mono ls-wrap mm-break' }, r.need_reload.join('、')),
          ])
          : h('span', { class: 'st ok' }, `查过 ${r.checked} 个单元，没有改过没重载的`);
      }
      if (r.pending) return h('span', { class: 'st warn' }, '盘上的配置比进程里的新');
      return h('span', { class: 'st ok' }, '进程里就是当前这份配置');
    },
  },
  {
    title: '拿什么比的', key: 'ref', minWidth: 250,
    render: (r: any) => {
      if (!r.ok) return h('span', { class: 'cell-sub' }, '—');
      if (r.kind === 'systemd') {
        return h('span', { class: 'cell-sub' }, `systemctl show -p NeedDaemonReload（${r.checked} 个单元）`);
      }
      const proc = r.kind === 'nginx'
        ? `最晚启动的 worker（${(r.worker_pids || []).join('、') || 'pid 读不到'}）：${r.ref_start_iso ? fmtDateTime(r.ref_start_iso) : '启动时间读不到'}`
        : `sshd pid ${r.pid ?? '读不到'}：${r.started_iso ? fmtDateTime(r.started_iso) : '启动时间读不到'}`;
      const conf = r.newest_file
        ? `${r.newest_file}：${r.newest_mtime_iso ? fmtDateTime(r.newest_mtime_iso) : '时间读不到'}（共 ${r.conf_files ?? r.files ?? 0} 个配置文件）`
        : '配置文件列表读不到';
      return h('div', { class: 'cell-txt' }, [
        h('div', { class: 'cell-sub ls-wrap mm-break' }, proc),
        h('div', { class: 'cell-sub ls-wrap mm-break' }, conf),
      ]);
    },
  },
  {
    // 处置只从后端那条判词里取，前端不另写一份命令：同一件事两处各写一遍，迟早一处改了另一处没改。
    title: '处置', key: 'fix', minWidth: 300,
    render: (r: any) => {
      const fix = pendFixOf(r.finding);
      if (!r.ok) return h('span', { class: 'cell-sub' }, '这一节读不到，先按判词里的命令查');
      return fix
        ? h('div', { class: 'st-fix' }, [
          h('code', { class: 'mono st-fix-cmd' }, fix),
          h(NButton, {
            size: 'tiny',
            quaternary: true,
            onClick: () => copy(fix, '已复制：面板不会替你执行，请 SSH 进去自己跑'),
          }, { default: () => '复制', icon: () => h(NIcon, { component: icons.CopyOutline, size: 13 }) }),
        ])
        : h('span', { class: 'cell-sub' }, '对上了，这一层不用动手');
    },
  },
];

const pendAlertText = computed(() => {
  const d = pendData.value;
  if (!d) return '';
  const k = d.kernel || {};
  const s = d.stale || {};
  if (!k.ok && s.ok === false) {
    return `内核与进程映射两节都读不到（${(d.unreadable || []).join('、') || '原因未知'}）——`
      + '这一页等于空白，别把它读成「没有问题」。';
  }
  const c = d.counts || {};
  if (c.risk) {
    const first = (d.findings || []).find((f: any) => f.severity === 'risk');
    return `${c.risk} 项改动没进正在跑的进程：${first ? first.title : ''}。`
      + '面板不替你 restart、不替你 reload、不替你 reboot——这些动作会断连接、会杀业务，'
      + '什么时候能做只有你知道。下面每条处置都是准备好给你自己执行的。';
  }
  if (c.warn) return `没有要紧项，另有 ${c.warn} 项值得处理（配置改过还没重载、非安全库的旧映射这类）。`;
  if (!k.ok) {
    return `内核一节读不到（${k.reason || '原因未知'}）；进程映射扫了 ${s.scanned ?? 0} 个，`
      + '没有发现被替换后还在继续跑的库。';
  }
  return `运行中的 ${k.running} 就是装着的那份内核，${s.scanned ?? 0} 个进程里没有谁踩着已被替换的映射，`
    + 'nginx / sshd / unit 也没有改过没重载的。';
});
const pendAlert = computed(() => {
  const d = pendData.value;
  if (!d) return null;
  const dead = !d.kernel?.ok && d.stale?.ok === false;
  const c = pendCounts.value;
  const type = dead || c.risk ? ('error' as const) : c.warn ? ('warning' as const) : ('success' as const);
  return { type, text: pendAlertText.value };
});

const allPorts = computed<any[]>(() => portData.value?.ports ?? []);
const portBusy = computed(() => portLoading.value || portRefreshing.value || auditing.value);
// 默认只列"能被外面碰到"的那几行：几百个仅本机监听的端口（rpc、metrics、数据库）
// 会把真正该看的一行挤到第二页。
const showAllPorts = ref(false);
const riskyPorts = computed(() => allPorts.value.filter((p) => p.verdict !== 'internal'));
const shownPorts = computed(() => (showAllPorts.value ? allPorts.value : riskyPorts.value));
const countOf = (v: string) => Number(portData.value?.counts?.[v]) || 0;
const exposedCount = computed(() => countOf('exposed'));
const blockedCount = computed(() => countOf('blocked'));
const silentApps = computed(() => (portData.value?.apps ?? []).filter((a: any) => a.port && !a.listening));
const silentLine = computed(() => silentApps.value.map((a: any) => `${a.name}:${a.port}`).join('、'));
// systemctl 的 unit-file 状态是英文长串，直接上表会把 96px 的胶囊撑到截断，
// 也和左边中文的「状态」列对不上。这里给短标签，原始值留在 title 里供排障对照。
// enabled-runtime 单列一档：重启后就没了，这是最容易让人误判的一档。
const ENABLED_LABELS: Record<string, string> = {
  enabled: '已启用',
  'enabled-runtime': '临时启用',
  disabled: '已关闭',
  'disabled-runtime': '临时关闭',
  'disabled-static': '已关闭',
  masked: '已屏蔽',
  static: '静态单元',
  indirect: '按需触发',
  generated: '生成式',
  transient: '运行时一次性',
  linked: '已链接',
  alias: '别名',
  bad: '状态异常',
};
const SCOPE_LABELS: Record<string, string> = {
  wildcard: '绑全网', 'public-ip': '绑公网地址', loopback: '仅本机回环', 'link-local': '链路本地',
};
const VERDICTS: Record<string, { text: string; type: any; note: string }> = {
  exposed: { text: '对外可达', type: 'error', note: '外部能连上，且不在预期清单里' },
  blocked: { text: '防火墙挡住', type: 'warning', note: '绑在全网，但入站规则没放行' },
  expected: { text: '预期内', type: 'info', note: '面板或反代自己声明的对外端口' },
  internal: { text: '仅本机', type: 'default', note: '只有这台机器自己能连' },
};
// 一页只说一件事：exposed 优先于 blocked，别让"还有个端口被挡着"稀释了真正的口子。
const portAlert = computed(() => {
  const d = portData.value;
  if (!d || portError.value) return null;
  if (!d.ports) return { type: 'error' as const, text: '读不到监听端口表（ss 不可用或退出码非 0），下面的判定都不可信。' };
  if (exposedCount.value) {
    return { type: 'error' as const, text: `${exposedCount.value} 个端口外部可达且不在预期内（共 ${d.counts.total} 个监听端口）。这是最该先看的一行。` };
  }
  if (blockedCount.value) {
    return { type: 'warning' as const, text: `没有对外多开的口子，另有 ${blockedCount.value} 个端口绑在全网、只是被防火墙挡住——改绑 127.0.0.1 才算真的关死。` };
  }
  const base = `没有对外多开的口子：${d.counts.total} 个监听端口里 ${d.counts.internal} 个仅本机、${d.counts.expected} 个是预期内的对外端口。`;
  if (!d.as_root) return { type: 'info' as const, text: `${base}（本次探测拿不到进程名，非 root 下 ss 会隐去它们）` };
  return { type: 'success' as const, text: base };
});

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
  // 端口同理，且门槛更高：只有"外部可达且不在预期内"才点亮，被防火墙挡住的用告警说话。
  { key: 'ports', label: '端口暴露面', badge: exposedCount.value > 0 },
  // 登录这道的红点门槛和端口一致：只有"要紧"才点，加固建议用页内告警说话。
  { key: 'login', label: '登录安全', badge: loginCounts.value.risk > 0 },
  // 存储这层的红点只认「要紧」（分区快满），值得处理的占用用页内告警说话：日志偏大、备份偏多天天都有，长期亮着就等于没亮。
  { key: 'storage', label: '存储占用', badge: storageCounts.value.risk > 0 },
  // 内存这一层的红点只认「要紧」（正在停顿、OOM 杀过人、顶到自己的上限）：天天都有的小波动长期亮着就等于没亮。
  { key: 'memory', label: '内存压力', badge: memCounts.value.risk > 0 },
  // 这一层的红点同样只认「要紧」：安全库的旧映射是要重启才算补上的，天天点没意义；
  // 值得处理（配置改过没 reload）用页内告警说话。
  { key: 'pending', label: '改动未生效', badge: pendCounts.value.risk > 0 },
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
      const text = uf ? ENABLED_LABELS[uf] || uf : r.unit.endsWith('.service') ? '未知' : '';
      const cls = uf === 'enabled' ? 'ok' : uf === 'masked' ? 'err' : uf === 'enabled-runtime' ? 'warn' : 'idle';
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

// 端口表：一个端口一行，判定和它的理由必须同屏——只给"对外可达"三个字，
// 管理员下一步还是得回 SSH 里手敲 ss + ufw status 才知道该拿它怎么办。
const VERDICT_ICON: Record<string, string> = {
  exposed: 'ShieldOutline', blocked: 'ShieldCheckmarkOutline', expected: 'GlobeOutline', internal: 'ServerOutline',
};
const portColumns: any[] = [
  {
    title: '端口', key: 'port', width: 130,
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, {
            component: icons[VERDICT_ICON[r.verdict]] || icons.InformationCircleOutline,
            size: 17,
            color: r.verdict === 'exposed' ? 'var(--cp-err)' : r.verdict === 'blocked' ? 'var(--cp-warn)' : 'var(--cp-text-mute)',
          }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name mono' }, String(r.port)),
          h('div', { class: 'cell-sub ls-wrap' }, r.addrs.join('、')),
        ]),
      ]),
  },
  {
    // 非 root 时 ss 会隐去进程名，这里如实显示"未知"而不是留空——留空看着像解析漏了。
    title: '进程', key: 'processes', width: 150,
    render: (r: any) =>
      h('div', { class: 'cell-txt' }, [
        h('span', { class: 'cell-name', title: r.processes.join('、') || '未知' }, r.processes.join('、') || '未知'),
        h('div', { class: 'cell-sub ls-wrap' }, r.pids.length ? `pid ${r.pids.slice(0, 3).join('、')}${r.pids.length > 3 ? '…' : ''}` : '—'),
      ]),
  },
  {
    title: '绑定', key: 'scope', width: 110,
    render: (r: any) => h('span', { class: 'st idle' }, SCOPE_LABELS[r.scope] || r.scope),
  },
  {
    title: '判定', key: 'verdict', width: 112,
    render: (r: any) => {
      const v = VERDICTS[r.verdict] ?? { text: r.verdict, type: 'default', note: '' };
      return h(NTag, { size: 'small', type: v.type, bordered: false, title: v.note }, () => v.text);
    },
  },
  {
    title: '说明与建议', key: 'reason', minWidth: 340,
    render: (r: any) =>
      h('div', { class: 'cell-txt' }, [
        h('div', { class: 'px-reason' }, r.reason),
        r.fix ? h('div', { class: 'px-fix' }, r.fix) : null,
      ]),
  },
];

const portPaging = { pageSize: 20, showSizePicker: false, showQuickJumper: false };
const loginPaging = { pageSize: 10, showSizePicker: false, showQuickJumper: false };
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
        :scroll-x="810" :max-height="tableH" :pagination="pager(filtered.length, paging)" :row-key="(r: any) => r.unit"
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
        :max-height="(patches?.packages || []).length ? tableH : undefined" :pagination="pager((patches?.packages || []).length, patchPaging)"
        :row-key="(r: any) => r.name" class="cp-tbl tbl-patches"
      >
        <template #empty>
          <EmptyBox :text="patchError && !patches ? '补丁清单获取失败，点上方「重新探测」重试' : '按本地包索引，没有待装更新'" />
        </template>
      </NDataTable>
    </CpToolbarCard>

    <CpToolbarCard v-else-if="tab === 'ports'" title="端口暴露面" style="margin-top: 10px">
      <template #right>
        <div class="px-toolbar">
          <NButton
            v-if="allPorts.length && riskyPorts.length !== allPorts.length"
            size="small" :quaternary="!showAllPorts" :type="showAllPorts ? 'primary' : 'default'"
            @click="showAllPorts = !showAllPorts"
          >
            {{ showAllPorts ? '只列能被外面碰到的' : `展开全部 ${allPorts.length} 个端口` }}
          </NButton>
          <NText v-if="portData" depth="3" class="px-time">
            对账于 {{ fmtDateTime(portData.checked_at) }}<template v-if="portData.cached">（缓存）</template>
          </NText>
          <NButton size="small" tertiary :loading="portBusy" :icon="icon('RefreshOutline', 14)" @click="refreshPorts">刷新</NButton>
          <NButton size="small" :loading="auditing" :icon="icon('SyncOutline', 14)" @click="reaudit">重新对账</NButton>
        </div>
      </template>

      <NAlert v-if="!portData && portError" type="error" :bordered="false" style="margin-bottom: var(--space-3)">
        端口对账失败：{{ (portError as Error).message || '未知错误' }}。点上方「重新对账」重试。
      </NAlert>
      <NAlert v-else-if="portAlert" :type="portAlert.type" :bordered="false" style="margin-bottom: var(--space-3)">
        {{ portAlert.text }}
      </NAlert>

      <div v-if="portData" class="px-chips">
        <span class="px-chip"><i>监听端口</i>{{ countOf('total') }} 个</span>
        <span class="px-chip" :class="{ err: exposedCount > 0 }"><i>外部可达</i>{{ exposedCount }} 个</span>
        <span class="px-chip" :class="{ warn: blockedCount > 0 }"><i>被防火墙挡住</i>{{ blockedCount }} 个</span>
        <span class="px-chip"><i>预期内</i>{{ countOf('expected') }} 个</span>
        <span class="px-chip"><i>仅本机</i>{{ countOf('internal') }} 个</span>
        <span class="px-chip">
          <i>防火墙</i>
          <template v-if="portData.firewall.tool === 'none'">未安装</template>
          <template v-else>{{ portData.firewall.tool }}{{ portData.firewall.active ? ' 已启用' : ' 未启用' }}</template>
        </span>
        <span v-if="portData.firewall.default_incoming" class="px-chip">
          <i>默认入站</i>{{ portData.firewall.default_incoming === 'deny' ? '拒绝' : portData.firewall.default_incoming }}
        </span>
        <span v-if="!portData.as_root" class="px-chip warn"><i>进程名</i>非 root，ss 未返回</span>
      </div>

      <!-- 登记过端口却没有监听的：这是"面板以为还在跑"和"机器上真在跑"的差额，
           列在这里比只出现在应用页更可能被看见。 -->
      <NText v-if="silentApps.length" depth="3" class="px-silent">
        面板登记了 {{ silentApps.length }} 个应用端口当前没有监听：{{ silentLine }}
      </NText>
      <div v-if="portData && (portData.notes || []).length" class="px-notes">
        <div v-for="(n, i) in portData.notes" :key="i" class="px-note">{{ n }}</div>
      </div>

      <NDataTable
        :columns="portColumns" :data="shownPorts" size="small" :bordered="false"
        :loading="portBusy && !portData" :scroll-x="842"
        :max-height="shownPorts.length ? tableH : undefined" :pagination="pager(shownPorts.length, portPaging)"
        :row-key="(r: any) => r.port" class="cp-tbl tbl-ports"
      >
        <template #empty>
          <EmptyBox :text="portError && !portData ? '端口对账失败，点上方「重新对账」重试' : '这台机器上没有能被外面碰到的监听端口'" />
        </template>
      </NDataTable>
    </CpToolbarCard>

    <CpToolbarCard v-else-if="tab === 'login'" title="登录安全" style="margin-top: 10px">
      <template #right>
        <div class="ls-toolbar">
          <NText v-if="loginData" depth="3" class="ls-time">
            对账于 {{ fmtDateTime(loginData.checked_at) }}<template v-if="loginData.cached">（缓存）</template>
          </NText>
          <NButton size="small" tertiary :loading="loginBusy" :icon="icon('RefreshOutline', 14)" @click="refreshLogin">刷新</NButton>
          <NButton size="small" :loading="probingLogin" :icon="icon('SyncOutline', 14)" @click="recheckLogin">重新对账</NButton>
        </div>
      </template>

      <NAlert v-if="!loginData && loginError" type="error" :bordered="false" style="margin-bottom: var(--space-3)">
        登录安全对账失败：{{ (loginError as Error).message || '未知错误' }}。点上方「重新对账」重试。
      </NAlert>
      <NAlert v-else-if="loginAlert" :type="loginAlert.type" :bordered="false" style="margin-bottom: var(--space-3)">
        {{ loginAlert.text }}
      </NAlert>

      <div v-if="loginData" class="ls-chips">
        <span class="ls-chip" :class="{ err: loginCounts.risk > 0 }"><i>要紧</i>{{ loginCounts.risk }} 项</span>
        <span class="ls-chip" :class="{ warn: loginCounts.warn > 0 }"><i>值得处理</i>{{ loginCounts.warn }} 项</span>
        <span class="ls-chip"><i>知道就好</i>{{ loginCounts.note }} 项</span>
        <span class="ls-chip"><i>24h 失败认证</i>{{ loginAttempts.failed ?? '—' }} 次</span>
        <span class="ls-chip"><i>来源地址</i>{{ loginAttempts.distinct_ips ?? '—' }} 个</span>
        <span class="ls-chip"><i>成功登录</i>{{ acceptedLine }}</span>
        <span class="ls-chip" :class="{ warn: !guardRunning }"><i>爆破防护</i>{{ guardLine }}</span>
        <span class="ls-chip"><i>公钥</i>{{ loginData.keys.total_keys }} 把 / {{ loginData.keys.scanned }} 个文件</span>
        <span class="ls-chip" :class="{ err: loginData.sshd.reachable === true }">
          <i>sshd 端口</i>{{ loginData.sshd.port }} {{ reachLine }}
        </span>
      </div>

      <!-- 来源排行紧跟失败次数：只报"7187 次"是没有信息量的，攻击永远在打，
           要看的是是不是就那么三五个人在轮。 -->
      <div v-if="(loginAttempts.top || []).length" class="ls-ips">
        <span v-for="t in loginAttempts.top" :key="t.ip" class="ls-ip">
          <code class="mono">{{ t.ip }}</code><b>{{ t.count }}</b>
        </span>
      </div>

      <div v-if="loginData && (loginData.notes || []).length" class="ls-notes">
        <div v-for="(n, i) in loginData.notes" :key="i" class="ls-note">{{ n }}</div>
      </div>

      <NDataTable
        :columns="findingColumns" :data="loginFindings" size="small" :bordered="false"
        :loading="loginBusy && !loginData" :scroll-x="760" :pagination="pager(loginFindings.length, loginPaging)"
        :row-key="(r: any) => r.id" class="cp-tbl tbl-findings"
      >
        <template #empty>
          <EmptyBox :text="loginError && !loginData ? '登录对账失败，点上方「重新对账」重试' : '登录方式没有要紧项，也没有值得处理的加固建议'" />
        </template>
      </NDataTable>

      <div class="ls-block">
        <span class="section-title">
          <NIcon :component="icons.TerminalOutline" :size="15" color="var(--cp-brand-soft)" /> SSHD 有效配置
        </span>
        <NText depth="3" class="ls-src">
          读自 {{ loginData?.sshd.source }}{{ loginData?.sshd.effective ? '' : '（原文解析，Match 段内的值不计入）' }}
        </NText>
        <NButton
          v-if="flaggedSettings.length && flaggedSettings.length !== loginSettings.length"
          size="tiny" :quaternary="!onlyFlagged" :type="onlyFlagged ? 'primary' : 'default'"
          @click="onlyFlagged = !onlyFlagged"
        >
          {{ onlyFlagged ? `展开全部 ${loginSettings.length} 项` : `只看有判定的 ${flaggedSettings.length} 项` }}
        </NButton>
      </div>
      <NDataTable
        :columns="settingColumns" :data="shownSettings" size="small" :bordered="false"
        :loading="loginBusy && !loginData" :scroll-x="780" :pagination="pager(shownSettings.length, loginPaging)"
        :row-key="(r: any) => r.key" class="cp-tbl tbl-settings"
      >
        <template #empty>
          <EmptyBox text="有效配置里没有需要处理的项" />
        </template>
      </NDataTable>

      <div class="ls-block">
        <span class="section-title">
          <NIcon :component="icons.KeyOutline" :size="15" color="var(--cp-brand-soft)" /> authorized_keys
        </span>
        <NText v-if="loginData?.keys?.truncated" depth="3" class="ls-src">文件太多，只列了前若干把钥匙</NText>
      </div>
      <NDataTable
        :columns="keyColumns" :data="keyFiles" size="small" :bordered="false"
        :loading="loginBusy && !loginData" :scroll-x="740"
        :row-key="(r: any) => r.path" class="cp-tbl tbl-keys"
      >
        <template #empty>
          <EmptyBox :text="loginData && !loginData.as_root ? '非 root 只能扫到自己那一份' : '可登录账号里没有 authorized_keys'" />
        </template>
      </NDataTable>

      <div v-if="loginData" class="ls-block">
        <span class="section-title">
          <NIcon :component="icons.PeopleOutline" :size="15" color="var(--cp-brand-soft)" /> 账号面
        </span>
      </div>
      <div v-if="loginData" class="ls-acc">
        <div class="ls-acc-line">
          <i>可交互登录的账号</i>{{ loginData.accounts.login_count }} 个：<span class="mono">{{ loginData.accounts.login_shells.join('、') || '无' }}</span>
        </div>
        <div class="ls-acc-line">
          <i>空口令</i>
          <template v-if="loginData.accounts.shadow_unreadable">/etc/shadow 读不到，这一项没核对</template>
          <template v-else>{{ loginData.accounts.empty_password.length ? loginData.accounts.empty_password.join('、') : '没有' }}</template>
        </div>
        <div class="ls-acc-line"><i>多余的 uid=0</i>{{ loginData.accounts.uid0_extra.length ? loginData.accounts.uid0_extra.join('、') : '没有' }}</div>
        <div class="ls-acc-line"><i>sudo / wheel 成员</i>{{ sudoLine }}</div>
        <div v-for="(n, i) in loginData.accounts.notes || []" :key="i" class="ls-note">{{ n }}</div>
      </div>
    </CpToolbarCard>

    <CpToolbarCard v-else-if="tab === 'storage'" title="存储占用" style="margin-top: 10px">
      <template #right>
        <div class="st-toolbar">
          <NText v-if="storageData" depth="3" class="st-time">
            扫描于 {{ fmtDateTime(storageData.as_of) }}<template v-if="storageData.cached">（缓存）</template>
          </NText>
          <NButton size="small" tertiary :loading="storageBusy" :icon="icon('RefreshOutline', 14)" @click="refreshStorage">刷新</NButton>
          <NButton size="small" :loading="scanning" :icon="icon('SyncOutline', 14)" @click="rescan">重新扫描</NButton>
        </div>
      </template>

      <NAlert v-if="!storageData && storageError" type="error" :bordered="false" style="margin-bottom: var(--space-3)">
        存储扫描失败：{{ (storageError as Error).message || '未知错误' }}。点上方「重新扫描」重试。
      </NAlert>
      <NAlert v-else-if="storageAlert" :type="storageAlert.type" :bordered="false" style="margin-bottom: var(--space-3)">
        {{ storageAlert.text }}
      </NAlert>

      <div v-if="storageData" class="st-chips">
        <span class="st-chip" :class="{ err: tightDisk }">
          <i>分区已用</i>{{ diskInfo.ok ? `${diskInfo.used_pct}%（剩 ${diskInfo.free_human}）` : '读不到' }}
        </span>
        <span class="st-chip"><i>分区总量</i>{{ diskInfo.total_human || '—' }}</span>
        <span class="st-chip" :class="{ err: tightMounts.length > 0 }">
          <i>挂载点</i>{{ mountRows.length }} 个<template v-if="tightMounts.length"> · {{ tightMounts.length }} 处吃紧</template><template v-else-if="mountData.omitted">（另有 {{ mountData.omitted }} 个未列）</template>
        </span>
        <span class="st-chip"><i>systemd 日志</i>{{ storageData.journal?.bytes_human ?? '读不到' }}</span>
        <span class="st-chip"><i>合计</i>{{ storageCounts.bytes_total_human }} / {{ storageCounts.roots }} 个目录</span>
        <span class="st-chip" :class="{ err: storageCounts.risk > 0 }"><i>要紧</i>{{ storageCounts.risk }} 项</span>
        <span class="st-chip" :class="{ warn: storageCounts.warn > 0 }"><i>值得处理</i>{{ storageCounts.warn }} 项</span>
        <span class="st-chip"><i>知道就好</i>{{ storageCounts.info }} 项</span>
        <span v-if="backupLine" class="st-chip"><i>备份产物</i>{{ backupLine }}</span>
      </div>

      <div v-if="storageData && (storageData.notes || []).length" class="st-notes">
        <div v-for="(n, i) in storageData.notes" :key="i" class="st-note">{{ n }}</div>
      </div>

      <NDataTable
        :columns="rootColumns" :data="storageRoots" size="small" :bordered="false"
        :loading="storageBusy && !storageData" :scroll-x="876"
        :max-height="storageRoots.length ? tableH : undefined" :pagination="false"
        :row-key="(r: any) => r.path" class="cp-tbl tbl-roots"
      >
        <template #empty>
          <EmptyBox :text="storageError && !storageData ? '存储扫描失败，点上方「重新扫描」重试' : '没有可扫描的目录：数据目录与 CP_FILE_ROOTS 都读不到'" />
        </template>
      </NDataTable>

      <div v-if="storageData" class="st-block">
        <span class="section-title">
          <NIcon :component="icons.LayersOutline" :size="15" color="var(--cp-brand-soft)" /> 挂载点
        </span>
        <NText depth="3" class="st-src">
          <template v-if="mountData.ok === false">{{ mountData.reason || '读不到挂载点表' }}</template>
          <template v-else-if="mountData.omitted">已列出最吃紧的 {{ mountRows.length }} 个，另有 {{ mountData.omitted }} 个都不比这些紧</template>
          <template v-else>每一个挂载点一行，同一块盘挂两处会标出来；内核自己挂的那些不列</template>
        </NText>
      </div>
      <NDataTable
        v-if="storageData" :columns="mountColumns" :data="mountRows" size="small" :bordered="false"
        :loading="storageBusy && !storageData" :scroll-x="654" :pagination="false"
        :row-key="(r: any) => r.path" class="cp-tbl tbl-mounts"
      >
        <template #empty>
          <EmptyBox :text="mountData.ok === false ? (mountData.reason || '读不到 /proc/mounts') : '没有列出任何挂载点'" />
        </template>
      </NDataTable>

      <div class="st-block">
        <span class="section-title">
          <NIcon :component="icons.AlertCircleOutline" :size="15" color="var(--cp-brand-soft)" /> 判词
        </span>
        <NButton
          v-if="infoFindings.length"
          size="tiny" :quaternary="onlySerious" :type="onlySerious ? 'default' : 'primary'"
          @click="onlySerious = !onlySerious"
        >
          {{ onlySerious ? `展开 ${infoFindings.length} 条「知道就好」` : '只看在意的' }}
        </NButton>
      </div>
      <NDataTable
        :columns="storageFindingColumns" :data="shownFindings" size="small" :bordered="false"
        :loading="storageBusy && !storageData" :scroll-x="696" :pagination="pager(shownFindings.length, storagePaging)"
        :row-key="(r: any) => r.id" class="cp-tbl tbl-findings"
      >
        <template #empty>
          <EmptyBox :text="onlySerious && infoFindings.length ? '要紧的没有，值得处理的也没有' : '没有异常占用'" />
        </template>
      </NDataTable>

      <div v-if="bigFiles.length" class="st-block">
        <span class="section-title">
          <NIcon :component="icons.DocumentTextOutline" :size="15" color="var(--cp-brand-soft)" />
          最大的 {{ bigFiles.length }} 个文件
        </span>
        <NText depth="3" class="st-src">只点名，不替你判断它还需不需要</NText>
      </div>
      <NDataTable
        v-if="bigFiles.length" :columns="bigFileColumns" :data="bigFiles" size="small" :bordered="false"
        :scroll-x="450" :pagination="pager(bigFiles.length, storagePaging)" :row-key="(r: any) => r.path" class="cp-tbl tbl-bigfiles"
      />
    </CpToolbarCard>
    <CpToolbarCard v-else-if="tab === 'memory'" title="内存压力" style="margin-top: 10px">
      <template #right>
        <div class="mm-toolbar">
          <NText v-if="memData" depth="3" class="mm-time">
            采样于 {{ fmtDateTime(memData.as_of) }}<template v-if="memData.cached">（缓存）</template>
          </NText>
          <NButton size="small" tertiary :loading="memBusy" :icon="icon('RefreshOutline', 14)" @click="refreshMemory">刷新</NButton>
          <NButton size="small" :loading="sampling" :icon="icon('SyncOutline', 14)" @click="resample">重新采样</NButton>
        </div>
      </template>

      <NAlert v-if="!memData && memError" type="error" :bordered="false" style="margin-bottom: var(--space-3)">
        内存探测失败：{{ (memError as Error).message || '未知错误' }}。点上方「重新采样」重试。
      </NAlert>
      <NAlert v-else-if="memAlert" :type="memAlert.type" :bordered="false" style="margin-bottom: var(--space-3)">
        {{ memAlert.text }}
      </NAlert>

      <div v-if="memData" class="mm-chips">
        <span class="mm-chip" :class="usedTone">
          <i>已用</i>{{ memInfo.ok ? `${memInfo.used_pct}%（剩 ${fmtBytes(memInfo.available)}）` : '读不到' }}
        </span>
        <span class="mm-chip"><i>可腾出</i>{{ memInfo.ok ? `${fmtBytes(memInfo.reclaimable)} 页缓存与可回收 slab` : '—' }}</span>
        <span class="mm-chip" :class="swapTone">
          <i>swap</i>{{ memInfo.swap_total ? `${memInfo.swap_pct}%（${fmtBytes(memInfo.swap_used)} / ${fmtBytes(memInfo.swap_total)}）` : '这台机器没配 swap' }}
        </span>
        <span class="mm-chip" :class="psiTone">
          <i>PSI some</i>{{ pressInfo.ok ? `近 10 秒 ${avgOf('avg10')}% · 近 5 分钟 ${avgOf('avg300')}%` : '读不到' }}
        </span>
        <span v-if="psiStopped" class="mm-chip"><i>累计停顿</i>{{ psiStopped }}</span>
        <span class="mm-chip" :class="rateTone">
          <i>直接回收</i>{{ rateInfo.ok ? `${rateNum(rateInfo.direct_reclaim)} 页/秒` : '速率没采到' }}
        </span>
        <span class="mm-chip" :class="rateTone"><i>换出</i>{{ rateInfo.ok ? rateBytes(rateInfo.swap_out) : '速率没采到' }}</span>
        <span class="mm-chip"><i>缺页等待</i>{{ rateInfo.ok ? `${rateNum(rateInfo.majfault, ' 次/秒')}` : '速率没采到' }}</span>
        <span class="mm-chip" :class="{ err: memKilled }">
          <i>OOM</i><template v-if="killTotal">内核杀过 {{ killTotal }} 次</template>
          <template v-else-if="memKilled">{{ Object.keys(unitKills).length }} 个单元被自己的上限杀过</template>
          <template v-else>没有杀过进程</template>
        </span>
        <span class="mm-chip"><i>承诺</i>{{ memInfo.commit_limit ? `${fmtBytes(memInfo.committed)} / ${fmtBytes(memInfo.commit_limit)}` : '读不到' }}</span>
        <span class="mm-chip" :class="{ err: memCounts.risk > 0 }"><i>要紧</i>{{ memCounts.risk }} 项</span>
        <span class="mm-chip" :class="{ warn: memCounts.warn > 0 }"><i>值得处理</i>{{ memCounts.warn }} 项</span>
        <span class="mm-chip"><i>知道就好</i>{{ memCounts.info }} 项</span>
        <span class="mm-chip"><i>榜上</i>{{ memCounts.units }} 个单元 · 全机 {{ memCounts.procs }} 个进程</span>
      </div>

      <div v-if="memData && tunInfo.ok" class="mm-tun">
        <span>swappiness {{ tunInfo.swappiness ?? '读不到' }}</span>
        <span>overcommit_memory {{ tunInfo.overcommit_memory ?? '读不到' }}</span>
        <span>min_free_kbytes {{ tunInfo.min_free_kbytes ?? '读不到' }}</span>
        <span>panic_on_oom {{ tunInfo.panic_on_oom ?? '读不到' }}</span>
        <span>面板只读这些策略，不改它们</span>
      </div>

      <div v-if="memData && (memData.notes || []).length" class="mm-notes">
        <div v-for="(n, i) in memData.notes" :key="i" class="mm-note">{{ n }}</div>
      </div>

      <div class="mm-block">
        <span class="section-title">
          <NIcon :component="icons.AlertCircleOutline" :size="15" color="var(--cp-brand-soft)" /> 判词
        </span>
        <NText depth="3" class="mm-src">先说结论：有没有正在缺内存、谁最该被看一眼、内核杀没杀过人</NText>
        <NButton
          v-if="memInfoFindings.length"
          size="tiny" :quaternary="memOnlySerious" :type="memOnlySerious ? 'default' : 'primary'"
          @click="memOnlySerious = !memOnlySerious"
        >
          {{ memOnlySerious ? `展开 ${memInfoFindings.length} 条「知道就好」` : '只看在意的' }}
        </NButton>
      </div>
      <NDataTable
        :columns="memFindingColumns" :data="memShownFindings" size="small" :bordered="false"
        :loading="memBusy && !memData" :scroll-x="696" :pagination="pager(memShownFindings.length, memPaging)"
        :row-key="(r: any) => r.id" class="cp-tbl tbl-mem-findings"
      >
        <template #empty>
          <EmptyBox :text="memOnlySerious && memInfoFindings.length ? '要紧的没有，值得处理的也没有' : '没有异常：既没在换页，也没人顶到上限'" />
        </template>
      </NDataTable>

      <div v-if="memData" class="mm-block">
        <span class="section-title">
          <NIcon :component="icons.CubeOutline" :size="15" color="var(--cp-brand-soft)" /> 谁拿着这些内存
        </span>
        <NText depth="3" class="mm-src">
          <template v-if="unitBlock.ok === false">{{ unitBlock.reason || '读不到 cgroup' }}</template>
          <template v-else>
            按各单元当前的 memory.current 排序<template v-if="unitBlock.omitted">，另有 {{ unitBlock.omitted }} 个都不比这些占得多</template><template v-if="unitBlock.managed">；其中 {{ unitBlock.managed }} 个是面板管理的应用</template>。面板自己那一行无条件留在表里。
          </template>
        </NText>
      </div>
      <NDataTable
        v-if="memData" :columns="unitColumns" :data="unitRows" size="small" :bordered="false"
        :loading="memBusy && !memData" :scroll-x="1046" :pagination="false"
        :max-height="unitRows.length ? tableH : undefined" :row-key="(r: any) => r.unit"
        class="cp-tbl tbl-mem-units"
      >
        <template #empty>
          <EmptyBox :text="unitBlock.ok === false ? (unitBlock.reason || '读不到 cgroup v2') : 'system.slice 里没有带内存统计的单元'" />
        </template>
      </NDataTable>

      <div v-if="memData" class="mm-block">
        <span class="section-title">
          <NIcon :component="icons.PulseOutline" :size="15" color="var(--cp-brand-soft)" /> 按 RSS 的前几名进程
        </span>
        <NText depth="3" class="mm-src">
          <template v-if="procBlock.ok === false">{{ procBlock.reason || '读不到 /proc' }}</template>
          <template v-else>
            逐行看，不要相加：共享页在每个进程的 RSS 里都算一遍。<template v-if="procBlock.budget_hit">这一轮为了读满 {{ procBlock.count }} 个进程的 statm 而用完预算、只收齐 {{ procBlock.scanned }} 个，榜外可能还有占得更多的。</template>
          </template>
        </NText>
      </div>
      <NDataTable
        v-if="memData" :columns="procColumns" :data="procRows" size="small" :bordered="false"
        :loading="memBusy && !memData" :scroll-x="620" :pagination="false"
        :max-height="procRows.length ? tableH : undefined" :row-key="(r: any) => r.pid"
        class="cp-tbl tbl-mem-procs"
      >
        <template #empty>
          <EmptyBox :text="procBlock.ok === false ? (procBlock.reason || '读不到 /proc') : '一个有常驻内存的进程都没读到'" />
        </template>
      </NDataTable>

      <div v-if="memKilled" class="mm-block">
        <span class="section-title">
          <NIcon :component="icons.WarningOutline" :size="15" color="var(--cp-err)" /> OOM 名单
        </span>
        <NText depth="3" class="mm-src">
          <template v-if="oomBlock.source === 'counter-only'">{{ oomBlock.reason || '只有次数，没有受害者' }}</template>
          <template v-else>当前 boot 的 kernel 日志里挑出来的这几行；认不出进程名的只留原文</template>
        </NText>
      </div>
      <NDataTable
        v-if="memKilled && oomRows.length" :columns="oomColumns" :data="oomRows" size="small" :bordered="false"
        :scroll-x="660" :pagination="false" :row-key="(r: any) => r.line" class="cp-tbl tbl-mem-oom"
      />

      <div v-if="swapDevs.length" class="mm-block">
        <span class="section-title">
          <NIcon :component="icons.LayersOutline" :size="15" color="var(--cp-brand-soft)" /> swap 设备
        </span>
        <NText depth="3" class="mm-src">写进去的不会自己回来；这台机器上真能用的换出空间就这几行</NText>
      </div>
      <NDataTable
        v-if="swapDevs.length" :columns="swapColumns" :data="swapDevs" size="small" :bordered="false"
        :scroll-x="490" :pagination="false" :row-key="(r: any) => r.name" class="cp-tbl tbl-mem-swaps"
      />
    </CpToolbarCard>
    <CpToolbarCard v-else-if="tab === 'pending'" title="改动未生效" style="margin-top: 10px">
      <template #right>
        <div class="mm-toolbar">
          <NText v-if="pendData" depth="3" class="mm-time">
            扫描于 {{ fmtDateTime(pendData.as_of) }}<template v-if="pendData.cached">（缓存）</template>
          </NText>
          <NButton size="small" tertiary :loading="pendBusy" :icon="icon('RefreshOutline', 14)" @click="refreshPending">刷新</NButton>
          <NButton size="small" :loading="rescanning" :icon="icon('SyncOutline', 14)" @click="rescanPending">重新扫描</NButton>
        </div>
      </template>

      <NAlert v-if="!pendData && pendError" type="error" :bordered="false" style="margin-bottom: var(--space-3)">
        改动对账失败：{{ (pendError as Error).message || '未知错误' }}。点上方「重新扫描」重试。
      </NAlert>
      <NAlert v-else-if="pendAlert" :type="pendAlert.type" :bordered="false" style="margin-bottom: var(--space-3)">
        {{ pendAlert.text }}
      </NAlert>

      <div v-if="pendData" class="mm-chips">
        <span class="mm-chip" :class="{ err: kernelInfo.needs_reboot === true }">
          <i>内核</i><template v-if="kernelInfo.ok">运行 {{ kernelInfo.running }}，已装 {{ (kernelInfo.installed || []).length }} 个镜像包<template v-if="(kernelInfo.newer || []).length">，其中 {{ kernelInfo.newer.map((x: any) => x.version).join('、') }} 还没用上</template>；{{ kernelInfo.needs_reboot === true ? '要重启才算补上' : kernelInfo.needs_reboot === false ? '跑的就是装着的那份' : '判定不了' }}</template>
          <template v-else>{{ kernelInfo.reason || '读不到' }}</template>
        </span>
        <span class="mm-chip">
          <i>本次启动</i>{{ kernelInfo.boot_iso ? `${fmtDateTime(kernelInfo.boot_iso)}（已跑 ${fmtDuration(pendData.uptime_sec)}）` : '读不到' }}
        </span>
        <span class="mm-chip" :class="{ err: staleRows.length > 0 }">
          <i>踩着旧文件</i>{{ staleInfo.ok ? `${staleInfo.procs_stale ?? 0} 个进程，归成 ${staleInfo.unit_total ?? 0} 个单元` : (staleInfo.reason || '读不到') }}
        </span>
        <span class="mm-chip">
          <i>这一轮扫了</i>{{ staleInfo.ok ? `${staleInfo.scanned ?? 0} / ${staleInfo.procs_total ?? 0} 个进程，${staleInfo.elapsed_ms ?? 0} 毫秒${staleInfo.budget_hit ? ' · 预算用完，没扫满' : ''}` : '—' }}
        </span>
        <span v-if="staleInfo.ok && staleInfo.orphan_total" class="mm-chip warn">
          <i>认不出归属</i>{{ staleInfo.orphan_total }} 个单元不归 systemd 管，得自己认领
        </span>
        <span class="mm-chip" :class="{ warn: reloadInfo.nginx?.pending === true }">
          <i>nginx 配置</i>{{ reloadInfo.nginx?.ok ? (reloadInfo.nginx.pending === true ? '盘上的比进程里的新，还没 reload' : reloadInfo.nginx.pending === false ? '进程里就是当前这份' : '判定不了') : (reloadInfo.nginx?.reason || '读不到') }}
        </span>
        <span class="mm-chip" :class="{ warn: reloadInfo.sshd?.pending === true }">
          <i>sshd 配置</i>{{ reloadInfo.sshd?.ok ? (reloadInfo.sshd.pending === true ? '盘上的比进程里的新，还没重启进程' : reloadInfo.sshd.pending === false ? '进程里就是当前这份' : '判定不了') : (reloadInfo.sshd?.reason || '读不到') }}
        </span>
        <span class="mm-chip" :class="{ warn: (reloadInfo.systemd?.need_reload || []).length > 0 }">
          <i>unit 文件</i>{{ reloadInfo.systemd?.ok ? ((reloadInfo.systemd.need_reload || []).length ? `${reloadInfo.systemd.need_reload.length} 个改过还没 daemon-reload` : `查了 ${reloadInfo.systemd.checked} 个单元，没有待重载`) : (reloadInfo.systemd?.reason || '读不到') }}
        </span>
        <span class="mm-chip">
          <i>apt</i>{{ aptInfo.ok ? `最近一笔事务 ${aptInfo.last_start_date || '读不到'}（历史里共 ${aptInfo.transactions} 笔）` : (aptInfo.reason || '读不到') }}
        </span>
        <span class="mm-chip" :class="{ err: pendCounts.risk > 0 }"><i>要紧</i>{{ pendCounts.risk }} 项</span>
        <span class="mm-chip" :class="{ warn: pendCounts.warn > 0 }"><i>值得处理</i>{{ pendCounts.warn }} 项</span>
        <span class="mm-chip"><i>知道就好</i>{{ pendCounts.info }} 项</span>
      </div>

      <div v-if="pendData" class="mm-tun">
        <span>只读对账：面板不 restart、不 reload、不 daemon-reload、不 reboot</span>
        <span>处置命令按「复制」拿走，请 SSH 进去自己执行</span>
        <span v-if="pendData.self_unit">面板自己跑在 {{ pendData.self_unit }}</span>
      </div>

      <div v-if="pendData && (pendData.notes || []).length" class="mm-notes">
        <div v-for="(n, i) in pendData.notes" :key="i" class="mm-note">{{ n }}</div>
      </div>

      <div class="mm-block">
        <span class="section-title">
          <NIcon :component="icons.AlertCircleOutline" :size="15" color="var(--cp-brand-soft)" /> 判词
        </span>
        <NText depth="3" class="mm-src">先说结论：哪一份改动还没进到正在跑的东西里、什么时候重启才算补上</NText>
        <NButton
          v-if="pendInfoFindings.length"
          size="tiny" :quaternary="pendOnlySerious" :type="pendOnlySerious ? 'default' : 'primary'"
          @click="pendOnlySerious = !pendOnlySerious"
        >
          {{ pendOnlySerious ? `展开 ${pendInfoFindings.length} 条「知道就好」` : '只看在意的' }}
        </NButton>
      </div>
      <NDataTable
        :columns="pendFindingColumns" :data="pendShownFindings" size="small" :bordered="false"
        :loading="pendBusy && !pendData" :scroll-x="696" :pagination="pager(pendShownFindings.length, pendPaging)"
        :row-key="(r: any) => r.id" class="cp-tbl tbl-pend-findings"
      >
        <template #empty>
          <EmptyBox :text="pendData && pendData.unreadable && pendData.unreadable.length ? '该出声的读不到，别把这一页读成「没有问题」' : '没有异常：装上去的都在跑着的那份里'" />
        </template>
      </NDataTable>

      <div v-if="pendData" class="mm-block">
        <span class="section-title">
          <NIcon :component="icons.CubeOutline" :size="15" color="var(--cp-brand-soft)" /> 还在跑旧文件的进程
        </span>
        <NText depth="3" class="mm-src">
          <template v-if="staleInfo.ok === false">{{ staleInfo.reason || '读不到 /proc' }}</template>
          <template v-else>
            按单元分组，映射里的文件在盘上已经换成了另一份（inode 对不上）。<template v-if="(staleInfo.unit_total ?? 0) > staleRows.length">这一轮列了 {{ staleRows.length }} 个，其余 {{ (staleInfo.unit_total ?? 0) - staleRows.length }} 个都不比这些更值得看。</template><template v-if="staleInfo.budget_hit">预算用完、只扫了 {{ staleInfo.scanned }} 个进程，榜外可能还有。</template>
          </template>
        </NText>
      </div>
      <NDataTable
        v-if="pendData" :columns="staleUnitColumns" :data="staleRows" size="small" :bordered="false"
        :loading="pendBusy && !pendData" :scroll-x="900" :pagination="false"
        :max-height="staleRows.length ? tableH : undefined" :row-key="(r: any) => `${r.unit}|${r.cmd}`"
        class="cp-tbl tbl-pend-units"
      >
        <template #empty>
          <EmptyBox :text="staleInfo.ok === false ? (staleInfo.reason || '读不到 /proc') : '没有进程踩着已被替换的映射'" />
        </template>
      </NDataTable>

      <div v-if="pendData" class="mm-block">
        <span class="section-title">
          <NIcon :component="icons.RefreshOutline" :size="15" color="var(--cp-brand-soft)" /> 配置和 unit 改过之后
        </span>
        <NText depth="3" class="mm-src">
          比的是「盘上最新的 mtime」和「进程被重载的时刻」：nginx 拿最晚启动的 worker 比（<code>nginx -s reload</code> 只换 worker、不换 master，拿 master 比会把已经 reload 过的判成没 reload），sshd 拿主进程比，unit 文件直接问 systemctl 的 NeedDaemonReload。
        </NText>
      </div>
      <NDataTable
        v-if="pendData" :columns="reloadColumns" :data="reloadRows" size="small" :bordered="false"
        :scroll-x="1010" :pagination="false" :row-key="(r: any) => r.kind" class="cp-tbl tbl-pend-reloads"
      />
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
/* 端口暴露面：判定结果用 chip 汇总，具体理由留在表里。
   告警 + chip + 表三层读下来正好是"有没有事 / 事的规模 / 是哪一行"。 */
.px-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.px-time { font-size: var(--fs-xs); }
.px-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: var(--space-3); }
.px-chip {
  display: inline-flex; align-items: baseline; gap: 6px; padding: 4px 10px;
  border-radius: var(--radius-md); background: var(--cp-bg-soft); font-size: var(--fs-sm); color: var(--cp-text);
}
.px-chip i { font-style: normal; font-size: var(--fs-xs); color: var(--cp-text-mute); }
.px-chip.warn { color: var(--cp-warn); }
.px-chip.err { color: var(--cp-err); }
.px-silent { display: block; margin: -4px 0 var(--space-3); font-size: var(--fs-xs); }
.px-notes { margin: -4px 0 var(--space-3); }
.px-note { font-size: var(--fs-xs); color: var(--cp-text-dim); line-height: 1.7; }
/* 登录安全：判词 + 依据 + "回去自己执行"的命令同屏。指纹给 ellipsis + title，
   不让一行 SHA256 把整张表顶出视口；判词是整句中文，折行（见 .ls-wrap）。
   下面这些 .cp-tbl :deep(...) 不是冗余写法：单元格内容由 h() 生成，拿不到本组件的
   data-v 作用域，纯 class 的 scoped 规则一条都不会命中（线上探针实测 .ls-fp 无 data-v）。
   必须从模板上的表级类走 :deep，规则才真的落进格子里。 */
.cp-tbl :deep(.ls-wrap) { white-space: normal; overflow: visible; text-overflow: clip; line-height: 1.6; }
.cp-tbl :deep(.px-reason) { font-size: var(--fs-sm); color: var(--cp-text); line-height: 1.6; }
.cp-tbl :deep(.px-fix) { font-size: var(--fs-xs); color: var(--cp-text-dim); line-height: 1.6; }
.cp-tbl :deep(.ls-value) { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.cp-tbl :deep(.ls-key) { display: flex; align-items: center; gap: 6px; min-width: 0; font-size: var(--fs-xs); line-height: 1.8; }
.cp-tbl :deep(.ls-fp) { min-width: 0; flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cp-tbl :deep(.ls-key-name) { color: var(--cp-text-dim); flex: 0 1 auto; }
.cp-tbl :deep(.ls-flag) { font-size: var(--fs-xs); color: var(--cp-warn); line-height: 1.7; }
.ls-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.ls-time { font-size: var(--fs-xs); }
.ls-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: var(--space-3); }
.ls-chip {
  display: inline-flex; align-items: baseline; gap: 6px; padding: 4px 10px; max-width: 100%;
  border-radius: var(--radius-md); background: var(--cp-bg-soft); font-size: var(--fs-sm); color: var(--cp-text);
}
.ls-chip i { font-style: normal; font-size: var(--fs-xs); color: var(--cp-text-mute); }
.ls-chip.warn { color: var(--cp-warn); }
.ls-chip.err { color: var(--cp-err); }
.ls-ips { display: flex; flex-wrap: wrap; gap: 6px; margin: -4px 0 var(--space-3); }
.ls-ip {
  display: inline-flex; align-items: baseline; gap: 5px; padding: 3px 8px;
  border-radius: var(--radius-sm); background: var(--cp-bg-soft); font-size: var(--fs-xs);
}
.ls-ip b { color: var(--cp-text-dim); font-weight: 500; }
.ls-notes { margin: -4px 0 var(--space-3); }
.ls-note { font-size: var(--fs-xs); color: var(--cp-text-dim); line-height: 1.7; }
.ls-block { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: var(--space-3) 0 6px; }
.ls-src { font-size: var(--fs-xs); }
.ls-acc { display: grid; gap: 4px; margin-bottom: var(--space-3); }
.ls-acc-line { font-size: var(--fs-sm); color: var(--cp-text); }
.ls-acc-line i { font-style: normal; display: inline-block; min-width: 130px; color: var(--cp-text-mute); font-size: var(--fs-xs); }
@media (max-width: 767px) {
  .ls-toolbar { justify-content: flex-start; }
  .ls-acc-line i { min-width: 0; }
  /* 窄屏上一行 SHA256 截掉尾巴就没法比对（指纹的全部意义是两边对得上），
     所以这里放开 ellipsis，让它折成两行完整显示。 */
  .cp-tbl :deep(.ls-fp) { max-width: none; white-space: normal; word-break: break-all; overflow: visible; text-overflow: clip; }
}
@media (max-width: 767px) {
  .px-toolbar { justify-content: flex-start; }
}
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
/* 存储占用：分区余量 + 每个根的量与构成 + 判词。格子里的类同样走 .cp-tbl :deep，
   理由和上面登录安全一样——h() 生成的节点拿不到本组件的 data-v 作用域。 */
.cp-tbl :deep(.st-bar) {
  display: block; width: 100%; height: 4px; margin: 4px 0 2px;
  border-radius: 2px; background: var(--cp-bg-soft); overflow: hidden;
}
.cp-tbl :deep(.st-bar i) { display: block; height: 100%; border-radius: 2px; background: var(--cp-brand-soft); }
/* 90% 以上才染红，和后端判词用的是同一条线：条子比判词先红，人就会去查一条其实不要紧的盘。 */
.cp-tbl :deep(.st-bar i.hot) { background: var(--cp-err); }
.cp-tbl :deep(.st-pieces) { display: flex; flex-wrap: wrap; gap: 5px; min-width: 0; }
.cp-tbl :deep(.st-piece) {
  display: inline-flex; align-items: baseline; gap: 5px; max-width: 100%; padding: 2px 7px;
  border-radius: var(--radius-sm); background: var(--cp-bg-soft); font-size: var(--fs-xs); color: var(--cp-text);
}
.cp-tbl :deep(.st-piece code) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 150px; }
.cp-tbl :deep(.st-piece b) { font-weight: 500; color: var(--cp-text-dim); }
.cp-tbl :deep(.st-piece.rest) { background: transparent; color: var(--cp-text-mute); }
.cp-tbl :deep(.st-marks) { display: flex; flex-direction: column; gap: 3px; }
.cp-tbl :deep(.st-mark) { font-size: var(--fs-xs); color: var(--cp-warn); line-height: 1.6; }
.cp-tbl :deep(.st-mark.mute) { color: var(--cp-text-mute); }
.cp-tbl :deep(.st-fix) { display: flex; align-items: center; gap: 6px; min-width: 0; }
.cp-tbl :deep(.st-fix-cmd) {
  flex: 0 1 auto; min-width: 0; padding: 3px 7px; border-radius: var(--radius-sm);
  background: var(--cp-bg-soft); font-size: var(--fs-xs); overflow-x: auto; white-space: nowrap;
}
.st-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.st-time { font-size: var(--fs-xs); }
.st-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: var(--space-3); }
.st-chip {
  display: inline-flex; align-items: baseline; gap: 6px; max-width: 100%; padding: 4px 10px;
  border-radius: var(--radius-md); background: var(--cp-bg-soft); font-size: var(--fs-sm); color: var(--cp-text);
}
.st-chip i { font-style: normal; font-size: var(--fs-xs); color: var(--cp-text-mute); }
.st-chip.warn { color: var(--cp-warn); }
.st-chip.err { color: var(--cp-err); }
.st-notes { margin: -4px 0 var(--space-3); }
.st-note { font-size: var(--fs-xs); color: var(--cp-text-dim); line-height: 1.7; }
.st-block { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: var(--space-3) 0 6px; }
.st-src { font-size: var(--fs-xs); }
@media (max-width: 767px) {
  .st-toolbar { justify-content: flex-start; }
  /* 窄屏上路径和清理命令都不能被截断：截了就没法照着敲，也就失去了这一页唯一的用途。 */
  .cp-tbl :deep(.st-piece code) { max-width: none; white-space: normal; word-break: break-all; overflow: visible; text-overflow: clip; }
  .cp-tbl :deep(.st-fix) { flex-wrap: wrap; }
  .cp-tbl :deep(.st-fix-cmd) { flex: 1 1 100%; max-width: 100%; white-space: normal; word-break: break-all; }
}

/* 内存压力：总量与速率的结论用 chip 摆最上面，往下是「谁拿着」（单元榜、进程榜）和已经杀过人的
   名单。格子里的类走 .cp-tbl :deep，理由和登录安全那页一样——h() 生成的节点拿不到本组件的 data-v。
   条子、标记、可复制命令直接复用存储那页的 .st-bar / .st-marks / .st-fix，两页对照时形状不会分叉。 */
.mm-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.mm-time { font-size: var(--fs-xs); }
.mm-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: var(--space-3); }
.mm-chip {
  display: inline-flex; align-items: baseline; gap: 6px; max-width: 100%; padding: 4px 10px;
  border-radius: var(--radius-md); background: var(--cp-bg-soft); font-size: var(--fs-sm); color: var(--cp-text);
}
.mm-chip i { font-style: normal; font-size: var(--fs-xs); color: var(--cp-text-mute); }
.mm-chip.warn { color: var(--cp-warn); }
.mm-chip.err { color: var(--cp-err); }
/* 内核策略单独一行：判词里说「swappiness=60 时写 swap 是内核的主动选择」，得让人看见那个 60 从哪来。 */
.mm-tun { display: flex; flex-wrap: wrap; gap: 14px; margin: -6px 0 var(--space-3); font-size: var(--fs-xs); color: var(--cp-text-dim); }
.mm-notes { margin: -4px 0 var(--space-3); }
.mm-note { font-size: var(--fs-xs); color: var(--cp-text-dim); line-height: 1.7; }
.mm-block { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: var(--space-3) 0 6px; }
.mm-src { font-size: var(--fs-xs); }
/* 一个单元可以踩着好几份被替换的文件，逐份之间要有分隔：整行给路径，不截断。
   和下面那组一样：格子里的内容是 h() 生成的，没有本组件的 scope id，不走 :deep 这条规则就静默丢掉 */
.cp-tbl :deep(.pd-file + .pd-file) { margin-top: 6px; padding-top: 6px; border-top: 1px dashed var(--cp-border); }
.cp-tbl :deep(.mm-tag) {
  display: inline-block; margin-left: 6px; padding: 1px 6px; border-radius: var(--radius-sm);
  font-size: var(--fs-2xs); font-weight: 500; vertical-align: 1px; background: var(--cp-bg-soft); color: var(--cp-text-dim);
}
.cp-tbl :deep(.mm-tag.self) { color: var(--cp-brand); }
.cp-tbl :deep(.mm-tag.app) { color: var(--cp-warn); }
/* 单元名、设备名、kernel 日志原文都是没有空格的长 token，只放开 white-space 会在定宽列里溢出被裁成
   半截；overflow-wrap 让它在真要越界时才断行。 */
.cp-tbl :deep(.mm-break) { overflow-wrap: anywhere; word-break: normal; }
@media (max-width: 767px) {
  .mm-toolbar { justify-content: flex-start; }
}

/* 1Panel 日志盒参数压过 LogStream 的行内高度（组件根上的类，:deep 到 .log-view） */
.svc-log :deep(.log-view) {
  height: calc(100vh - 320px) !important;
  min-height: 420px;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
}
</style>
