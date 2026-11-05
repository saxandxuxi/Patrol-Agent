<template>
  <div class="app-shell" :class="{ 'widget-mode': isWidget || isEmbed }">
    <header v-if="!isWidget && !isEmbed" class="app-header">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true"></div>
        <div>
          <div class="app-title">道路 AI 巡检养护智能体</div>
          <div class="app-subtitle">Road Inspection &amp; Maintenance Copilot</div>
        </div>
      </div>

      <nav class="top-nav" aria-label="主导航">
        <button
          v-for="item in mainNavItems"
          :key="item.key"
          class="nav-item"
          :class="{ active: active === item.key }"
          type="button"
          @click="active = item.key"
        >
          {{ item.label }}
        </button>
        <!-- 低频配置收进齿轮，降低认知负荷（对话/看板/快捷查询是高频主显） -->
        <el-dropdown trigger="click" @command="(key) => (active = key)">
          <button class="nav-item" :class="{ active: adminKeys.includes(active) }" type="button">
            设置 ▾
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="item in adminNavItems" :key="item.key" :command="item.key">
                {{ item.label }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </nav>

      <div class="header-meta">
        <span class="status-dot"></span>
        <span>服务在线 · {{ isWidget ? '悬浮球模式' : 'Hybrid 引擎' }}</span>
      </div>
    </header>

    <button v-if="isWidget && !isEmbed" class="workbench-fab" type="button" @click="openWorkbench">
      打开完整工作台 ↗
    </button>

    <main class="app-main">
      <ChatPanel v-if="isEmbed" embedded />
      <KeepAlive v-else>
        <component :is="currentView" />
      </KeepAlive>
    </main>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

// 悬浮球模式：?mode=widget → 隐藏顶部导航、只渲染对话面板，方便被巡检平台 iframe 嵌入
// （见 docs/产品化与结果可信度整改方案.md 第七章方式一）
const query = new URLSearchParams(window.location.search)
const mode = query.get('mode')
const isWidget = mode === 'widget'
const isEmbed = mode === 'embed'
function openWorkbench() {
  window.open(window.location.pathname, '_blank')
}
import ChatPanel from './components/ChatPanel.vue'
import AdminPanel from './components/AdminPanel.vue'
import KnowledgeManager from './components/KnowledgeManager.vue'
import ModelSettings from './components/ModelSettings.vue'
import PresetQueries from './components/PresetQueries.vue'
import SchemaExplorer from './components/SchemaExplorer.vue'
import Workbench from './components/Workbench.vue'

const navItems = [
  { key: 'chat', label: '对话' },
  { key: 'workbench', label: '固定看板' },
  { key: 'presets', label: '快捷查询' },
]

// 低频的配置类页面收进右上角「设置」齿轮
const adminNavItems = [
  { key: 'knowledge', label: '知识库' },
  { key: 'schema', label: '数据库' },
  { key: 'models', label: '模型设置' },
  { key: 'admin', label: '管理' },
]
const mainNavItems = navItems
const adminKeys = adminNavItems.map((item) => item.key)

const views = {
  chat: ChatPanel,
  workbench: Workbench,
  presets: PresetQueries,
  knowledge: KnowledgeManager,
  schema: SchemaExplorer,
  models: ModelSettings,
  admin: AdminPanel,
}
const requestedView = query.get('view') || 'chat'
const active = ref(Object.hasOwn(views, requestedView) ? requestedView : 'chat')
const currentView = computed(() => views[active.value])
</script>
