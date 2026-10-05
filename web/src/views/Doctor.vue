<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import {
  NAlert, NButton, NCard, NIcon, NProgress, NResult, NSpace, NSkeleton, NTag, NText, NTooltip,
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
  pass: { tag: 'success', label: '正常', icon: 'CheckmarkCircleOutline', color: 'var(--cp-ok, #34c77b)' },
  warn: { tag: 'warning', label: '注意', icon: 'WarningOutline', color: '#f5a623' },
  fail: { tag: 'error', label: '异常', icon: 'CloseCircleOutline', color: 'var(--cp-err, #f5616c)' },
} as const;

const percent = computed(() => {
  const r = report.value;
  if (!r) return 0;
  const total = r.counts.pass + r.counts.warn + r.counts.fail;
  return total ? Math.round((r.counts.pass / total) * 100) : 0;
});

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
    <PageHeader title="安全自检" subtitle="只读检查，不改任何配置；每项都给出可复制的修复命令">
      <template #actions>
        <NButton size="small" secondary :loading="loading" @click="load">
          <template #icon><NIcon :component="icons.RefreshOutline" /></template>
          重新检查
        </NButton>
      </template>
    </PageHeader>

    <NAlert v-if="error" type="error" :title="error" style="margin-bottom: 14px" />

    <NSkeleton v-if="loading && !report" height="96px" class="cp-shimmer" style="border-radius: 10px; margin-bottom: 14px" />

    <template v-if="report">
      <NCard size="small" style="margin-bottom: 14px">
        <NSpace align="center" :size="16" wrap>
          <NProgress
            type="circle"
            :percentage="percent"
            :height="80"
            :color="report.status === 'pass' ? '#34c77b' : report.status === 'warn' ? '#f5a623' : '#f5616c'"
          />
          <div>
            <NText strong style="font-size: 16px">
              通过 {{ report.counts.pass }} · 注意 {{ report.counts.warn }} · 异常 {{ report.counts.fail }}
            </NText>
            <div style="margin-top: 6px; color: var(--cp-text-dim); font-size: 13px">
              {{ report.status === 'pass' ? '全部检查通过' : '按下方修复建议逐项处理，异常项会直接影响可用性' }}
            </div>
            <NSpace v-if="nextFix" :size="8" style="margin-top: 10px" align="center">
              <NText depth="3" style="font-size: 12.5px">优先处理：</NText>
              <code class="mono-dim">{{ nextFix.fix }}</code>
              <NTooltip trigger="hover">
                <template #trigger>
                  <NButton size="tiny" text @click="copyFix(nextFix.fix)">
                    <template #icon><NIcon :component="icons.CopyOutline" /></template>
                  </NButton>
                </template>
                复制修复命令
              </NTooltip>
            </NSpace>
          </div>
        </NSpace>
      </NCard>

      <NSpace vertical :size="10">
        <NCard
          v-for="(it, idx) in report.items"
          :key="it.id"
          size="small"
          class="cp-rise"
          :style="`--i:${idx}`"
        >
          <NSpace align="center" justify="space-between" :wrap="false" style="gap: 12px">
            <NSpace align="center" :size="10" style="min-width: 0">
              <NIcon
                :size="18"
                :component="icons[TYPE[it.status].icon] || icons.InformationCircleOutline"
                :color="TYPE[it.status].color"
              />
              <NText strong>{{ it.title }}</NText>
            </NSpace>
            <NTag :type="TYPE[it.status].tag" size="small" round>{{ TYPE[it.status].label }}</NTag>
          </NSpace>
          <div style="margin-top: 6px; color: var(--cp-text-mute); font-size: 13px">{{ it.detail }}</div>
          <NSpace v-if="it.fix && it.status !== 'pass'" :size="8" align="center" style="margin-top: 8px">
            <code class="mono-dim">{{ it.fix }}</code>
            <NButton size="tiny" text @click="copyFix(it.fix)">
              <template #icon><NIcon :component="icons.CopyOutline" /></template>
              复制
            </NButton>
          </NSpace>
        </NCard>
      </NSpace>
    </template>

    <NResult v-if="!loading && !report && !error" status="500" title="暂无自检结果" description="点击「重新检查」重试">
      <template #footer>
        <NButton size="small" @click="load">重新检查</NButton>
      </template>
    </NResult>
  </div>
</template>
