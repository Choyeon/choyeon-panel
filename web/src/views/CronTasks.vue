<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  NAlert, NCard, NDataTable, NDrawer, NDrawerContent, NForm, NFormItem, NIcon, NInput, NModal, NSelect, NSpace, NSwitch, NTag, NText,
} from 'naive-ui';
import { NButton } from '../ui';
import { req } from '../api';
import { icon, icons } from '../icons';
import CpToolbarCard from '../components/CpToolbarCard.vue';
import CpRowActions from '../components/CpRowActions.vue';
import ConfirmActionDialog from '../components/ConfirmActionDialog.vue';
import DescriptionsCard from '../components/DescriptionsCard.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { invalidateCache, useCachedFetch } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';
import { fmtDateTime } from '../composables/useFormat';

type Task = {
  id: number; name: string; expr: string; enabled: number; last_run: string; last_status: string;
  log_tail: string; cmd: string; human: string; expr_error: boolean; timer: string;
  timer_active: boolean; timer_next: string; timer_unit_state: string; argv_list: string[];
};
type Options = { allowed: string[]; presets: any[]; units: string[]; note: string };
type Preview = { expr: string; human: string; oncalendar: string | null; error: string | null };

const err = ref('');
const runningId = ref<number | null>(null);
const saving = ref(false);
const preview = ref<Preview | null>(null);
const options = ref<Options>({ allowed: [], presets: [], units: [], note: '' });
const showForm = ref(false);
const editing = ref<Task | null>(null);
const logRow = ref<Task | null>(null);
const form = ref<{ name: string; expr: string; preset: number; unit: string; args: string; enabled: boolean }>({
  name: '', expr: '', preset: 0, unit: '', args: '', enabled: true,
});

// 列表与白名单都取一次就够（白名单只在打开表单时才用得上），但写操作后必须作废，
// 否则新建的任务要等 maxAge 才出现，人会以为没保存上。
const { data, loading, busy, refresh } = useCachedFetch<{ items: Task[] }>({
  key: () => 'cron:list',
  loader: () => req('/cron'),
  maxAge: 20000,
});
const { load: loadOptions } = useCachedFetch<Options>({
  key: () => 'cron:options',
  loader: () => req('/cron/options'),
  maxAge: 300000,
  immediate: false,
});

const rows = computed<Task[]>(() => data.value?.items ?? []);
const presetOpts = computed(() => (options.value.presets || []).map((p, i) => ({ label: p.label, value: i })));
const unitOpts = computed(() => (options.value.units || []).map((u) => ({ label: u, value: u })));
const needUnit = computed(() => !!options.value.presets?.[form.value.preset]?.needs_unit);

const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

async function reload() {
  invalidateCache('cron:list');
  return refresh().catch((e: any) => { err.value = e.message || String(e); });
}

// 1Panel TableSetting 自动刷新：0/5/10/30/60/120/300s，localStorage 记忆
const AUTO_OPTS = [0, 5, 10, 30, 60, 120, 300].map((s) => ({ label: s ? `${s} 秒` : '关闭自动刷新', value: s }));
const autoSec = ref(Math.max(0, Number(localStorage.getItem('cron-auto-refresh')) || 0));
let autoTimer: number | null = null;
function restartAuto() {
  if (autoTimer) clearInterval(autoTimer);
  autoTimer = autoSec.value > 0 ? window.setInterval(() => void reload(), autoSec.value * 1000) : null;
}
watch(autoSec, (v) => { localStorage.setItem('cron-auto-refresh', String(v)); restartAuto(); });
onMounted(restartAuto);
onBeforeUnmount(() => { if (autoTimer) clearInterval(autoTimer); });

async function openCreate() {
  editing.value = null;
  form.value = { name: '', expr: '0 4 * * *', preset: 0, unit: '', args: '', enabled: true };
  preview.value = null;
  showForm.value = true;
  err.value = '';
  await ensureOptions();
  runPreview();
}

async function ensureOptions() {
  if (options.value.presets?.length) return;
  try {
    options.value = (await loadOptions(true)) as Options;
  } catch (e: any) {
    err.value = e.message || String(e);
  }
}

