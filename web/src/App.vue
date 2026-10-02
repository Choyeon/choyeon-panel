<script setup lang="ts">
import { NConfigProvider, NMessageProvider, NDialogProvider, NNotificationProvider, zhCN, dateZhCN, darkTheme } from 'naive-ui';
import Shell from './Shell.vue';

const themeOverrides = {
  common: {
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
    fontFamily: `'Inter','PingFang SC','HarmonyOS Sans SC','Microsoft YaHei',sans-serif`,
  },
  Card: { borderRadius: '12px', borderColor: 'rgba(255,255,255,0.07)', boxShadow: '0 1px 3px rgba(0,0,0,0.3)' },
  DataTable: { thColor: 'rgba(255,255,255,0.035)', thFontWeight: '600', borderColor: 'rgba(255,255,255,0.06)', tdPaddingSmall: '8px 12px', thPaddingSmall: '10px 12px' },
  Tag: { borderRadius: '5px' },
  Button: { fontWeight: '500' },
  Input: { color: 'rgba(255,255,255,0.045)' },
  LayoutSider: { color: '#16161C', borderColor: 'rgba(255,255,255,0.06)' },
  LayoutHeader: { color: '#141419', borderColor: 'rgba(255,255,255,0.06)' },
  Layout: { color: '#101014' },
  Menu: { itemIconMargin: '0 12px 0 0', itemHeight: '40px', itemNameFontSize: '13.5px' },
};
</script>

<template>
  <NConfigProvider :theme="darkTheme" :theme-overrides="themeOverrides" :locale="zhCN" :date-locale="dateZhCN">
    <NMessageProvider :duration="3000">
      <NNotificationProvider>
        <NDialogProvider>
          <Shell />
        </NDialogProvider>
      </NNotificationProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>
