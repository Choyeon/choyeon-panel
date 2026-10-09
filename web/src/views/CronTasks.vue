<script setup lang="ts">
import { computed, h, onBeforeUnmount, ref, watch } from 'vue';
import {
  NAlert, NDataTable, NForm, NFormItem, NInput, NModal, NPopconfirm, NSelect, NSpace, NSwitch, NTag, NText,
} from 'naive-ui';
import { NButton } from '../ui';
import { req } from '../api';
import { icon } from '../icons';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { invalidateCache, useCachedFetch } from '../composables/useCachedFetch';
import { useTableHeight } from '../composables/useTableHeight';
import { fmtAgo, fmtDateTime } from '../composables/useFormat';

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

async function remove(r: Task) {
  err.value = '';
  try {
    await req(`/cron/${r.id}`, { method: 'DELETE', timeout: 60000 });
    await reload();
  } catch (e: any) {
    err.value = e.message || String(e);
  }
}

function statusTag(r: Task) {
  const s = r.last_status || '';
  if (!s) return h('span', { class: 'cell-sub' }, '未执行');
  const type = s === 'ok' ? 'success' : s === 'timeout' ? 'error' : 'warning';
  return h(NTag, { size: 'tiny', bordered: false, type: type as any }, { default: () => s });
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
        ? h(NTag, { size: 'tiny', bordered: false, type: 'error' }, { default: () => '表达式无法解析' })
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
    title: '启用', key: 'enabled', width: 76,
    render: (r: Task) => h(NSwitch, {
      size: 'small', value: !!r.enabled, disabled: runningId.value === r.id,
      'onUpdate:value': (v: boolean) => toggle(r, v),
    }),
  },
  {
    title: 'Timer', key: 'timer', width: 116,
    render: (r: Task) => h('span', { class: 'st ' + (r.timer_active ? 'run' : 'idle'), title: r.timer_next ? `下次：${r.timer_next}` : '下次执行时间未取到' }, [
      h('span', { class: 'dot ' + (r.timer_active ? 'run' : 'idle') }),
      r.timer_active ? '已挂表' : (r.enabled ? '未挂表' : '已停用'),
    ]),
  },
  {
    title: '上次执行', key: 'last_run', width: 132,
    render: (r: Task) => h('span', { class: 'mono-dim', title: r.last_run ? `${r.last_run} (本地)` : '' }, r.last_run ? fmtAgo(r.last_run) : '—'),
  },
  { title: '结果', key: 'last_status', width: 96, render: statusTag },
  {
    title: '操作', key: 'ops', width: 236,
    render: (r: Task) => h(NSpace, { size: 6, wrap: false }, {
      default: () => [
        h(NButton, {
          size: 'tiny', tertiary: true, type: 'primary', loading: runningId.value === r.id,
          disabled: runningId.value !== null, icon: icon('PlayOutline', 12), onClick: () => runNow(r),
        }, () => '立即执行'),
        h(NButton, {
          size: 'tiny', tertiary: true, disabled: !r.log_tail && !r.last_run,
          icon: icon('DocumentTextOutline', 12), onClick: () => { logRow.value = r; },
        }, () => '输出'),
        h(NButton, {
          size: 'tiny', tertiary: true, icon: icon('PencilOutline', 12), onClick: () => openEdit(r),
        }, () => '编辑'),
        h(NPopconfirm, { onPositiveClick: () => remove(r) }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true, type: 'error', icon: icon('TrashOutline', 12), 'aria-label': `删除任务 ${r.name}` }),
          default: () => `删除后 ${r.timer} 会一起从 systemd 摘掉，确认？`,
        }),
      ],
    }),
  },
];
</script>

