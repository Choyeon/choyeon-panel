<script setup lang="ts">
import { computed, onMounted, ref, watch, h } from 'vue';
import { useRouter } from 'vue-router';
import {
  NDataTable, NSpace, NTag, NDrawer, NDrawerContent, NForm, NFormItem, NInputNumber,
  NSelect, useMessage, NIcon, NAlert, NSwitch,
} from 'naive-ui';
import { NButton, useDrawerWidth } from '../ui';
import { api, getRole } from '../api';
import { icon, icons } from '../icons';
import CpToolbarCard from '../components/CpToolbarCard.vue';
import CpRowActions from '../components/CpRowActions.vue';
import ConfirmActionDialog from '../components/ConfirmActionDialog.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { useCachedFetch, invalidateCache } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';
import { fmtBytes, fmtDateTime } from '../composables/useFormat';
import { useVisiblePoll } from '../composables/useVisiblePoll';

const msg = useMessage();
const router = useRouter();
// 备份任务的增删改与立即执行都是 MUTATION，服务端统一拦 403：只读账号只看计数
const isAdmin = getRole() === 'admin';
const show = ref(false);
const drawerW = useDrawerWidth(0.5);
const busy = ref<number | null>(null);
const toggling = ref<number | null>(null);
const submitting = ref(false);
const editingId = ref<number | null>(null);
const form = ref<any>({ kind: 'pg', target: null, schedule: 'daily', hour: 3, minute: 30, keep: 7, enabled: true });
// pg 的 target 是库名，不预填任何值：这里曾经硬编码过一个别的项目的库名，
// 换一台机器建任务就会带着它提交——pg 的库名只校验格式、不校验存在性，
// 于是任务照样创建成功，每晚 pg_dump 在 runner 里失败（与批次⑤O 同一类缺陷）。
const pgDbs = ref<any[]>([]);

// 执行日志抽屉：立即备份的返回体带 log，失败也要能看输出
const logShow = ref(false);
const logText = ref('');
const logTitle = ref('');

// 备份任务表几乎不变（只有改计划/删除会变），30s 缓存让这一页秒开；
// 应用列表只用于下拉建议与目标名展示，缓存 60s。
const { data, error, loading, refreshing, refresh } = useCachedFetch<any[]>({
  key: () => 'backups',
  loader: () => api.backups(),
  maxAge: 30000,
});
const { data: appsData, error: appsError, load: loadApps } = useCachedFetch<any[]>({
  key: () => 'apps',
  loader: () => api.apps(),
  maxAge: 60000,
});
watch(error, (e) => { if (e) msg.error((e as Error).message || String(e)); });
// 这一份失败只影响下拉建议与目标名展示，不拦任何操作，所以是 warning 不是 error；
// 但不说就成了另一回事：下拉空着，人只会以为这台机器上没有应用。
watch(appsError, (e) => { if (e) msg.warning(`应用列表没取到，新建备份任务的下拉不会给建议：${(e as Error).message || String(e)}`); });
// 原实现在 onMounted 里吞掉应用列表的错误（列表只影响下拉建议），保持一致
void loadApps().catch(() => undefined);

const rows = computed<any[]>(() => data.value ?? []);
const apps = computed<any[]>(() => appsData.value ?? []);

const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

function reload() {
  invalidateCache('backups');
  // 立即备份/删除任务都会写审计，审计页的缓存一起作废
  invalidateCache('audit:');
  return refresh().catch(() => undefined);
}

// 1Panel TableSetting 的自动刷新：0/5/10/30/60/120/300s，localStorage 记忆
const AUTO_OPTS = [0, 5, 10, 30, 60, 120, 300].map((s) => ({ label: s ? `${s} 秒` : '关闭自动刷新', value: s }));
const autoSec = ref(Math.max(0, Number(localStorage.getItem('backups-auto-refresh')) || 0));
const autoPoll = useVisiblePoll(() => void reload(), autoSec.value * 1000);
watch(autoSec, (v) => { localStorage.setItem('backups-auto-refresh', String(v)); autoPoll.every(v * 1000); });
onMounted(autoPoll.start);

const kindOpts = [
  { label: 'PostgreSQL 数据库', value: 'pg' },
  { label: '应用目录 (tar.gz)', value: 'app' },
  { label: '应用 SQLite 库（在线一致性快照）', value: 'sqlite' },
];
const schedOpts = [
  { label: '手动执行', value: 'manual' },
  { label: '每日', value: 'daily' },
  { label: '每周一', value: 'weekly' },
];
const KIND_ICON: Record<string, string> = { pg: 'CubeOutline', app: 'FolderOutline', sqlite: 'ServerOutline' };
const KIND_DESC: Record<string, string> = {
  pg: 'pg_dump 自定义格式（pg_restore 可恢复）',
  app: '应用目录打包（排除依赖目录）',
  sqlite: 'SQLite 在线备份 API 快照（自动发现目录内 *.db）',
};

