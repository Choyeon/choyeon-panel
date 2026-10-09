<script setup lang="ts">
/** 键值清单卡（对标 1Panel 的「系统信息」）：左列标签、右列取值，一行一条。 */
export interface InfoItem {
  k: string;
  v: string;
  /** 版本号 / IP / 路径这类等宽更好读 */
  mono?: boolean;
}
defineProps<{ items: InfoItem[] }>();
</script>

<template>
  <dl class="infolist">
    <div v-for="it in items" :key="it.k" class="info-row">
      <dt class="info-k">{{ it.k }}</dt>
      <dd class="info-v" :class="{ mono: it.mono }" :title="it.v">{{ it.v || '—' }}</dd>
    </div>
  </dl>
</template>

<style scoped>
/* 1Panel 的 el-descriptions(:column="1", border) 里，**标签比取值更强**：
   标签 14px/#1f2329，取值 13px/#646a73，行间距靠 border-spacing: 0 5px。
   我们原来正好写反了（标签小且弱），所以这里把主次换回来。 */
.infolist { margin: 0; display: flex; flex-direction: column; }
.info-row {
  display: flex; align-items: baseline; gap: var(--space-3); min-width: 0;
  padding: 5px 0;
}
.info-row + .info-row { border-top: 1px solid var(--cp-line); }
.info-k {
  flex-shrink: 0; min-width: 5.5em; font-size: var(--fs-md); font-weight: 400;
  color: var(--cp-text-strong); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 0;
}
.info-v {
  margin: 0; min-width: 0; flex: 1; font-size: var(--fs-sm); color: var(--cp-text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.info-v.mono { font-family: var(--font-mono); font-size: var(--fs-xs); }
</style>
