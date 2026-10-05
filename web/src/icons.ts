import { h } from 'vue';
import { NIcon } from 'naive-ui';
import {
  SpeedometerOutline, AppsOutline as ApplicationsOutline, ServerOutline, CloudOutline, TimeOutline,
  FolderOpenOutline, TerminalOutline, SettingsOutline, LogoElectron, LogoPython,
  AddOutline, RefreshOutline, TrashOutline, PlayOutline,
  RefreshCircleOutline, KeyOutline, DownloadOutline, ArrowForwardOutline,
  CheckmarkCircleOutline, CloseCircleOutline, AlertCircleOutline,
  GlobeOutline, ChevronForwardOutline, DocumentTextOutline,
  SyncOutline, ShieldCheckmarkOutline, CubeOutline, PulseOutline,
  SaveOutline, CloudUploadOutline, OptionsOutline, WifiOutline,
  StopOutline, PauseOutline, ArrowBackOutline, SearchOutline,
  PencilOutline, FolderOutline, DocumentOutline,
  MenuOutline, SunnyOutline, MoonOutline, ColorPaletteOutline,
  WarningOutline, InformationCircleOutline, CopyOutline, EyeOutline,
} from '@vicons/ionicons5';

/** 全站图标按名称索引，统一 ionicons5 图标集（24x24 viewBox，避免混用 emoji）。 */
export const icons: Record<string, any> = {
  SpeedometerOutline, ApplicationsOutline, ServerOutline, CloudOutline, TimeOutline,
  FolderOpenOutline, TerminalOutline, SettingsOutline, LogoElectron, LogoPython,
  AddOutline, RefreshOutline, TrashOutline, PlayOutline, CubeOutline, PulseOutline,
  RefreshCircleOutline, KeyOutline, DownloadOutline, ArrowForwardOutline, OptionsOutline,
  CheckmarkCircleOutline, CloseCircleOutline, AlertCircleOutline, GlobeOutline,
  ChevronForwardOutline, DocumentTextOutline, SyncOutline, ShieldCheckmarkOutline,
  SaveOutline, CloudUploadOutline, WifiOutline,
  StopOutline, PauseOutline, ArrowBackOutline, SearchOutline,
  PencilOutline, FolderOutline, DocumentOutline,
  MenuOutline, SunnyOutline, MoonOutline, ColorPaletteOutline,
  WarningOutline, InformationCircleOutline, CopyOutline, EyeOutline,
};

export function icon(name: string, size = 18, color?: string) {
  return () => h(NIcon, { size, color }, { default: () => h(icons[name] || icons.SettingsOutline) });
}