const appOpts = computed(() => apps.value.map((a: any) => ({ label: a.name, value: a.name })));
// sqlite 的 target 存 app_id：应用改名后按名的 target 会指向不存在的目录
const sqliteOpts = computed(() => apps.value.map((a: any) => ({ label: a.name, value: String(a.id) })));
const targetOptions = computed(() => (form.value.kind === 'sqlite' ? sqliteOpts.value : appOpts.value));
// pg 目标下拉：真实库名 + all。列表拿不到（未装 PG / 连接失败）时留空，
// 选择器仍可键入，不把这条路径堵死。
const pgOpts = computed<any[]>(() => [
  ...pgDbs.value.map((d: any) => ({ label: `${d.name}（${d.size || '大小未知'}）`, value: d.name })),
  { label: 'all（整库 pg_dumpall，只能手工恢复）', value: 'all' },
]);
async function loadPg() {
  try {
    pgDbs.value = await api.pgDbs();
  } catch {
    pgDbs.value = [];
  }
}

const columns: any[] = [
  {
    title: '任务', key: 'target',
    render: (r: any) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: icons[KIND_ICON[r.kind] || 'FolderOutline'], size: 18, color: r.kind === 'pg' ? 'var(--cp-brand-soft)' : 'var(--cp-node)' }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('span', { class: 'cell-name', title: r.kind === 'sqlite' ? sqliteTargetName(r.target) : r.target }, r.kind === 'sqlite' ? sqliteTargetName(r.target) : r.target),
          r.target_missing
            ? h(NTag, { size: 'tiny', type: 'warning', bordered: false, class: 'cell-sub' }, () => '目标应用已删除，备份将失败')
            : null,
        ]),
      ]),
  },
  {
    // 1Panel Status operate 模式：启用/停用是可点的小圆按钮，点击直发 updateBackup。
    // 以前这颗开关和"每日 03:00"挤在同一列、列名还和定时任务页的「计划」（cron 表达式）
    // 同名不同义，两页来回切的时候读不出这是哪一类信息。
    title: '启用', key: 'enabled', width: 90,
    render: (r: any) =>
      r.schedule === 'manual'
        ? h('span', { class: 'cell-sub' }, '—')
        : isAdmin
          ? h(NButton, {
              size: 'tiny', round: true, tertiary: true, type: r.enabled ? 'success' : 'info',
              loading: toggling.value === r.id, disabled: toggling.value !== null,
              onClick: () => toggleEnabled(r),
            }, () => (r.enabled ? '启用' : '停用'))
          // 只读账号不给按不动的开关，直接只显示状态文字
          : h(NTag, { size: 'small', round: true, bordered: false, type: r.enabled ? 'success' : 'default' }, () => (r.enabled ? '启用' : '停用')),
  },
  {
    title: '调度', key: 'schedule', width: 130,
    render: (r: any) =>
      r.schedule === 'manual'
        ? h(NTag, { size: 'small', round: true, bordered: false }, () => '手动')
        : h('span', { class: 'mono-dim' }, `${r.schedule === 'daily' ? '每日' : '每周一'} ${String(r.hour).padStart(2, '0')}:${String(r.minute).padStart(2, '0')}`),
  },
  { title: '保留', key: 'keep', width: 70, render: (r: any) => h('span', { class: 'mono-dim' }, `${r.keep} 份`) },
  {
    title: '产物', key: 'files', width: 250,
    render: (r: any) =>
      r.files.length
        ? h('div', { class: 'cell-txt' }, [
            h(NButton, { text: true, size: 'small', type: 'primary', class: 'cp-op', onClick: () => router.push({ path: '/backups/files', query: { job: String(r.id) } }) }, () => `${r.files.length} 份`),
            h('div', { class: 'cell-sub' }, `合计 ${fmtBytes(r.files.reduce((n: number, f: any) => n + (f.size || 0), 0))} · 最近 ${fmtDateTime(r.files[0].mtime)} UTC`),
          ])
        : h('span', { class: 'cell-sub' }, '—'),
  },
  {
    title: '操作', key: 'ops', width: 230, align: 'center', fixed: 'right' as const,
    render: (r: any) =>
      h(CpRowActions, {
        ellipsis: 3,
        buttons: [
          ...(isAdmin
            ? [{ label: busy.value === r.id ? '备份中…' : '立即备份', run: () => run(r.id), disabled: busy.value !== null }]
            : []),
          { label: '备份文件', run: () => router.push({ path: '/backups/files', query: { job: String(r.id) } }), disabled: busy.value === r.id || !r.files.length },
          ...(isAdmin
            ? [
                { label: '编辑', run: () => openEdit(r), disabled: busy.value === r.id },
                { label: '删除', danger: true, run: () => confirmDel(r), disabled: busy.value === r.id },
              ]
            : []),
        ],
      }),
  },
];