<template>
  <div>
    <PageHeader title="定时任务" sub="表达式渲染成 systemd timer，由 systemd 按计划执行；命令只认白名单，且不经 shell">
      <template #actions>
        <NButton size="small" tertiary :loading="busy" :icon="icon('RefreshOutline', 14)" @click="reload">刷新</NButton>
        <NButton size="small" type="primary" :icon="icon('AddOutline', 14)" @click="openCreate">新建任务</NButton>
      </template>
    </PageHeader>

    <NAlert v-if="err" type="error" :bordered="false" size="small" role="alert" style="margin-bottom: var(--space-3)">{{ err }}</NAlert>

    <NAlert type="info" :bordered="false" size="small" style="margin-bottom: var(--space-4)">
      {{ options.note || '定时任务以 root 直接执行固定参数，不经过 shell；不在白名单内的命令不会被保存。' }}
      与备份任务的 timer 互不影响：备份仍走「备份」页的 keep 自动裁剪。
    </NAlert>

    <NDataTable
      ref="tableAnchor"
      :columns="cols" :data="rows" size="small" :bordered="false" :loading="loading"
      :scroll-x="1180" class="cp-tbl tbl-cron" :max-height="tableH"
      :row-key="(r: Task) => r.id"
    >
      <template #empty><EmptyBox text="还没有定时任务，点「新建任务」加一条" /></template>
    </NDataTable>

    <NModal v-model:show="showForm" preset="card" :title="editing ? `编辑任务：${editing.name}` : '新建定时任务'" style="width: 640px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NForm label-placement="top" :show-feedback="false">
          <NSpace :size="12" align="start" style="width: 100%">
            <NFormItem label="任务名" style="flex: 1 1 220px; min-width: 0">
              <NInput v-model:value="form.name" :disabled="!!editing" placeholder="small-dns-kebab" maxlength="39" />
            </NFormItem>
            <NFormItem label="启用" style="flex: 0 0 90px">
              <NSwitch v-model:value="form.enabled" size="small" />
            </NFormItem>
          </NSpace>
          <NFormItem label="计划表达式（分 时 日 月 周）">
            <NInput v-model:value="form.expr" placeholder="0 4 * * 1" maxlength="80" />
          </NFormItem>
          <div class="cr-preview" role="status">
            <template v-if="preview?.error">
              <NText type="error" style="font-size: var(--fs-xs)">{{ preview.error }}</NText>
            </template>
            <template v-else-if="preview">
              <NText style="font-size: var(--fs-sm)">{{ preview.human }}</NText>
              <NText class="mono-dim cp-break" style="font-size: var(--fs-xs)">OnCalendar: {{ preview.oncalendar || '—' }}</NText>
            </template>
            <NText v-else class="cell-sub" style="font-size: var(--fs-xs)">支持 *、数字、a-b、列表与 /步长；星期 0=周日，不支持 @daily 这类宏。</NText>
          </div>
          <NFormItem label="命令">
            <NSelect
              v-model:value="form.preset" :options="presetOpts"
              :disabled="!presetOpts.length" placeholder="正在读取白名单…"
            />
          </NFormItem>
          <NFormItem v-if="needUnit" label="要重启的 unit">
            <NSelect v-model:value="form.unit" :options="unitOpts" filterable placeholder="只能选面板纳管的 unit" />
          </NFormItem>
          <NFormItem label="附加参数（可留空）">
            <NInput v-model:value="form.args" placeholder="例如 --lines=500" maxlength="200" />
          </NFormItem>
          <NText class="cell-sub" style="font-size: var(--fs-xs); line-height: 1.6">
            将执行：<span class="mono-dim">{{ buildArgv().join(' ') || '（命令不完整）' }}</span>
            。白名单在服务端逐条校验（命令名、子命令、参数个数与取值范围），不合格会被拒绝且不会保存。
          </NText>
        </NForm>
        <NSpace justify="end" :size="8">
          <NButton tertiary :disabled="saving" @click="showForm = false">取消</NButton>
          <NButton
            type="primary" class="cp-press" :loading="saving"
            :disabled="saving || !form.name.trim() || !form.expr.trim() || (!!needUnit && !form.unit)"
            @click="save"
          >{{ editing ? '保存修改' : '创建任务' }}</NButton>
        </NSpace>
      </NSpace>
    </NModal>

    <NModal
      :show="!!logRow" preset="card" title="最近一次输出" style="width: 720px; max-width: 94vw"
      @update:show="(v: boolean) => { if (!v) logRow = null; }"
    >
      <NSpace vertical :size="10">
        <NText class="cell-sub" style="font-size: var(--fs-xs)">
          {{ logRow?.name }} · {{ logRow?.last_run ? fmtDateTime(logRow.last_run) : '尚未执行' }} · 结果 {{ logRow?.last_status || '—' }}
        </NText>
        <NAlert type="warning" :bordered="false" size="small">服务端已对口令/连接串做脱敏，并且只保留最后 4KB。</NAlert>
        <pre class="cr-log mono">{{ logRow?.log_tail || '（无输出）' }}</pre>
      </NSpace>
    </NModal>
  </div>
</template>

<style scoped>
.cr-preview {
  display: flex; flex-direction: column; gap: 4px; min-height: 44px;
  padding: var(--space-3); border: 1px solid var(--cp-border); border-radius: 8px;
  background: var(--cp-code-bg);
}
.cr-log {
  margin: 0; max-height: 46vh; overflow: auto; padding: var(--space-3);
  background: var(--cp-code-bg); color: var(--cp-code-text);
  border: 1px solid var(--cp-border); border-radius: 8px;
  font-size: var(--fs-2xs); line-height: 1.6; white-space: pre-wrap; word-break: break-all;
}
</style>
