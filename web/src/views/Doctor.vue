<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  NAlert, NCard, NDataTable, NGrid, NGridItem, NIcon, NPopover, NResult, NSkeleton, NSpace, NText, useMessage,
} from 'naive-ui';
import { NButton } from '../ui';
import PageHeader from '../components/PageHeader.vue';
import CpToolbarCard from '../components/CpToolbarCard.vue';
import CpStatusTag from '../components/CpStatusTag.vue';
import CopyButton from '../components/CopyButton.vue';
import DescriptionsCard from '../components/DescriptionsCard.vue';
import StatGauge from '../components/StatGauge.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { api } from '../api';
import { icon, icons } from '../icons';
import { useTableHeight } from '../composables/useTableHeight';

type Item = { id: string; title: string; status: 'pass' | 'warn' | 'fail'; detail: string; fix: string; fixable?: boolean };
type Report = { status: string; counts: { pass: number; warn: number; fail: number }; items: Item[] };

const router = useRouter();
const msg = useMessage();
const loading = ref(true);
const error = ref('');
const report = ref<Report | null>(null);
const fixing = ref('');
const filter = ref<'all' | 'warn' | 'fail'>('all');
let readyTimer: number | null = null;
onBeforeUnmount(() => { if (readyTimer) clearInterval(readyTimer); });
const { anchor: tableAnchor, heightPx: tableH } = useTableHeight({ reserve: 16, min: 240 });

const TYPE = {
  pass: { label: '正常', icon: 'CheckmarkCircleOutline', color: 'var(--cp-ok)' },
  warn: { label: '注意', icon: 'WarningOutline', color: 'var(--cp-warn)' },
  fail: { label: '异常', icon: 'CloseCircleOutline', color: 'var(--cp-err)' },
} as const;

// CpStatusTag 的语义色来自 1Panel 的状态词表：healthy→success、degraded→warning、unhealthy→danger
const TAG = { pass: 'healthy', warn: 'degraded', fail: 'unhealthy' } as const;
const STATUS_LABEL: Record<string, string> = { pass: '全部通过', warn: '存在注意项', fail: '存在异常项' };

const ico = (name: string, size = 14) => icon(name, size);

// 概览读数条的 hover 明细在触屏上要能点出来，和 1Panel 的 hasFinePointer 判断等价
const finePointer = typeof window !== 'undefined'
  && !!window.matchMedia?.('(hover: hover) and (pointer: fine)').matches;

const total = computed(() => {
  const c = report.value?.counts;
  return c ? c.pass + c.warn + c.fail : 0;
});
const pct = (n: number) => (total.value ? Math.round((n / total.value) * 100) : 0);

const percent = computed(() => pct(report.value?.counts.pass ?? 0));

const overallColor = computed(() =>
  report.value?.status === 'pass' ? 'var(--cp-ok)' : report.value?.status === 'warn' ? 'var(--cp-warn)' : 'var(--cp-err)',
);

const gauges = computed(() => {
  const r = report.value;
  if (!r) return [];
  return [
    { key: 'overall' as const, percent: percent.value, text: String(percent.value), unit: '%', label: '通过率', detail: r.status === 'pass' ? '全部检查通过' : '优先处理异常项', color: overallColor.value },
    { key: 'pass' as const, percent: pct(r.counts.pass), text: String(r.counts.pass), unit: '项', label: '正常', detail: `共 ${total.value} 项检查`, color: 'var(--cp-ok)' },
    { key: 'warn' as const, percent: pct(r.counts.warn), text: String(r.counts.warn), unit: '项', label: '注意', detail: '不阻断使用，建议处理', color: 'var(--cp-warn)' },
    { key: 'fail' as const, percent: pct(r.counts.fail), text: String(r.counts.fail), unit: '项', label: '异常', detail: '直接影响可用性', color: 'var(--cp-err)' },
  ];
});

const byStatus = (s: string) => (report.value?.items || []).filter((i) => i.status === s);