function displayName(r: any) {
  return r.kind === 'sqlite' ? sqliteTargetName(r.target) : r.target;
}

function sqliteTargetName(target: any) {
  const t = String(target || '');
  const hit = apps.value.find((a: any) => String(a.id) === t || a.name === t);
  return hit ? `${hit.name} · SQLite` : `应用 #${t} · SQLite`;
}

async function toggleEnabled(r: any) {
  if (toggling.value !== null) return;
  toggling.value = r.id;
  try {
    await api.updateBackup(r.id, { enabled: !r.enabled });
    await reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    toggling.value = null;
  }
}

async function run(id: number) {
  if (busy.value !== null) return;
  busy.value = id;
  try {
    const r = await api.runBackup(id);
    if (r.code === 0) msg.success('备份完成');
    else msg.error(`备份失败 (exit ${r.code})`);
    const job = rows.value.find((x: any) => x.id === id);
    if (r.log) {
      logText.value = r.log;
      logTitle.value = job ? displayName(job) : `任务 #${id}`;
      logShow.value = true;
    }
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = null;
  }
}

const delDialog = ref<InstanceType<typeof ConfirmActionDialog> | null>(null);
function confirmDel(r: any) {
  delDialog.value?.acceptParams({
    title: '确认删除备份任务',
    names: [displayName(r)],
    msg: '删除任务及其 systemd timer（已生成的备份文件保留），操作不可撤销。',
    api: async () => {
      await api.deleteBackup(r.id);
      await reload();
    },
    successMsg: '已删除',
  });
}

