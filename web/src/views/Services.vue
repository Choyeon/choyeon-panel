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
        :max-height="shownPorts.length ? tableH : undefined" :pagination="portPaging"
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
        :loading="loginBusy && !loginData" :scroll-x="760" :pagination="loginPaging"
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
        :loading="loginBusy && !loginData" :scroll-x="780" :pagination="loginPaging"
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
        :loading="storageBusy && !storageData" :scroll-x="696" :pagination="storagePaging"
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
        :scroll-x="450" :pagination="storagePaging" :row-key="(r: any) => r.path" class="cp-tbl tbl-bigfiles"
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

/* 1Panel 日志盒参数压过 LogStream 的行内高度（组件根上的类，:deep 到 .log-view） */
.svc-log :deep(.log-view) {
  height: calc(100vh - 320px) !important;
  min-height: 420px;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
}
</style>
