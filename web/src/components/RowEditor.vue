<script setup lang="ts">
import { computed, h, nextTick, ref, watch } from 'vue';
import {
  NDrawer, NDrawerContent, NSpace, NTag, NCheckbox, NInput, NPopconfirm, NText, useMessage,
} from 'naive-ui';
import { useDrawerWidth } from '../ui';
import { NButton } from '../ui';
import { api, getRole } from '../api';
import { icon } from '../icons';
import JsonView from './JsonView.vue';

/**
 * PG 单行抽屉：默认只读详情，点「编辑」才把每列换成表单控件、底部换成「提交更改」。
 * 直接进表单会让人分不清哪些值动过；只读态也顺带解决 JSON 列在网格里看不清的问题。
 */
const props = withDefaults(defineProps<{
  show: boolean;
  db: string;
  schema: string;
  table: string;
  cols: any[];
  pk: string[];
  row: any | null;
  insert?: boolean;
  /** 打开后要高亮并滚到该列（从 JSON 摘要芯片点进来时给） */
  focus?: string | null;
}>(), { insert: false, focus: null });
const emit = defineEmits<{ 'update:show': [v: boolean]; saved: [] }>();

const msg = useMessage();
const drawerW = useDrawerWidth(0.44);
const editing = ref(false);
const busy = ref(false);
const form = ref<Record<string, { isNull: boolean; text: string }>>({});

const udt = (c: any) => String(c?.udt || '');
const isJson = (c: any) => udt(c) === 'json' || udt(c) === 'jsonb';
const isBool = (c: any) => udt(c) === 'bool';
const isWide = (c: any) =>
  isJson(c) || udt(c).startsWith('_') || ['text', 'bytea', 'xml'].includes(udt(c)) || c?.type === 'USER-DEFINED';