// 回填时按"预设自带的固定前缀"切分：前缀之外先是 unit（需要 unit 的预设），再是附加参数。
// 对不上任何预设（历史数据、或后端白名单收紧后残留）就退回第一个预设，把整条命令
// 原样放进参数框，让用户看得见真实要执行的内容而不是被静默改写。
async function openEdit(r: Task) {
  await ensureOptions();
  const argv = r.argv_list || [];
  const presets = options.value.presets || [];
  const idx = presets.findIndex((p: any) => (p.argv || []).join(' ') === argv.slice(0, (p.argv || []).length).join(' '));
  editing.value = r;
  showForm.value = true;
  preview.value = null;
  if (idx < 0) {
    form.value = { name: r.name, expr: r.expr, preset: 0, unit: '', args: argv.join(' '), enabled: !!r.enabled };
    err.value = '这条任务的命令不在当前白名单预设里，参数框里保留的是原命令，保存前请确认。';
  } else {
    const base = (presets[idx].argv || []).length;
    form.value = {
      name: r.name,
      expr: r.expr,
      preset: idx,
      unit: presets[idx].needs_unit ? argv[base] || '' : '',
      args: argv.slice(presets[idx].needs_unit ? base + 1 : base).join(' '),
      enabled: !!r.enabled,
    };
  }
  runPreview();
}

// 表达式回显是人话而不是 OnCalendar：写 cron 的人想确认的是「每周一 4 点」，
// 而不是再去查一遍 systemd 的日历语法。
let previewTimer: number | null = null;
function runPreview() {
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = window.setTimeout(async () => {
    if (!form.value.expr.trim()) { preview.value = null; return; }
    try {
      preview.value = await req(`/cron/preview?expr=${encodeURIComponent(form.value.expr)}`);
    } catch (e: any) {
      preview.value = { expr: form.value.expr, human: '', oncalendar: null, error: e.message || String(e) };
    }
  }, 350);
}
watch(() => form.value.expr, runPreview);
onBeforeUnmount(() => { if (previewTimer) clearTimeout(previewTimer); });

function buildArgv(): string[] {
  const p = options.value.presets?.[form.value.preset] || { argv: [] };
  const base = [...(p.argv || [])];
  if (p.needs_unit) {
    if (form.value.unit) base.push(form.value.unit);
  }
  const extra = form.value.args.trim();
  if (extra) base.push(...extra.split(/\s+/));
  return base;
}

async function save() {
  saving.value = true;
  err.value = '';
  const body: any = {
    name: form.value.name.trim(),
    expr: form.value.expr.trim(),
    argv: buildArgv(),
    enabled: form.value.enabled,
  };
  try {
    if (editing.value) await req(`/cron/${editing.value.id}`, { method: 'PATCH', body, timeout: 60000 });
    else await req('/cron', { body, timeout: 60000 });
    showForm.value = false;
    await reload();
  } catch (e: any) {
    err.value = e.message || String(e);
  } finally {
    saving.value = false;
  }
}

async function runNow(r: Task) {
  runningId.value = r.id;
  err.value = '';
  try {
    const res = await req(`/cron/${r.id}/run`, { body: {}, timeout: 660000 });
    logRow.value = { ...r, last_status: res?.status || '', log_tail: res?.log || '' };
    await reload();
  } catch (e: any) {
    err.value = e.message || String(e);
  } finally {
    runningId.value = null;
  }
}

async function toggle(r: Task, val: boolean) {
  err.value = '';
  try {
    await req(`/cron/${r.id}`, { method: 'PATCH', body: { enabled: val }, timeout: 60000 });
    await reload();
  } catch (e: any) {
    err.value = e.message || String(e);
    await reload();
  }
}

const delDialog = ref<InstanceType<typeof ConfirmActionDialog> | null>(null);
function confirmDel(r: Task) {
  delDialog.value?.acceptParams({
    title: '确认删除定时任务',
    names: [r.name],
    msg: `删除后 ${r.timer} 会一起从 systemd 摘掉，操作不可撤销。`,
    api: async () => {
      await req(`/cron/${r.id}`, { method: 'DELETE', timeout: 60000 });
      await reload();
    },
    successMsg: '已删除',
  });
}

function statusTag(r: Task) {
  const s = r.last_status || '';
  if (!s) return h('span', { class: 'cell-sub' }, '未执行');
  const type = s === 'ok' ? 'success' : s === 'timeout' ? 'error' : 'warning';
  return h(NTag, { size: 'tiny', bordered: false, round: true, type: type as any }, { default: () => s });
}

// 1Panel cronjob 上次执行列：状态图标 + 绝对时间（无相对时间）
function lastRunCell(r: Task) {
  const s = r.last_status || '';
  const icoName = !s ? 'InformationCircleOutline' : s === 'ok' ? 'CheckmarkCircleOutline' : 'CloseCircleOutline';
  const color = !s ? 'var(--cp-text-mute)' : s === 'ok' ? 'var(--cp-node)' : 'var(--cp-err)';
  return h('div', { class: 'cell-lastrun' }, [
    h(NIcon, { component: icons[icoName], size: 14, color }),
    h('span', { class: 'mono-dim', title: r.last_run ? `${r.last_run} (本地)` : '' }, r.last_run ? fmtDateTime(r.last_run) : '—'),
  ]);
}