const shown = computed(() => {
  const items = report.value?.items || [];
  if (filter.value === 'all') return items;
  return items.filter((i) => i.status === filter.value);
});

const columns: any[] = [
  {
    title: '项目', key: 'title', minWidth: 240,
    render: (r: Item) =>
      h('div', { class: 'cell-main' }, [
        h('span', { class: 'cell-ico' }, [
          h(NIcon, { component: icons[TYPE[r.status].icon], size: 17, color: TYPE[r.status].color }),
        ]),
        h('div', { class: 'cell-txt' }, [
          h('div', { class: 'cell-name' }, r.title),
          h('div', { class: 'cell-sub cp-prose', title: r.detail }, r.detail),
        ]),
      ]),
  },
  {
    title: '结果', key: 'status', width: 110,
    render: (r: Item) => h(CpStatusTag, { status: TAG[r.status], text: TYPE[r.status].label }),
  },
  {
    title: '操作', key: 'act', width: 160, fixed: 'right' as const,
    render: (r: Item) => {
      if (r.status === 'pass' || (!r.fixable && !r.fix)) return h(NText, { depth: 3 }, () => '—');
      const children: any[] = [];
      if (r.fixable) {
        children.push(h(NButton, {
          text: true, type: 'primary', size: 'small',
          loading: fixing.value === r.id, disabled: !!fixing.value, onClick: () => fix(r),
        }, () => '一键修复'));
      }
      if (r.fix) {
        if (!r.fixable) children.push(h('span', { class: 'vchip', title: r.fix }, r.fix));
        children.push(h(CopyButton, { text: r.fix, label: `复制修复命令：${r.title}` }));
      }
      // 操作列统一走全局 .cp-ops（5px gap + 居中），不再单开一个 NSpace：
      // 这一列的按钮和 CpRowActions 渲染的是同一形态，间距却各算各的。
      return h('div', { class: 'cp-ops' }, children);
    },
  },
];

async function load() {
  loading.value = true;
  error.value = '';
  try {
    report.value = (await api.doctor()) as Report;
  } catch (e: any) {
    error.value = e?.message || '自检失败';
  } finally {
    loading.value = false;
  }
}

// 面板重启后自恢复：轮询 /api/ready，通了就重新自检
function watchReady() {
  let tries = 0;
  readyTimer = window.setInterval(async () => {
    tries += 1;
    try {
      const r = await api.ready();
      if (r?.ready) {
        if (readyTimer) clearInterval(readyTimer);
        readyTimer = null;
        msg.success('面板已重启完成，正在重新自检');
        load();
      }
    } catch { /* 重启窗口内请求必然失败，继续等 */ }
    if (tries > 30 && readyTimer) {
      clearInterval(readyTimer);
      readyTimer = null;
      msg.warning('重启超时，请刷新页面确认面板状态');
    }
  }, 2000);
}

async function fix(it: Item) {
  // 有一条修复是重启面板自身：重启窗口里的第二次请求根本到不了后端，只会以一句网络错误
  // 收场，而同一条修复跑两遍也没有意义。
  if (fixing.value !== '') return;
  fixing.value = it.id;
  try {
    const r = await api.doctorFix(it.id);
    if (r?.restarting) {
      msg.info(r.message || '面板正在重启');
      watchReady();
    } else if (r?.ok) {
      msg.success(r.message || '修复完成');
      await load();
    } else {
      msg.error(r?.item?.detail || '修复后仍有问题');
    }
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    fixing.value = '';
  }
}

onMounted(load);
</script>