function fmtRaw(v: any) {
  if (v === null || v === undefined) return 'NULL';
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}
/** JS 数组 → PG 数组字面量 `{a,b}`：数组列按文本编辑，看到的就是要写回的形式。 */
function arrText(v: any[]) {
  return `{${v.map((x) => {
    if (x === null || x === undefined) return 'NULL';
    if (typeof x === 'number' || typeof x === 'boolean') return String(x);
    return `"${String(x).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
  }).join(',')}}`;
}
function toText(c: any, v: any) {
  if (v === null || v === undefined) return '';
  if (isJson(c)) return JSON.stringify(v, null, 2);
  if (Array.isArray(v)) return arrText(v);
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

// 只读账号在这里只看得到数据：写操作（编辑/删除/创建）在服务端一律拦 403。
const isAdmin = computed(() => getRole() === 'admin');
const title = computed(() => (props.insert ? '新增行' : props.table));
const rowLabel = computed(() => {
  if (props.insert || !props.row) return `${props.schema}.${props.table}`;
  const k = props.pk.length
    ? props.pk.map((c) => `${c}=${fmtRaw(props.row[c])}`).join(' · ')
    : `ctid ${props.row._ctid}`;
  return `${props.schema}.${props.table} · ${k}`;
});
const headIcon = icon('PencilOutline', 14);
const trashIcon = icon('TrashOutline', 14);

function buildForm() {
  const f: Record<string, { isNull: boolean; text: string }> = {};
  for (const c of props.cols) {
    const raw = props.row ? props.row[c.name] : undefined;
    f[c.name] = {
      // 新增时没有“旧值”可言：一律给空文本框，留空即不提交该列
      isNull: props.insert ? false : raw === null || raw === undefined,
      text: props.insert ? '' : toText(c, raw),
    };
  }
  form.value = f;
}
// 抽屉开着时也能点表格里的另一行：只 watch show 会让表单停在上一次的行上
const roRefs = new Map<string, HTMLElement>();
function keepRoRef(name: string, el: unknown) {
  if (el) roRefs.set(name, el as HTMLElement);
  else roRefs.delete(name);
}
// 从 JSON 摘要芯片点进来的话，把那一列滚进视野：字段按列序排，长表里让人
// 自己找到那一列不如直接送过去。等两帧是因为抽屉内容在过渡里才挂上。
async function scrollToFocus() {
  const f = props.focus;
  if (!f) return;
  await nextTick();
  await nextTick();
  roRefs.get(f)?.scrollIntoView({ block: 'center' });
}
watch(
  [() => props.show, () => props.row, () => props.insert, () => props.focus],
  ([open]) => {
    if (!open) return;
    editing.value = props.insert;
    buildForm();
    scrollToFocus();
  },
  { immediate: true },
);

function rowKey(): Record<string, any> {
  if (props.pk.length) {
    const k: Record<string, any> = {};
    for (const c of props.pk) k[c] = props.row?.[c] ?? null;
    return k;
  }
  return { _ctid: props.row?._ctid };
}

function startEdit() {
  buildForm();
  editing.value = true;
}
function cancelEdit() {
  if (props.insert) {
    emit('update:show', false);
    return;
  }
  editing.value = false;
}

/** 表单 → 提交给后端的 values。JSON/布尔在前端先挡一轮，省一次往返和一条看不懂的 PG 报错。 */
function collect(): Record<string, any> | null {
  const values: Record<string, any> = {};
  for (const c of props.cols) {
    const f = form.value[c.name];
    if (!f) continue;
    if (f.isNull) {
      // 更新时「置空」＝真写 NULL；新增时留空＝不提交该列，交给默认值/自增序列
      if (!props.insert) values[c.name] = null;
      continue;
    }
    if (props.insert && !f.text.trim()) continue;
    if (isJson(c) && f.text.trim()) {
      try {
        JSON.parse(f.text);
      } catch {
        msg.error(`「${c.name}」不是合法 JSON，已停止提交`);
        return null;
      }
    }
    if (isBool(c) && !['true', 'false', 't', 'f', '1', '0', 'yes', 'no'].includes(f.text.trim().toLowerCase())) {
      msg.error(`「${c.name}」需要 true 或 false`);
      return null;
    }
    values[c.name] = f.text;
  }
  if (!Object.keys(values).length) {
    msg.error(props.insert ? '至少要填写一个字段' : '没有需要保存的改动');
    return null;
  }
  return values;
}

async function submit() {
  // INSERT 没有幂等键：回车 + 点「保存」两下就是两行内容相同的记录，
  // 而界面只报一次「已新增 1 行」，多出来的那行要到下次翻页才看得见。
  if (busy.value) return;
  const values = collect();
  if (!values) return;
  busy.value = true;
  try {
    const r = await api.pgRow(props.db, {
      action: props.insert ? 'insert' : 'update',
      schema: props.schema,
      table: props.table,
      key: props.insert ? undefined : rowKey(),
      values,
    });
    const pk = r?.pk ? Object.entries(r.pk).map(([k, v]) => `${k}=${v}`).join(' · ') : '';
    msg.success(props.insert ? `已新增 1 行${pk ? `（${pk}）` : ''}` : `已更新 ${r?.affected ?? 1} 行`);
    emit('saved');
    if (props.insert) emit('update:show', false);
    else editing.value = false;
  } catch (e: any) {
    // 失败时保留表单内容：报错即清空等于让用户凭记忆重打一遍
    msg.error(e.message);
  } finally {
    busy.value = false;
  }
}

async function delRow() {
  if (busy.value) return;
  busy.value = true;
  try {
    const r = await api.pgRow(props.db, {
      action: 'delete', schema: props.schema, table: props.table, key: rowKey(),
    });
    msg.success(`已删除 ${r?.affected ?? 1} 行`);
    emit('saved');
    emit('update:show', false);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <NDrawer
    v-if="show" :show="true" :width="drawerW" placement="right"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <NDrawerContent :native-scrollbar="false" closable>
      <template #header>
        <NSpace :size="8" align="center" :wrap="false">
          <span class="dr-title">{{ title }}</span>
          <NTag size="tiny" :bordered="false">{{ editing ? '编辑' : '查看' }}</NTag>
          <NTag v-if="!insert && !pk.length" size="tiny" :bordered="false" type="warning">无主键 · ctid 定位</NTag>
        </NSpace>
      </template>

      <NSpace vertical :size="10">
        <NText depth="3" style="font-size: var(--fs-xs)">{{ rowLabel }}</NText>

        <div v-if="!editing" class="ro">
          <div
            v-for="c in cols" :key="c.name"
            :class="['ro-row', focus === c.name ? 'ro-focus' : '']"
            :ref="(el: any) => keepRoRef(c.name, el)"
          >
            <div class="ro-label">
              <span class="ro-name">{{ c.name }}</span>
              <span class="ro-type">{{ c.type }}</span>
              <NTag v-if="pk.includes(c.name)" size="tiny" :bordered="false" type="info">PK</NTag>
              <NTag v-else-if="c.nullable" size="tiny" :bordered="false">NULL</NTag>
            </div>
            <div class="ro-value">
              <JsonView v-if="typeof row?.[c.name] === 'object' && row?.[c.name] !== null" :value="row[c.name]" />
              <span v-else-if="row?.[c.name] === null || row?.[c.name] === undefined" class="ro-null">NULL</span>
              <span v-else :class="['ro-val', isWide(c) ? 'ro-wide' : '']">{{ fmtRaw(row[c.name]) }}</span>
            </div>
          </div>
        </div>

        <div v-else class="fm">
          <div v-for="c in cols" :key="c.name" class="fm-row">
            <div class="fm-head">
              <div class="ro-label">
                <span class="ro-name">{{ c.name }}</span>
                <span class="ro-type">{{ c.type }}</span>
                <NTag v-if="pk.includes(c.name)" size="tiny" :bordered="false" type="info">PK</NTag>
              </div>
              <NCheckbox v-if="!insert" v-model:checked="form[c.name].isNull" size="small">置为 NULL</NCheckbox>
            </div>
            <NInput
              v-if="!form[c.name].isNull"
              v-model:value="form[c.name].text"
              size="small"
              :type="isWide(c) ? 'textarea' : 'text'"
              :autosize="isWide(c) ? { minRows: 2, maxRows: 12 } : undefined"
              :placeholder="insert ? '留空＝不提交该列，交给默认值'
                : (isBool(c) ? 'true / false' : (c.nullable ? '留空＝空字符串，勾「置为 NULL」才写 NULL' : '必填'))"
              :input-props="{ class: 'mono', 'aria-label': `字段 ${c.name}` }"
            />
            <div v-else class="fm-skip">将写入 NULL</div>
          </div>
        </div>
      </NSpace>

      <template #footer>
        <NSpace justify="space-between" align="center" style="width: 100%">
          <NText depth="3" style="font-size: var(--fs-xs)">
            {{ insert ? '留空的列不会出现在 INSERT 里（由默认值/自增填充）' : (pk.length ? `主键：${pk.join('、')}` : '该表无主键，按 ctid 定位') }}
          </NText>
          <NSpace :size="8">
            <template v-if="!editing">
              <NPopconfirm v-if="isAdmin && !insert" @positive-click="delRow">
                <template #trigger>
                  <NButton size="small" tertiary type="error" :icon="trashIcon" :loading="busy" :disabled="busy">删除行</NButton>
                </template>
                删除该行？不可恢复
              </NPopconfirm>
              <NButton size="small" tertiary @click="emit('update:show', false)">关闭</NButton>
              <NButton v-if="isAdmin" size="small" type="primary" :icon="headIcon" @click="startEdit">编辑</NButton>
            </template>
            <template v-else>
              <NButton size="small" tertiary :disabled="busy" @click="cancelEdit">取消</NButton>
              <NButton size="small" type="primary" class="cp-press" :loading="busy" :disabled="busy" @click="submit">
                {{ insert ? '创建' : '提交更改' }}
              </NButton>
            </template>
          </NSpace>
        </NSpace>
      </template>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
/* 抽屉标题（表名/新增行）在 nowrap 的 NSpace 里，长表名会被挤出 header-extra 的按钮区：
   给它收缩 + ellipsis，两个标签固定不塌。 */
.dr-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.ro { display: flex; flex-direction: column; gap: 2px; }
.ro-row {
  display: flex; flex-direction: column; gap: 4px; padding: var(--space-2) 0;
  border-bottom: 1px solid var(--cp-border);
}
/* 芯片点进来的那一列：左边一道重音色 + 极浅底，滚动位置也落在它身上 */
.ro-focus {
  box-shadow: inset 2px 0 0 var(--cp-brand-soft);
  background: var(--cp-bg-soft);
  padding-left: var(--space-2);
}
.ro-label { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ro-name { font-weight: 600; font-size: var(--fs-sm); color: var(--cp-text); min-width: 0; overflow-wrap: anywhere; }
.ro-type { font-size: var(--fs-2xs); color: var(--cp-text-mute); font-family: var(--font-mono); }
.ro-value { min-width: 0; }
.ro-val { font-size: var(--fs-sm); color: var(--cp-text); word-break: break-all; overflow-wrap: anywhere; }
.ro-wide {
  display: block; font-family: var(--font-mono); font-size: var(--fs-xs);
  white-space: pre-wrap; word-break: break-all; max-height: 240px; overflow: auto;
  padding: var(--space-2); border-radius: var(--radius-md);
  background: var(--cp-bg-soft);
}
.ro-null, .fm-skip { font-size: var(--fs-xs); font-style: italic; color: var(--cp-text-mute); }
.fm { display: flex; flex-direction: column; gap: var(--space-3); }
.fm-row { display: flex; flex-direction: column; gap: 5px; }
.fm-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
</style>
