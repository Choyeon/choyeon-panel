<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  NAlert, NCard, NIcon, NProgress, NResult, NSpace, NText, NSkeleton, NTooltip, useMessage,
} from 'naive-ui';
import { NButton } from '../ui';
import PageHeader from '../components/PageHeader.vue';
import EmptyBox from '../components/EmptyBox.vue';
import { api } from '../api';
import { icon, icons } from '../icons';

type Item = { id: string; title: string; status: 'pass' | 'warn' | 'fail'; detail: string; fix: string; fixable?: boolean };
type Report = { status: string; counts: { pass: number; warn: number; fail: number }; items: Item[] };

const msg = useMessage();
const loading = ref(true);
const error = ref('');
const report = ref<Report | null>(null);
const fixing = ref('');
const filter = ref<'all' | 'warn' | 'fail'>('all');
let readyTimer: number | null = null;
onBeforeUnmount(() => { if (readyTimer) clearInterval(readyTimer); });

const TYPE = {
  pass: { label: '正常', icon: 'CheckmarkCircleOutline', color: 'var(--cp-ok)' },
  warn: { label: '注意', icon: 'WarningOutline', color: 'var(--cp-warn)' },
  fail: { label: '异常', icon: 'CloseCircleOutline', color: 'var(--cp-err)' },
} as const;

const ico = (name: string, size = 14) => icon(name, size);

const percent = computed(() => {
  const r = report.value;
  if (!r) return 0;
  const total = r.counts.pass + r.counts.warn + r.counts.fail;
  return total ? Math.round((r.counts.pass / total) * 100) : 0;
});

const overallColor = computed(() =>
  report.value?.status === 'pass' ? 'var(--cp-ok)' : report.value?.status === 'warn' ? 'var(--cp-warn)' : 'var(--cp-err)',
);

const shown = computed(() => {
  const items = report.value?.items || [];
  if (filter.value === 'all') return items;
  return items.filter((i) => i.status === filter.value);
});

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

async function copyFix(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    msg.success('修复命令已复制');
  } catch {
    msg.warning('浏览器拒绝了剪贴板访问，请手动复制');
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
    <PageHeader title="安全自检" sub="只读检查，不改任何配置；可自动修复的项目直接点「一键修复」，其余给出可复制命令">
      <template #actions>
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">重新检查</NButton>
      </template>
    </PageHeader>

    <NAlert v-if="error" type="error" :title="error" style="margin-bottom: var(--space-4)" />

    <NSkeleton v-if="loading && !report" height="96px" class="cp-shimmer" style="border-radius: 10px; margin-bottom: var(--space-4)" />

    <template v-if="report">
      <NCard size="small" style="margin-bottom: var(--space-4)">
        <NSpace align="center" :size="16" wrap>
          <NProgress type="circle" :percentage="percent" :height="76" :color="overallColor" />
          <div>
            <NText strong class="cp-pop" style="font-size: var(--fs-lg)">
              通过 {{ report.counts.pass }} · 注意 {{ report.counts.warn }} · 异常 {{ report.counts.fail }}
            </NText>
            <div style="margin-top: var(--space-2); color: var(--cp-text-dim); font-size: var(--fs-sm)">
              {{ report.status === 'pass' ? '全部检查通过' : '优先处理异常项，它们直接影响可用性' }}
            </div>
            <NSpace :size="8" style="margin-top: var(--space-3)">
              <NButton size="tiny" :tertiary="filter !== 'all'" @click="filter = 'all'">全部</NButton>
              <NButton size="tiny" :type="filter === 'fail' ? 'error' : 'default'" :tertiary="filter !== 'fail'" :disabled="!report.counts.fail" @click="filter = 'fail'">
                异常 {{ report.counts.fail }}
              </NButton>
              <NButton size="tiny" :type="filter === 'warn' ? 'warning' : 'default'" :tertiary="filter !== 'warn'" :disabled="!report.counts.warn" @click="filter = 'warn'">
                注意 {{ report.counts.warn }}
              </NButton>
            </NSpace>
          </div>
        </NSpace>
      </NCard>

      <NCard size="small" :content-style="{ padding: '0' }">
        <div
          v-for="(it, idx) in shown" :key="it.id"
          class="doc-row cp-rise" :style="`--i:${idx}`"
        >
          <NIcon :size="18" :component="icons[TYPE[it.status].icon]" :color="TYPE[it.status].color" style="flex-shrink: 0" />
          <div class="doc-txt">
            <NText strong style="font-size: var(--fs-sm)">{{ it.title }}</NText>
            <div class="doc-detail">{{ it.detail }}</div>
            <code v-if="it.fix && it.status !== 'pass' && !it.fixable" class="mono-dim doc-cmd">{{ it.fix }}</code>
          </div>
          <div class="doc-act">
            <span class="st" :class="it.status === 'pass' ? 'ok' : it.status === 'warn' ? 'warn' : 'err'">
              <span class="dot" :class="it.status === 'pass' ? 'ok' : it.status === 'warn' ? 'warn' : 'err'"></span>{{ TYPE[it.status].label }}
            </span>
            <NButton
              v-if="it.fixable && it.status !== 'pass'"
              size="tiny" type="primary" :loading="fixing === it.id" :disabled="!!fixing"
              :icon="ico('BuildOutline')" @click="fix(it)"
            >
              一键修复
            </NButton>
            <NTooltip v-else-if="it.fix && it.status !== 'pass' && it.fixable" trigger="hover">
              <template #trigger>
                <NButton size="tiny" circle quaternary :aria-label="`复制修复命令：${it.title}`" :title="`复制修复命令：${it.title}`" :icon="ico('CopyOutline')" @click="copyFix(it.fix)" />
              </template>
              复制修复命令
            </NTooltip>
          </div>
        </div>
        <EmptyBox v-if="!shown.length" :text="filter === 'all' ? '暂无检查结果' : `没有${filter === 'fail' ? '异常' : '注意'}项，切换筛选查看其他`" />
      </NCard>
    </template>

    <NResult v-if="!loading && !report && !error" status="500" title="暂无自检结果" description="点击「重新检查」重试">
      <template #footer>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">重新检查</NButton>
      </template>
    </NResult>
  </div>
</template>

<style scoped>
.doc-row {
  display: flex; align-items: flex-start; gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--cp-border);
  flex-wrap: wrap;
}
.doc-row:last-child { border-bottom: 0; }
.doc-row:hover { background: var(--cp-hover); }
.doc-txt { flex: 1 1 260px; min-width: 0; }
/* 右侧动作区：以前是 flex-shrink:0 的一整块，窄屏时把说明文字挤到容器外、
   状态标签被裁成半截。改成可换行、靠右对齐，空间不足就整块落到下一行。 */
.doc-act {
  flex: 0 1 auto; margin-left: auto;
  display: flex; align-items: center; justify-content: flex-end; gap: var(--space-2); flex-wrap: wrap;
}
.doc-detail { color: var(--cp-text-mute); font-size: var(--fs-xs); margin-top: 2px; overflow-wrap: anywhere; }
.doc-cmd { display: block; margin-top: var(--space-2); word-break: break-all; }
</style>
