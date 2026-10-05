<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue';
import {
  NConfigProvider, NMessageProvider, NDialogProvider, NNotificationProvider,
  zhCN, dateZhCN, darkTheme, type GlobalThemeOverrides,
} from 'naive-ui';
import Shell from './Shell.vue';

type Mode = 'dark' | 'light' | 'auto';
const STORE_KEY = 'cp_theme';

const mode = ref<Mode>((localStorage.getItem(STORE_KEY) as Mode) || 'auto');
const systemDark = ref(window.matchMedia('(prefers-color-scheme: dark)').matches);
let mq: MediaQueryList | null = null;

const isDark = computed(() => (mode.value === 'auto' ? systemDark.value : mode.value === 'dark'));

const common: GlobalThemeOverrides['common'] = {
  primaryColor: '#4F7CFF',
  primaryColorHover: '#6B92FF',
  primaryColorPressed: '#3D66E8',
  primaryColorSuppl: '#4F7CFF',
  infoColor: '#5B8DEF',
  successColor: '#34C77B',
  warningColor: '#F5A623',
  errorColor: '#F5616C',
  borderRadius: '8px',
  borderRadiusSmall: '6px',
  fontFamily: `'Inter','PingFang SC','HarmonyOS Sans SC','Microsoft YaHei',system-ui,sans-serif`,
};

const darkOverrides: GlobalThemeOverrides = {
  common,
  Card: { borderRadius: '12px', borderColor: 'rgba(255,255,255,0.07)', color: '#16161c', boxShadow: '0 1px 3px rgba(0,0,0,0.3)' },
  DataTable: { thColor: 'rgba(255,255,255,0.035)', thFontWeight: '600', borderColor: 'rgba(255,255,255,0.06)', tdPaddingSmall: '8px 12px', thPaddingSmall: '10px 12px' },
  Tag: { borderRadius: '5px' },
  Button: { fontWeight: '500' },
  Input: { color: 'rgba(255,255,255,0.045)' },
  Layout: {
    color: '#101014',
    siderColor: '#16161C',
    siderBorderColor: 'rgba(255,255,255,0.06)',
    headerColor: '#141419',
    headerBorderColor: 'rgba(255,255,255,0.06)',
  },
  Menu: { itemHeight: '40px', itemTextColor: 'inherit' },
};

const lightOverrides: GlobalThemeOverrides = {
  common: { ...common, bodyColor: '#f5f6fa' },
  Card: { borderRadius: '12px', borderColor: '#e3e6ef', color: '#ffffff', boxShadow: '0 1px 2px rgba(15,23,42,0.06)' },
  DataTable: { thColor: '#f7f8fc', thFontWeight: '600', borderColor: '#eceef6', tdPaddingSmall: '8px 12px', thPaddingSmall: '10px 12px' },
  Tag: { borderRadius: '5px' },
  Button: { fontWeight: '500' },
  Input: { color: '#ffffff' },
  Layout: {
    color: '#f5f6fa',
    siderColor: '#ffffff',
    siderBorderColor: '#e3e6ef',
    headerColor: '#ffffff',
    headerBorderColor: '#e3e6ef',
  },
  Menu: { itemHeight: '40px', itemTextColor: 'inherit' },
};

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
