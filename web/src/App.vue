<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue';
import {
  NConfigProvider, NMessageProvider, NDialogProvider, NNotificationProvider,
  zhCN, dateZhCN, darkTheme,
} from 'naive-ui';
import { darkOverrides, lightOverrides } from './theme';
import Shell from './Shell.vue';

type Mode = 'dark' | 'light' | 'auto';
const STORE_KEY = 'cp_theme';

const mode = ref<Mode>((localStorage.getItem(STORE_KEY) as Mode) || 'auto');
const systemDark = ref(window.matchMedia('(prefers-color-scheme: dark)').matches);
let mq: MediaQueryList | null = null;

const isDark = computed(() => (mode.value === 'auto' ? systemDark.value : mode.value === 'dark'));

const themeOverrides = computed(() => (isDark.value ? darkOverrides : lightOverrides));

function apply() {
  document.documentElement.dataset.theme = isDark.value ? 'dark' : 'light';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', isDark.value ? '#101014' : '#f5f6fa');
}

function setMode(m: Mode) {
  mode.value = m;
  localStorage.setItem(STORE_KEY, m);
  apply();
}

function onSystemChange(e: MediaQueryListEvent) {
  systemDark.value = e.matches;
  if (mode.value === 'auto') apply();
}

onMounted(() => {
  apply();
  mq = window.matchMedia('(prefers-color-scheme: dark)');
  mq.addEventListener('change', onSystemChange);
});
onBeforeUnmount(() => mq?.removeEventListener('change', onSystemChange));

defineExpose({ setMode, mode });
</script>

<template>
  <NConfigProvider
    :theme="isDark ? darkTheme : null"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
    :inline-theme-disabled="false"
  >
    <NMessageProvider :duration="3000" :max="3" placement="top">
      <NNotificationProvider :max="3" placement="bottom-right">
        <NDialogProvider>
          <Shell :theme-mode="mode" @update:theme-mode="setMode" />
        </NDialogProvider>
      </NNotificationProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>