const cols: any[] = [
  {
    title: '任务', key: 'name', minWidth: 170,
    render: (r: Task) => h('div', { class: 'cell-stack' }, [
      h('span', { class: 'cell-name', title: r.name }, r.name),
      h('span', { class: 'cell-sub mono-dim cp-break' }, r.timer),
    ]),
  },
  {
    title: '计划', key: 'expr', minWidth: 210,
    render: (r: Task) => h('div', { class: 'cell-stack' }, [
      r.expr_error
        ? h(NTag, { size: 'tiny', bordered: false, round: true, type: 'error' }, { default: () => '表达式无法解析' })
        : h('span', { class: 'cell-name', title: `${r.expr} → ${r.human}` }, r.human || r.expr),
      h('span', { class: 'cell-sub mono-dim cp-break' }, r.expr),
    ]),
  },
  {
    title: '命令', key: 'cmd', minWidth: 240,
    // 命令行可能很长：这里必须 word-break，否则整列把表格撑出横向滚动，
    // 后面的状态列就看不见（这一列的文字本身也是运维信息，不做 tooltip 隐藏）。
    render: (r: Task) => h('span', { class: 'mono-dim cp-break', style: { fontSize: 'var(--fs-xs)' } }, r.cmd || '—'),
  },
  {
    // 1Panel Status operate 模式：启用/停用是可点的圆角小按钮，点击直发 PATCH
    title: '启用', key: 'enabled', width: 90,
    render: (r: Task) => h(NButton, {
      size: 'tiny', round: true, tertiary: true, type: r.enabled ? 'success' : 'info',
      disabled: runningId.value === r.id, onClick: () => toggle(r, !r.enabled),
    }, { default: () => (r.enabled ? '启用' : '停用') }),
  },
  {
    title: 'Timer', key: 'timer', width: 116,
    render: (r: Task) => h('span', { class: 'st ' + (r.timer_active ? 'run' : 'idle'), title: r.timer_next ? `下次：${r.timer_next}` : '下次执行时间未取到' }, [
      h('span', { class: 'dot ' + (r.timer_active ? 'run' : 'idle') }),
      r.timer_active ? '已挂表' : (r.enabled ? '未挂表' : '已停用'),
    ]),
  },
  { title: '上次执行', key: 'last_run', width: 180, render: lastRunCell },
  { title: '结果', key: 'last_status', width: 96, render: statusTag },
  {
    title: '操作', key: 'ops', width: 200,
    render: (r: Task) => h(CpRowActions, {
      ellipsis: 3,
      buttons: [
        { label: runningId.value === r.id ? '执行中…' : '执行', run: () => runNow(r), disabled: runningId.value !== null },
        { label: '输出', run: () => { logRow.value = r; }, disabled: !r.log_tail && !r.last_run },
        { label: '编辑', run: () => openEdit(r) },
        { label: '删除', danger: true, run: () => confirmDel(r) },
      ],
    }),
  },
];
</script>

