<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue';
import {
  NAlert, NButton, NCard, NIcon, NProgress, NResult, NSpace, NText, NSkeleton, NTooltip,
  useMessage,
} from 'naive-ui';
import PageHeader from '../components/PageHeader.vue';
import { api } from '../api';
import { icons } from '../icons';

type Item = { id: string; title: string; status: 'pass' | 'warn' | 'fail'; detail: string; fix: string };
type Report = { status: string; counts: { pass: number; warn: number; fail: number }; items: Item[] };

const msg = useMessage();
const loading = ref(true);
const error = ref('');
const report = ref<Report | null>(null);

const TYPE = {
  pass: { st: 'ok', label: '正常', icon: 'CheckmarkCircleOutline', color: 'var(--cp-ok)' },
  warn: { st: 'warn', label: '注意', icon: 'WarningOutline', color: 'var(--cp-warn)' },
  fail: { st: 'err', label: '异常', icon: 'CloseCircleOutline', color: 'var(--cp-err)' },
} as const;

function ico(name: string, size = 14) {
  return () => h(NIcon, { size }, { default: () => h(icons[name]) });
}

const percent = computed(() => {
  const r = report.value;
  if (!r) return 0;
  const total = r.counts.pass + r.counts.warn + r.counts.fail;
  return total ? Math.round((r.counts.pass / total) * 100) : 0;
});

const overallColor = computed(() =>
  report.value?.status === 'pass' ? 'var(--cp-ok)' : report.value?.status === 'warn' ? 'var(--cp-warn)' : 'var(--cp-err)',
);

const nextFix = computed(() => report.value?.items.find((i) => i.status !== 'pass' && i.fix) || null);

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

onMounted(load);
</script>

<template>
  <div>
    <PageHeader title="安全自检" sub="只读检查，不改任何配置；每项都给出可复制的修复命令">
      <template #actions>
        <NButton size="small" tertiary :loading="loading" :icon="ico('RefreshOutline')" @click="load">重新检查</NButton>
      </template>
    </PageHeader>

    <NAlert v-if="error" type="error" :title="error" style="margin-bottom: var(--space-4)" />

    <NSkeleton v-if="loading && !report" height="96px" class="cp-shimmer" style="border-radius: 10px; margin-bottom: var(--space-4)" />

    <template v-if="report">
      <NCard size="small" style="margin-bottom: var(--space-4)">
        <NSpace align="center" :size="16" wrap>
          <NProgress
            type="circle"
            :percentage="percent"
            :height="80"
            :color="overallColor"
          />
          <div>
            <NText strong class="cp-pop" style="font-size: var(--fs-lg)">
              通过 {{ report.counts.pass }} · 注意 {{ report.counts.warn }} · 异常 {{ report.counts.fail }}
            </NText>
            <div style="margin-top: var(--space-2); color: var(--cp-text-dim); font-size: var(--fs-sm)">
              {{ report.status === 'pass' ? '全部检查通过' : '按下方修复建议逐项处理，异常项会直接影响可用性' }}
            </div>
            <NSpace v-if="nextFix" :size="8" style="margin-top: var(--space-3)" align="center">
              <NText depth="3" style="font-size: var(--fs-sm)">优先处理：</NText>
              <code class="mono-dim">{{ nextFix.fix }}</code>
              <NTooltip trigger="hover">
                <template #trigger>
                  <NButton
                    size="tiny"
                    circle
                    quaternary
                    aria-label="复制优先修复命令"
                    title="复制修复命令"
                    :icon="ico('CopyOutline')"
                    @click="copyFix(nextFix.fix)"
                  />
                </template>
                复制修复命令
              </NTooltip>
            </NSpace>
          </div>
        </NSpace>
      </NCard>

      <NSpace vertical :size="12">
        <NCard
          v-for="(it, idx) in report.items"
          :key="it.id"
          size="small"
          class="cp-rise"
          :style="`--i:${idx}`"
        >
          <NSpace align="center" justify="space-between" style="gap: var(--space-3)">
            <NSpace align="center" :size="8" style="min-width: 0">
              <NIcon
                :size="18"
                :component="icons[TYPE[it.status].icon] || icons.InformationCircleOutline"
                :color="TYPE[it.status].color"
              />
              <NText strong>{{ it.title }}</NText>
            </NSpace>
            <span class="st" :class="TYPE[it.status].st">
              <span class="dot" :class="TYPE[it.status].st"></span>{{ TYPE[it.status].label }}
            </span>
          </NSpace>
          <div style="margin-top: var(--space-2); color: var(--cp-text-mute); font-size: var(--fs-sm)">{{ it.detail }}</div>
          <NSpace v-if="it.fix && it.status !== 'pass'" :size="8" align="center" style="margin-top: var(--space-2)">
            <code class="mono-dim">{{ it.fix }}</code>
            <NTooltip trigger="hover">
              <template #trigger>
                <NButton
                  size="tiny"
                  circle
                  quaternary
                  :aria-label="`复制修复命令：${it.title}`"
                  :title="`复制修复命令：${it.title}`"
                  :icon="ico('CopyOutline')"
                  @click="copyFix(it.fix)"
                />
              </template>
              复制修复命令
            </NTooltip>
          </NSpace>
        </NCard>
      </NSpace>
    </template>

    <NResult v-if="!loading && !report && !error" status="500" title="暂无自检结果" description="点击「重新检查」重试">
      <template #footer>
        <NButton size="small" tertiary :icon="ico('RefreshOutline')" @click="load">重新检查</NButton>
      </template>
    </NResult>
  </div>
</template>
