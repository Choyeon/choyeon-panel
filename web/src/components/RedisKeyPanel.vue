<script setup lang="ts">
import { computed, h, ref, watch } from 'vue';
import { NCard, NSpace, NTag, NInput, NPopconfirm, NText, NDataTable, useMessage } from 'naive-ui';
import { NButton } from '../ui';
import { api, getRole } from '../api';
import { icon } from '../icons';
import JsonView from './JsonView.vue';
import EmptyBox from './EmptyBox.vue';

/**
 * Redis key 详情面板：查看 + 在线修改。
 * string 改值、hash/list/set/zset 按条目增删、TTL 单独编辑。
 * 每次写成功后让父级重拉一次详情，TTL/内存/长度这些派生值才不会是旧的。
 */
const props = defineProps<{ detail: any }>();
// 只读账号看 key 的内容，但改值/增删条目/改 TTL/删 key 在服务端一律拦 403，这里不摆入口。
const isAdmin = computed(() => getRole() === 'admin');
const emit = defineEmits<{ refresh: []; deleted: [k: string] }>();

const editing = ref(false);
const draft = ref('');
const busy = ref(false);
const ttlDraft = ref('');
const addForm = ref({ field: '', value: '', score: '' });
const editField = ref('');
const editValue = ref('');

const type = computed(() => String(props.detail?.type || ''));
const isMember = computed(() => ['hash', 'list', 'set', 'zset'].includes(type.value));
const editIco = icon('PencilOutline', 14);
const saveIco = icon('SaveOutline', 14);
const addIco = icon('AddOutline', 14);
const trashIco = icon('TrashOutline', 14);
const refreshIco = icon('RefreshOutline', 14);