function blankForm() {
  return { kind: 'pg', target: null, schedule: 'daily', hour: 3, minute: 30, keep: 7, enabled: true };
}
function openCreate() {
  editingId.value = null;
  form.value = blankForm();
  void loadPg();
  show.value = true;
}
function openEdit(r: any) {
  editingId.value = r.id;
  form.value = {
    kind: r.kind,
    target: r.kind === 'sqlite' ? String(r.target) : r.target,
    schedule: r.schedule,
    hour: r.hour,
    minute: r.minute,
    keep: r.keep,
    enabled: !!r.enabled,
  };
  void loadPg();
  show.value = true;
}
async function submit() {
  if (submitting.value) return;
  // 切换类型会把 target 置空（pg 的库名不是应用名），而后端 update 对 target=null
  // 的语义是"沿用旧值"：不选目标就提交，任务会静默继续指向原来那个名字，
  // 列表上还显示着新类型 —— 必须在提交前把空目标拦下来。
  if (!form.value.target) {
    msg.error(form.value.kind === 'pg' ? '请选择要备份的数据库（或 all）' : '请选择要备份的应用');
    return;
  }
  submitting.value = true;
  try {
    if (editingId.value == null) {
      await api.createBackup(form.value);
      msg.success('已创建');
    } else {
      await api.updateBackup(editingId.value, form.value);
      msg.success('已保存');
    }
    show.value = false;
    reload();
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div>
    <CpToolbarCard title="备份">
      <template #left>
        <NButton v-if="isAdmin" size="small" type="primary" :icon="icon('AddOutline', 14)" @click="openCreate">新建备份任务</NButton>
        <NButton size="small" tertiary :icon="icon('FolderOpenOutline', 14)" @click="router.push('/backups/files')">备份文件</NButton>
      </template>
      <template #right>
        <NSelect v-model:value="autoSec" :options="AUTO_OPTS" size="small" style="width: 130px; max-width: 100%" aria-label="自动刷新间隔" />
        <NButton size="small" tertiary :loading="loading || refreshing" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
      </template>

      <NAlert type="info" :bordered="false" size="small" style="margin-bottom: var(--space-3)">
        产物在本页管理、在「备份文件」页统一下载/恢复/清理。调度由 systemd timer 独立完成，面板进程停止不影响备份执行。PG 用 pg_dump 自定义格式；应用目录打包自动排除 node_modules / .venv / .next / .output；SQLite 走在线备份 API，热备份不会写出页不一致的库。
      </NAlert>

      <NDataTable
        ref="tableAnchor"
        :columns="columns" :data="rows" size="small" :bordered="false" :loading="loading"
        :scroll-x="1230" :max-height="tableH" :row-key="(r: any) => r.id"
      >
        <template #empty>
          <EmptyBox :text="error ? '备份任务获取失败，点上方「刷新」重试' : (isAdmin ? '还没有备份任务，点左上方「新建备份任务」' : '还没有备份任务')" />
        </template>
      </NDataTable>
    </CpToolbarCard>

    <NDrawer v-model:show="show" :width="drawerW">
      <NDrawerContent :title="`${editingId == null ? '新建备份任务' : '编辑备份任务'} - ${form.target || ''}`" closable>
        <NForm label-placement="top">
          <NSpace vertical :size="12">
            <NFormItem label="类型">
              <NSpace vertical :size="2" style="width: 100%">
                <NSelect v-model:value="form.kind" :options="kindOpts" aria-label="备份类型" @update:value="form.target = null" />
                <span class="cp-help">{{ KIND_DESC[form.kind] }}</span>
              </NSpace>
            </NFormItem>
            <NFormItem v-if="form.kind === 'pg'" label="数据库">
              <NSpace vertical :size="2" style="width: 100%">
                <NSpace :size="8">
                  <!-- 下拉来自实例里真实存在的库；PG 没装或连不上时列表是空的，
                       tag+filterable 让用户直接键入，不至于把这条路径整个堵死。 -->
                  <NSelect
                    v-model:value="form.target"
                    :options="pgOpts"
                    filterable
                    tag
                    placeholder="选择数据库"
                    style="width: 230px; max-width: 100%"
                    aria-label="数据库名称"
                  />
                  <NButton size="small" tertiary @click="form.target = 'all'">全部</NButton>
                </NSpace>
                <span class="cp-help">填单个库名只备份该库；选 all 备份实例内全部库。库名只校验格式，之后库不存在的话这个任务每晚都会失败。</span>
              </NSpace>
            </NFormItem>
            <NFormItem v-else :label="form.kind === 'sqlite' ? 'SQLite 应用' : '应用'">
              <NSpace vertical :size="2" style="width: 100%">
                <NSelect v-model:value="form.target" :options="targetOptions" aria-label="备份应用" placeholder="选择应用" />
                <span v-if="form.kind === 'sqlite'" class="cp-help">自动选取应用目录下体积最大的 *.db / *.sqlite*（忽略 -wal / -shm），备份文件名会记下源库名，恢复时按它精确写回。</span>
              </NSpace>
            </NFormItem>
            <NFormItem label="计划">
              <NSpace vertical :size="2" style="width: 100%">
                <NSelect v-model:value="form.schedule" :options="schedOpts" aria-label="备份计划" />
                <span class="cp-help">定时计划渲染成 systemd timer，面板进程停止不影响执行；手动任务不挂 timer，只在列表里点「立即备份」。</span>
              </NSpace>
            </NFormItem>
            <NFormItem v-if="form.schedule !== 'manual'" label="执行时刻">
              <NSpace align="center" :size="8">
                <NInputNumber v-model:value="form.hour" :min="0" :max="23" :input-props="{ 'aria-label': '执行小时' }" style="width: 84px; max-width: 100%" /> 时
                <NInputNumber v-model:value="form.minute" :min="0" :max="59" :input-props="{ 'aria-label': '执行分钟' }" style="width: 84px; max-width: 100%" /> 分（本地时区）
              </NSpace>
            </NFormItem>
            <NSpace justify="space-between" align="center" style="width: 100%">
              <span id="bk-schedule-enabled" style="font-size: var(--fs-sm); color: var(--cp-text-dim)">启用定时计划</span>
              <NSwitch v-model:value="form.enabled" size="small" aria-labelledby="bk-schedule-enabled" />
            </NSpace>
            <NFormItem label="保留份数">
              <NSpace vertical :size="2">
                <NInputNumber v-model:value="form.keep" :min="1" :max="365" :input-props="{ 'aria-label': '保留备份份数' }" style="width: 130px; max-width: 100%" />
                <span class="cp-help">超出份数后由任务自动裁剪最旧的产物（按写入顺序删，同一秒生成的多份按序号排；不是按文件名的字母序）。</span>
              </NSpace>
            </NFormItem>
          </NSpace>
        </NForm>
        <template #footer>
          <NSpace justify="end" :size="12">
            <NButton size="small" :disabled="submitting" @click="show = false">取消</NButton>
            <NButton size="small" type="primary" class="cp-press" :loading="submitting" :disabled="submitting" :icon="icon(editingId == null ? 'AddOutline' : 'SaveOutline', 14)" @click="submit">确定</NButton>
          </NSpace>
        </template>
      </NDrawerContent>
    </NDrawer>

    <NDrawer v-model:show="logShow" :width="drawerW">
      <NDrawerContent :title="`执行日志 - ${logTitle}`" closable>
        <pre class="log-view log-drawer">{{ logText || '（无输出）' }}</pre>
      </NDrawerContent>
    </NDrawer>

    <ConfirmActionDialog ref="delDialog" />
  </div>
</template>