<template>
  <div>
    <NAlert v-if="err" type="error" :bordered="false" size="small" role="alert" style="margin-bottom: var(--space-3)">{{ err }}</NAlert>

    <CpToolbarCard title="定时任务">
      <template #left>
        <NButton size="small" type="primary" :icon="icon('AddOutline', 14)" @click="openCreate">新建任务</NButton>
      </template>
      <template #right>
        <NSelect v-model:value="autoSec" :options="AUTO_OPTS" size="small" style="width: 130px" aria-label="自动刷新间隔" />
        <NButton size="small" tertiary :loading="busy" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
      </template>

      <NAlert type="info" :bordered="false" size="small" style="margin-bottom: var(--space-3)">
        {{ options.note || '定时任务以 root 直接执行固定参数，不经过 shell；不在白名单内的命令不会被保存。' }}
        表达式渲染成 systemd timer，由 systemd 按计划执行；与备份任务的 timer 互不影响：备份仍走「备份」页的 keep 自动裁剪。
      </NAlert>

      <NDataTable
        ref="tableAnchor"
        :columns="cols" :data="rows" size="small" :bordered="false" :loading="loading"
        :scroll-x="1220" :max-height="tableH"
        :row-key="(r: Task) => r.id"
      >
        <template #empty><EmptyBox text="还没有定时任务，点「新建任务」加一条" /></template>
      </NDataTable>
    </CpToolbarCard>

    <NModal v-model:show="showForm" preset="card" :title="editing ? `编辑任务：${editing.name}` : '新建定时任务'" style="width: 640px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NForm label-placement="top" :show-feedback="false">
          <NCard size="small" title="基本信息" :bordered="true">
            <NSpace :size="12" align="start" style="width: 100%">
              <NFormItem label="任务名" style="flex: 1 1 220px; min-width: 0">
                <NSpace vertical :size="2" style="width: 100%">
                  <NInput v-model:value="form.name" :disabled="!!editing" placeholder="small-dns-kebab" maxlength="39" />
                  <span class="cp-help">任务名保存后不可修改。</span>
                </NSpace>
              </NFormItem>
              <NFormItem label="启用" style="flex: 0 0 90px">
                <NSwitch v-model:value="form.enabled" size="small" />
              </NFormItem>
            </NSpace>
          </NCard>
          <NCard size="small" title="计划" :bordered="true">
            <NFormItem label="计划表达式（分 时 日 月 周）">
              <NSpace vertical :size="4" style="width: 100%">
                <NInput v-model:value="form.expr" placeholder="0 4 * * 1" maxlength="80" />
                <span class="cp-help">支持 *、数字、a-b、列表与 /步长；星期 0=周日，不支持 @daily 这类宏。</span>
              </NSpace>
            </NFormItem>
            <div class="cr-preview" role="status">
              <template v-if="preview?.error">
                <NText type="error" style="font-size: var(--fs-xs)">{{ preview.error }}</NText>
              </template>
              <template v-else-if="preview">
                <NText style="font-size: var(--fs-sm)">{{ preview.human }}</NText>
                <NText class="mono-dim cp-break" style="font-size: var(--fs-xs)">OnCalendar: {{ preview.oncalendar || '—' }}</NText>
              </template>
              <NText v-else class="cell-sub" style="font-size: var(--fs-xs)">等待有效的计划表达式…</NText>
            </div>
          </NCard>
          <NCard size="small" title="命令" :bordered="true">
            <NFormItem label="白名单预设">
              <NSpace vertical :size="2" style="width: 100%">
                <NSelect
                  v-model:value="form.preset" :options="presetOpts"
                  :disabled="!presetOpts.length" placeholder="正在读取白名单…"
                />
                <span class="cp-help">白名单在服务端逐条校验（命令名、子命令、参数个数与取值范围），不合格会被拒绝且不会保存。</span>
              </NSpace>
            </NFormItem>
            <NFormItem v-if="needUnit" label="要重启的 unit">
              <NSelect v-model:value="form.unit" :options="unitOpts" filterable placeholder="只能选面板纳管的 unit" />
            </NFormItem>
            <NFormItem label="附加参数（可留空）">
              <NSpace vertical :size="2" style="width: 100%">
                <NInput v-model:value="form.args" placeholder="例如 --lines=500" maxlength="200" />
                <span class="cp-help">按空格拆分为多个参数。</span>
              </NSpace>
            </NFormItem>
            <span class="cp-help">将执行：<span class="mono-dim">{{ buildArgv().join(' ') || '（命令不完整）' }}</span></span>
          </NCard>
        </NForm>
        <NSpace justify="end" :size="8">
          <NButton tertiary :disabled="saving" @click="showForm = false">返回</NButton>
          <NButton
            type="primary" class="cp-press" :loading="saving"
            :disabled="saving || !form.name.trim() || !form.expr.trim() || (!!needUnit && !form.unit)"
            @click="save"
          >确定</NButton>
        </NSpace>
      </NSpace>
    </NModal>

    <NDrawer :show="!!logRow" :width="'50%'" @update:show="(v: boolean) => { if (!v) logRow = null; }">
      <NDrawerContent :title="`最近一次输出 - ${logRow?.name || ''}`" closable>
        <NSpace vertical :size="10" style="width: 100%">
          <DescriptionsCard
            :items="[
              { label: '开始时间', value: logRow?.last_run ? fmtDateTime(logRow.last_run) : '未执行' },
              { label: '状态', value: logRow?.last_status || '未执行' },
            ]"
          />
          <NAlert type="warning" :bordered="false" size="small">服务端已对口令/连接串做脱敏，并且只保留最后 4KB。</NAlert>
          <pre class="cr-log mono">{{ logRow?.log_tail || '（无输出）' }}</pre>
        </NSpace>
      </NDrawerContent>
    </NDrawer>

    <ConfirmActionDialog ref="delDialog" />
  </div>
</template>

<style scoped>
.cell-lastrun { display: flex; align-items: center; gap: 6px; min-width: 0; }
.cr-preview {
  display: flex; flex-direction: column; gap: 4px; min-height: 44px;
  padding: var(--space-3); border: 1px solid var(--cp-border); border-radius: 8px;
  background: var(--cp-code-bg);
}
/* 1Panel LogFile：calc(100vh - 420px)、14px/16px、padding 5px、深色代码底 */
.cr-log {
  margin: 0; height: calc(100vh - 420px); overflow: auto; padding: 5px;
  background: var(--cp-code-bg); color: var(--cp-code-text);
  border: 1px solid var(--cp-border); border-radius: 8px;
  font-size: 14px; line-height: 16px; white-space: pre-wrap; word-break: break-all;
}
</style>