// 只有没截断的 string 才尝试按 JSON 解析成树：截断后的文本必然解析失败
const parsed = computed(() => {
  if (type.value !== 'string' || props.detail?.truncated) return null;
  const v = props.detail?.value;
  if (typeof v !== 'string' || !/^\s*[[{]/.test(v)) return null;
  try {
    return JSON.parse(v);
  } catch {
    return null;
  }
});

// 非 UTF-8 的 value（序列化对象、gzip、msgpack）在后端是 errors="replace" 解码的，
// 页面上看到的文本已经不等于原值。基于它点保存会把替换符写回 Redis，
// 原值就此损坏且不可恢复——和截断同理，一律不开放编辑。
const LOSSY = "\uFFFD";
const hasLossy = (v: any) => typeof v === "string" && v.includes(LOSSY);
const lossy = computed(() => type.value === "string" && hasLossy(props.detail?.value));

const memberData = computed<any[]>(() => {
  const v = props.detail?.value;
  if (!Array.isArray(v)) return [];
  if (type.value === 'hash') return v.map((p: any) => ({ ...p }));
  // zset 后端按 ZRANGE WITHSCORES 的顺序回 {field: member, value: score}，
  // 而下面列定义用 v=member、field=score，这里统一转一次。
  if (type.value === 'zset') return v.map((p: any) => ({ v: p.field, field: p.value }));
  return v.map((x: any, i: number) => ({ i, v: x }));
});

const memberCols = computed<any[]>(() => {
  const ops = (render: (r: any) => any) => ({ title: '操作', key: 'ops', width: 116, className: 'row-ops', render, fixed: 'right' as const });
  const cols: any[] = [];
  if (type.value === 'hash') {
    cols.push({ title: 'field', key: 'field', width: 140, ellipsis: { tooltip: true }, render: (r: any) => hText(r.field) });
    cols.push({
      title: 'value', key: 'value', minWidth: 240, ellipsis: { tooltip: true },
      render: (r: any) =>
        editField.value === r.field
          ? h(NInput, {
            value: editValue.value, size: 'tiny',
            'input-props': { 'aria-label': `字段 ${r.field} 的新值` },
            'onUpdate:value': (v: string) => { editValue.value = v; },
          })
          : hText(String(r.value ?? '')),
    });
    if (isAdmin.value) cols.push(ops((r: any) =>
      editField.value === r.field
        ? h('div', { class: 'cp-ops' }, [
            h(NButton, { size: 'tiny', text: true, type: 'primary', class: 'cp-op', onClick: () => saveField(r) }, { default: () => '保存' }),
            h(NButton, { size: 'tiny', text: true, class: 'cp-op', onClick: () => { editField.value = ''; } }, { default: () => '取消' }),
          ])
        : h('div', { class: 'cp-ops' }, [
            h(NButton, { size: 'tiny', text: true, class: 'cp-op', onClick: () => startFieldEdit(r) }, { default: () => '编辑' }),
            h(NPopconfirm, { onPositiveClick: () => delMember('', r.field) }, {
              trigger: () => h(NButton, { size: 'tiny', text: true, type: 'error', class: 'cp-op' }, { default: () => '删除' }),
              default: () => `HDEL ${r.field}？`,
            }),
          ])));
    return cols;
  }
  if (type.value === 'list') cols.push({ title: '#', key: 'i', width: 56 });
  cols.push({
    title: type.value === 'zset' ? 'member' : 'value', key: 'v', ellipsis: { tooltip: true },
    render: (r: any) => hText(String(r.v ?? '')),
  });
  if (type.value === 'zset') cols.push({ title: 'score', key: 'field', width: 90 });
  if (isAdmin.value) cols.push(ops((r: any) =>
    h(NPopconfirm, { onPositiveClick: () => delMember(String(r.v), '') }, {
      trigger: () => h(NButton, { size: 'tiny', text: true, type: 'error', class: 'cp-op' }, { default: () => '删除' }),
      default: () => `从 ${type.value} 中删除该条目？`,
    })));
  return cols;
});

function hText(s: string) {
  // 单元格里是 nowrap + ellipsis（列宽所限），全文靠 ellipsis:{tooltip:true} 的浮层看
  return h('span', { class: 'mono-dim', title: s.slice(0, 500) }, s.slice(0, 500));
}

watch(() => props.detail?.key, () => {
  editing.value = false;
  editField.value = '';
  ttlDraft.value = '';
  addForm.value = { field: '', value: '', score: '' };
});

const msg = useMessage();

async function act(fn: () => Promise<any>, ok: string | ((r: any) => string)) {
  busy.value = true;
  try {
    const r = await fn();
    msg.success(typeof ok === 'function' ? ok(r) : ok);
    emit('refresh');
    return true;
  } catch (e: any) {
    msg.error(e?.message || String(e));
    return false;
  } finally {
    busy.value = false;
  }
}

function startEdit() {
  draft.value = typeof props.detail?.value === 'string' ? props.detail.value : '';
  editing.value = true;
}
async function saveString() {
  if (await act(() => api.redisSet(props.detail.key, draft.value), '已保存值，TTL 保持不变')) editing.value = false;
}
function startFieldEdit(r: any) {
  if (hasLossy(r.value)) {
    msg.warning('该字段含非 UTF-8 字节，页面显示的不是原值，编辑保存会写坏数据，已禁止');
    return;
  }
  editField.value = r.field;
  editValue.value = String(r.value ?? '');
}
async function saveField(r: any) {
  if (await act(() => api.redisMember({ key: props.detail.key, type: 'hash', op: 'add', field: r.field, value: editValue.value }), '已更新该字段')) editField.value = '';
}
async function delMember(value: string, field: string) {
  // list 走 LREM key 0 <value>：它删掉的是**所有**相同条目，只报"已删除该条目"
  // 会让人以为列表只少了一行，实际可能少了好几行。
  await act(
    () => api.redisMember({ key: props.detail.key, type: type.value, op: 'del', value, field }),
    (r) => (type.value === 'list' && Number(r?.n || 1) > 1 ? `已删除 ${r.n} 个相同条目` : '已删除该条目'),
  );
}
async function addMember() {
  const f = addForm.value;
  if (type.value === 'hash' && !f.field) return msg.error('请填写 field');
  if (type.value !== 'hash' && !f.value) return msg.error('请填写要新增的值');
  if (await act(() => api.redisMember({ key: props.detail.key, type: type.value, op: 'add', field: f.field, value: f.value, score: f.score }), '已新增条目')) {
    addForm.value = { field: '', value: '', score: '' };
  }
}
async function saveTtl() {
  const t = Number(ttlDraft.value.trim());
  if (!Number.isInteger(t) || t < -1) return msg.error('TTL 需为整数秒，-1 表示不过期');
  await act(() => api.redisTtl(props.detail.key, t), t === -1 ? '已设为不过期' : `已设置为 ${t}s 后过期`);
}
</script>

<template>
  <NCard size="small" class="rk-detail" :title="detail.key">
    <template #header-extra>
      <NSpace :size="6" align="center">
        <NTag size="tiny" :bordered="false" type="warning">{{ detail.type }}</NTag>
        <NTag size="tiny" :bordered="false">{{ detail.ttl === -1 ? '永不过期' : (detail.ttl === -2 || detail.ttl == null ? '—' : `${detail.ttl}s 剩余`) }}</NTag>
        <NTag size="tiny" :bordered="false">{{ detail.mem == null ? '—' : `${detail.mem} B` }}</NTag>
        <NButton size="tiny" text :loading="busy" title="重新读取该 key" aria-label="重新读取该 key" :icon="refreshIco" @click="emit('refresh')" />
      </NSpace>
    </template>

    <NSpace vertical :size="10">
      <NText v-if="detail.truncated" depth="3" style="font-size: var(--fs-xs)">内容过长，仅读取前 64KB（截断）；截断状态下不开放编辑，保存会把值覆盖成截断后的内容</NText>
      <NText v-else-if="lossy" depth="3" style="font-size: var(--fs-xs)">该值是二进制/非 UTF-8 内容，页面显示的文本已含替换符、不等于原值，因此不开放编辑：保存会把这份文本当成新值写回，原数据不可恢复</NText>

      <template v-if="type === 'string'">
        <NInput
          v-if="editing" v-model:value="draft" type="textarea" :autosize="{ minRows: 4, maxRows: 16 }"
          :input-props="{ class: 'mono', 'aria-label': '该 key 的新值' }"
        />
        <JsonView v-else-if="parsed !== null" :value="parsed" />
        <pre v-else class="mono-pre">{{ detail.value }}</pre>
        <NSpace v-if="isAdmin" justify="end" :size="8">
          <template v-if="editing">
            <NButton size="tiny" tertiary :disabled="busy" @click="editing = false">取消</NButton>
            <NButton size="tiny" type="primary" class="cp-press" :loading="busy" :icon="saveIco" @click="saveString">保存值</NButton>
          </template>
          <NButton v-else-if="!detail.truncated && !lossy" size="tiny" tertiary :icon="editIco" @click="startEdit">编辑值</NButton>
        </NSpace>
      </template>

      <template v-else-if="isMember">
        <NDataTable
          size="small" :bordered="false" :max-height="300" :scroll-x="520"
          :columns="memberCols" :data="memberData" :row-key="(r: any) => `${r.i ?? ''}|${r.field ?? ''}|${r.v ?? ''}`"
        >
          <template #empty><EmptyBox text="这个 key 里还没有条目" /></template>
        </NDataTable>
        <NSpace v-if="isAdmin" align="end" :size="8" wrap>
          <NInput
            v-if="type === 'hash'" v-model:value="addForm.field" size="tiny" placeholder="field"
            style="width: 140px" :input-props="{ class: 'mono', 'aria-label': '新增条目的 field' }"
          />
          <NInput
            v-model:value="addForm.value" size="tiny" :placeholder="type === 'hash' ? 'value' : '要新增的值'"
            style="width: 190px" :input-props="{ class: 'mono', 'aria-label': '新增条目的值' }"
            @keyup.enter="addMember"
          />
          <NInput
            v-if="type === 'zset'" v-model:value="addForm.score" size="tiny" placeholder="score"
            style="width: 90px" :input-props="{ class: 'mono', 'aria-label': 'sorted set 的 score' }"
          />
          <NButton size="tiny" tertiary type="primary" :loading="busy" :icon="addIco" @click="addMember">新增条目</NButton>
        </NSpace>
      </template>

      <NText v-else depth="3" style="font-size: var(--fs-xs)">该类型暂不支持在线查看（{{ detail.type }}）</NText>

      <div v-if="isAdmin" class="ttl-row">
        <NText depth="3" class="ttl-label">过期时间</NText>
        <NInput
          v-model:value="ttlDraft" size="tiny" style="width: 128px"
          :placeholder="detail.ttl === -1 ? '秒数，如 3600' : `当前 ${detail.ttl}s`"
          :input-props="{ class: 'mono', 'aria-label': 'TTL 秒数' }"
          @keyup.enter="saveTtl"
        />
        <NButton size="tiny" tertiary :loading="busy" :disabled="ttlDraft.trim() === ''" @click="saveTtl">应用</NButton>
        <NText depth="3" style="font-size: var(--fs-2xs)">-1 ＝ 不过期</NText>
      </div>

      <NSpace v-if="isAdmin" justify="end">
        <NPopconfirm @positive-click="emit('deleted', detail.key)">
          <template #trigger>
            <NButton size="tiny" tertiary type="error" :icon="trashIco">删除此 key</NButton>
          </template>
          UNLINK {{ detail.key }}？不可恢复。
        </NPopconfirm>
      </NSpace>
    </NSpace>
  </NCard>
</template>

<style scoped>
/* 卡片标题就是 redis key 本体，动辄上百字符：naive 的 header 不收缩时会把末字裁半截，
   这里给标题 ellipsis（完整 key 在 title 浮层里），并让右侧标签区固定不被挤扁。 */
.rk-detail :deep(.n-card-header) { flex-wrap: wrap; }
.rk-detail :deep(.n-card-header__main) { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rk-detail :deep(.n-card-header__extra) { flex-shrink: 0; max-width: 100%; }
.mono-pre {
  margin: 0; padding: var(--space-3); max-height: 300px; overflow: auto;
  background: var(--cp-bg-soft);
  border: 1px solid var(--cp-border);
  border-radius: var(--radius-md);
  font-family: var(--font-mono); font-size: var(--fs-xs);
  white-space: pre-wrap; word-break: break-all; color: var(--cp-text);
}
.ttl-row {
  display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap;
  padding-top: var(--space-2); border-top: 1px solid var(--cp-border, rgba(128, 128, 128, 0.14));
}
.ttl-label { font-size: var(--fs-xs); min-width: 4em; }
</style>

