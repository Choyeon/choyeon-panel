<script setup lang="ts">
import { computed, defineComponent, nextTick, onMounted, onScopeDispose, ref } from 'vue';
import {
  NConfigProvider, NMessageProvider, NDialogProvider, NNotificationProvider, NLoadingBarProvider,
  useLoadingBar, zhCN, darkTheme,
} from 'naive-ui';
import { darkOverrides, lightOverrides } from './theme';
import { revealThemeSwitch, type ThemeOrigin } from './themeReveal';
import { bindLoadingBar } from './composables/useAppProgress';
import Shell from './Shell.vue';

/**
 * 进度条只在 provider 作用域内可取（useLoadingBar），而计数器的唯一持有者是
 * composables/useAppProgress（api.ts 直接调用，不需要 inject）。这个空渲染的
 * 组件只负责在挂载时把自己交给它、卸载时收回。
 */
const LoadingBarBridge = defineComponent({
  name: 'LoadingBarBridge',
  setup() {
    const bar = useLoadingBar();
    bindLoadingBar({ start: () => bar.start(), finish: () => bar.finish(), error: () => bar.error() });
    onScopeDispose(() => bindLoadingBar(null));
    return () => null;
  },
});

type Mode = 'dark' | 'light';
const STORE_KEY = 'cp_theme';

function initialMode(): Mode {
  const saved = localStorage.getItem(STORE_KEY);
  if (saved === 'dark' || saved === 'light') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

const mode = ref<Mode>(initialMode());

const isDark = computed(() => mode.value === 'dark');
const themeOverrides = computed(() => (isDark.value ? darkOverrides : lightOverrides));

function apply() {
  document.documentElement.dataset.theme = isDark.value ? 'dark' : 'light';
  const meta = document.querySelector('meta[name="theme-color"]');
  // 读 token 而不是再抄一份色值：这里和 style.css 的 --cp-bg 一旦分叉，
  // 浏览器工具栏/移动状态栏就会和页面底色对不上。
  const bg = getComputedStyle(document.documentElement).getPropertyValue('--cp-bg').trim();
  if (meta && bg) meta.setAttribute('content', bg);
}

function setMode(m: Mode, origin?: ThemeOrigin) {
  if (m === mode.value) return;
  localStorage.setItem(STORE_KEY, m);
  const commit = async () => {
    mode.value = m;
    apply();
    await nextTick();
  };
  void revealThemeSwitch({ origin, toDark: m === 'dark', mutate: commit });
}

onMounted(() => apply());

defineExpose({ setMode, mode });
</script>

<template>
  <NConfigProvider
    :theme="isDark ? darkTheme : null"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :inline-theme-disabled="false"
  >
    <NMessageProvider :duration="3000" :max="3" placement="top">
      <NNotificationProvider :max="3" placement="bottom-right">
        <NLoadingBarProvider :delay="180" :duration="240" placement="top">
          <LoadingBarBridge />
          <NDialogProvider>
            <Shell :theme-mode="mode" @update:theme-mode="setMode" />
          </NDialogProvider>
        </NLoadingBarProvider>
      </NNotificationProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>