<template>
  <div>
    <PageHeader title="安全自检">
      <template #actions>
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">重新检查</NButton>
      </template>
    </PageHeader>

    <div class="doc-prompt">
      <NAlert type="info" :bordered="false" class="doc-alert">
        只读检查，不改任何配置；可自动修复的项目点「一键修复」，其余项目给出可复制的修复命令
      </NAlert>
      <NButton text type="primary" size="small" :icon="ico('ArrowForwardOutline')" @click="router.push('/settings')">前往设置</NButton>
    </div>

    <NAlert v-if="error" type="error" :title="error" style="margin-bottom: var(--space-4)" />

    <NSkeleton v-if="loading && !report" height="96px" class="cp-shimmer" style="border-radius: 10px; margin-bottom: var(--space-4)" />

    <template v-if="report">
      <NCard size="small" style="margin-bottom: var(--space-4)">
        <div class="cp-chart-title" style="margin-bottom: var(--space-2)">检查汇总</div>
        <NGrid cols="1 s:2 m:4" :x-gap="8" :y-gap="8" responsive="screen" item-responsive>
          <NGridItem v-for="g in gauges" :key="g.key" span="1">
            <NPopover :trigger="finePointer ? 'hover' : 'click'" :width="320" :delay="150">
              <template #trigger>
                <div class="doc-band">
                  <StatGauge :percent="g.percent" :text="g.text" :unit="g.unit" :label="g.label" :detail="g.detail" :color="g.color" :size="104" />
                </div>
              </template>
              <DescriptionsCard
                v-if="g.key === 'overall'"
                :items="[
                  { label: '总体状态', value: STATUS_LABEL[report.status] || report.status },
                  { label: '检查项总数', value: String(total) },
                  { label: '正常', value: `${report.counts.pass} 项` },
                  { label: '注意', value: `${report.counts.warn} 项` },
                  { label: '异常', value: `${report.counts.fail} 项` },
                ]"
              />
              <div v-else class="doc-pop">
                <div v-for="i in byStatus(g.key)" :key="i.id" class="doc-pop-item">{{ i.title }}</div>
                <div v-if="!byStatus(g.key).length" class="doc-pop-empty">暂无条目</div>
              </div>
            </NPopover>
          </NGridItem>
        </NGrid>
      </NCard>

      <CpToolbarCard title="自检项目">
        <template #right>
          <NSpace :size="8">
            <NButton size="tiny" :tertiary="filter !== 'all'" @click="filter = 'all'">全部</NButton>
            <NButton size="tiny" :type="filter === 'fail' ? 'error' : 'default'" :tertiary="filter !== 'fail'" :disabled="!report.counts.fail" @click="filter = 'fail'">
              异常 {{ report.counts.fail }}
            </NButton>
            <NButton size="tiny" :type="filter === 'warn' ? 'warning' : 'default'" :tertiary="filter !== 'warn'" :disabled="!report.counts.warn" @click="filter = 'warn'">
              注意 {{ report.counts.warn }}
            </NButton>
          </NSpace>
        </template>
        <NDataTable
          ref="tableAnchor"
          :columns="columns" :data="shown" size="small" :bordered="false" :row-key="(i: Item) => i.id"
          :scroll-x="'auto'" :max-height="tableH" class="cp-tbl"
        >
          <template #empty>
            <EmptyBox :text="filter === 'all' ? '暂无检查结果' : `没有${filter === 'fail' ? '异常' : '注意'}项，切换筛选查看其他`" />
          </template>
        </NDataTable>
      </CpToolbarCard>
    </template>

    <NResult v-if="!loading && !report && !error" status="500" title="暂无自检结果" description="点击「重新检查」重试">
      <template #footer>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">重新检查</NButton>
      </template>
    </NResult>
  </div>
</template>

<style scoped>
.doc-prompt { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: var(--space-4); }
.doc-alert { flex: 1 1 320px; min-width: 0; }
/* 概览读数 160px 一档（1Panel home/status 的 v-charts height=160px），明细走 hover popover */
.doc-band {
  height: 160px; display: flex; align-items: center; justify-content: center;
  border-radius: var(--radius); cursor: default;
  transition: background var(--dur) var(--ease);
}
.doc-band:hover { background: var(--cp-hover); }
.doc-pop { max-height: 260px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
.doc-pop-item { font-size: var(--fs-sm); color: var(--cp-text); }
.doc-pop-empty { font-size: var(--fs-sm); color: var(--cp-text-mute); }
</style>
