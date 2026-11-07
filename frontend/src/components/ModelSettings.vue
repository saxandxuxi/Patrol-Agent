<template>
  <section class="page-card">
    <div class="page-heading">
      <div>
        <div class="page-eyebrow">Model Provider</div>
        <h1 class="page-title">模型设置</h1>
        <p class="page-description">
          默认用 .env 里配的那几个模型（下面对应项会写明「默认（.env：xxx）」）；
          角色模型可以按「供应商/模型」跨供应商选，比如 deepseek/deepseek-chat、zhipu/glm-4.6。
        </p>
      </div>
    </div>

    <div class="settings-grid">
      <div class="panel-card catalog-card">
        <div class="card-heading">
          <div>
            <h2>可用文本模型</h2>
            <span>每 30 天自动检查一次；只显示用当前密钥实际调用成功的模型</span>
          </div>
          <el-button
            size="small"
            :loading="catalogLoading || catalog.refreshing"
            @click="refreshCatalog"
          >
            刷新模型目录
          </el-button>
        </div>
        <div class="catalog-summary">
          <span><strong>{{ catalog.models?.length || 0 }}</strong> 个已验证文本模型</span>
          <span>上次更新：{{ formatCatalogTime(catalog.updated_at) }}</span>
          <span>自动更新周期：{{ catalog.ttl_days || 30 }} 天</span>
        </div>
        <el-alert
          v-if="catalog.refreshing"
          title="正在拉取模型目录并逐个验证当前密钥的调用权限，完成后下拉框会自动更新。"
          type="info"
          :closable="false"
          show-icon
        />
        <el-alert
          v-else-if="catalog.last_error"
          :title="`模型目录刷新失败：${catalog.last_error}`"
          type="warning"
          :closable="false"
          show-icon
        />
        <p class="catalog-note">
          模型直接进入角色模型下拉框，不需要在「已保存模型」里逐条注册。
          未开通、无权限、非文本生成或不兼容 Chat Completions 的模型会自动忽略。
        </p>
      </div>

      <div class="panel-card form-card">
        <div class="card-heading">
          <h2>角色模型</h2>
          <span>生成 SQL 和知识问答可以各用各的模型</span>
        </div>
        <el-form label-position="top">
          <el-form-item label="生成 SQL 的模型">
            <el-select
              v-model="roles.sql_model"
              filterable
              allow-create
              clearable
              :placeholder="defaultLabel('sql_model')"
              style="width: 100%"
            >
              <el-option :label="defaultLabel('sql_model')" value="" />
              <el-option-group
                v-for="group in roleOptionGroups"
                :key="group.label"
                :label="group.label"
              >
                <el-option
                  v-for="item in group.items"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-option-group>
            </el-select>
            <span class="param-hint">当前生效：{{ effective('sql_model') }}</span>
          </el-form-item>
          <el-form-item label="知识问答的模型">
            <el-select
              v-model="roles.knowledge_model"
              filterable
              allow-create
              clearable
              :placeholder="defaultLabel('knowledge_model')"
              style="width: 100%"
            >
              <el-option :label="defaultLabel('knowledge_model')" value="" />
              <el-option-group
                v-for="group in roleOptionGroups"
                :key="group.label"
                :label="group.label"
              >
                <el-option
                  v-for="item in group.items"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-option-group>
            </el-select>
            <span class="param-hint">当前生效：{{ effective('knowledge_model') }}</span>
          </el-form-item>
          <el-form-item label="校验 / 纠错模型">
            <el-select
              v-model="roles.verify_model"
              filterable
              allow-create
              clearable
              :placeholder="defaultLabel('verify_model')"
              style="width: 100%"
            >
              <el-option :label="defaultLabel('verify_model')" value="" />
              <el-option-group
                v-for="group in roleOptionGroups"
                :key="group.label"
                :label="group.label"
              >
                <el-option
                  v-for="item in group.items"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-option-group>
            </el-select>
            <span class="param-hint">当前生效：{{ effective('verify_model') }}</span>
          </el-form-item>
          <el-form-item label="语义理解模型">
            <el-select
              v-model="roles.semantic_model"
              filterable
              allow-create
              clearable
              :placeholder="defaultLabel('semantic_model')"
              style="width: 100%"
            >
              <el-option :label="defaultLabel('semantic_model')" value="" />
              <el-option-group
                v-for="group in roleOptionGroups"
                :key="group.label"
                :label="group.label"
              >
                <el-option
                  v-for="item in group.items"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-option-group>
            </el-select>
            <span class="param-hint">当前生效：{{ effective('semantic_model') }}</span>
          </el-form-item>
          <el-form-item label="任务规划模型">
            <el-select
              v-model="roles.planner_model"
              filterable
              allow-create
              clearable
              :placeholder="defaultLabel('planner_model')"
              style="width: 100%"
            >
              <el-option :label="defaultLabel('planner_model')" value="" />
              <el-option-group
                v-for="group in roleOptionGroups"
                :key="group.label"
                :label="group.label"
              >
                <el-option
                  v-for="item in group.items"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-option-group>
            </el-select>
            <span class="param-hint">当前生效：{{ effective('planner_model') }}</span>
          </el-form-item>
          <el-form-item label="ReAct 调度模型">
            <el-select
              v-model="roles.controller_model"
              filterable
              allow-create
              clearable
              :placeholder="defaultLabel('controller_model')"
              style="width: 100%"
            >
              <el-option :label="defaultLabel('controller_model')" value="" />
              <el-option-group
                v-for="group in roleOptionGroups"
                :key="group.label"
                :label="group.label"
              >
                <el-option
                  v-for="item in group.items"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-option-group>
            </el-select>
            <span class="param-hint">当前生效：{{ effective('controller_model') }}</span>
          </el-form-item>
          <div class="form-actions">
            <el-button type="primary" @click="saveRoles">保存角色模型</el-button>
          </div>
        </el-form>
        <p class="role-hint">
          实测 SQL 生成用 coder 族更稳（qwen3-coder-plus 已从端点下架，改用 qwen3-coder-flash）；
          知识问答用通用大模型更稳。
          校验/纠错要判断「用户问了几个点、回复答了几个点」，用便宜的小模型会把
          明显答非所问的回复判通过，建议用强通用模型（qwen3.7-plus）；清空 = 跟随语义理解模型。
          语义理解模型管「读懂人话」的几个环节（意图分类 / 拆分 / 上下文补全 / 结果解读 /
          后置计算说明 / 数据源判断 / 反思改 SQL）——它决定后面整条链路的走向，
          建议用强通用模型（qwen3.7-plus）。清空 = 跟随默认模型。
          任务规划模型负责拆步骤和执行前契约；ReAct 调度模型只在异常时决定补证据、重试或
          停止当前分支。清空任一项 = 跟随语义理解模型，可分别换成 GLM / DeepSeek 做交叉校准。
        </p>
      </div>

      <div class="panel-card form-card">
        <div class="card-heading">
          <h2>新增 / 更新模型</h2>
          <span>保存前建议先测试连接</span>
        </div>
        <el-form :model="form" label-position="top">
          <el-form-item label="名称">
            <el-input v-model="form.name" placeholder="例如 Qwen Plus" />
          </el-form-item>
          <el-form-item label="Base URL">
            <el-input v-model="form.base_url" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" />
          </el-form-item>
          <el-form-item label="API Key">
            <el-input v-model="form.api_key" type="password" show-password />
          </el-form-item>
          <el-form-item label="模型名">
            <el-input v-model="form.model" placeholder="qwen-plus" />
          </el-form-item>
          <el-form-item label="设为默认">
            <el-switch v-model="form.is_default" />
          </el-form-item>
          <div class="form-actions">
            <el-button @click="test">测试连接</el-button>
            <el-button type="primary" @click="save">保存模型</el-button>
          </div>
        </el-form>
      </div>

      <div class="panel-card table-card">
        <div class="card-heading">
          <h2>已保存模型</h2>
          <span>默认模型用于意图理解和 SQL 生成</span>
        </div>
        <el-table :data="models" border stripe>
          <el-table-column prop="name" label="名称" min-width="140" />
          <el-table-column prop="base_url" label="Base URL" min-width="240" show-overflow-tooltip />
          <el-table-column prop="model" label="模型" min-width="120" />
          <el-table-column prop="is_default" label="默认" width="80">
            <template #default="{ row }">
              <el-tag v-if="row.is_default" type="success" effect="plain">默认</el-tag>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button link type="danger" @click="remove(row.id)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 对话页只留「沿用上轮」；这几个开关低频但会影响回答，放这里管。 -->
      <div class="panel-card form-card">
        <div class="card-heading">
          <h2>对话行为</h2>
          <span>下一轮提问生效，对话页不再显示这些开关</span>
        </div>
        <el-form label-position="top">
          <el-form-item>
            <div class="toggle-row">
              <el-switch v-model="chatOptions.enable_thinking" />
              <div>
                <strong>思考</strong>
                <p>知识问答是否开启思考模式（慢一点，但推理更完整）。</p>
              </div>
            </div>
          </el-form-item>
          <el-form-item>
            <div class="toggle-row">
              <el-switch v-model="chatOptions.enable_search" />
              <div>
                <strong>联网</strong>
                <p>知识问答是否允许联网搜索补充资料。</p>
              </div>
            </div>
          </el-form-item>
          <el-form-item>
            <div class="toggle-row">
              <el-switch v-model="chatOptions.reflect_check" />
              <div>
                <strong>反思自检</strong>
                <p>分析型回答发出前自检一遍（数字自洽、结论有数据支撑、不许编比率）。关掉省一次调用。</p>
              </div>
            </div>
          </el-form-item>
          <el-form-item>
            <div class="toggle-row">
              <el-switch v-model="chatOptions.followup_suggestions" />
              <div>
                <strong>后续建议</strong>
                <p>回答末尾主动提示你可能还想问什么；关掉回答更简洁。</p>
              </div>
            </div>
          </el-form-item>
          <el-form-item>
            <div class="toggle-row">
              <div style="flex: 1">
                <strong>确定性能力（模板 / 已验证示例）</strong>
                <p>
                  每次问我：命中现成口径先弹确认卡让你选（默认，也是唯一会执行模板的方式）；
                  不复用：全部走 AI 重新生成。看板卡片与快捷查询的"刷新"属你主动点选，不受此开关影响。
                </p>
              </div>
              <el-select v-model="chatOptions.reuse_mode" size="small" style="width: 150px">
                <el-option label="每次问我（ask）" value="ask" />
                <el-option label="不复用（off）" value="off" />
              </el-select>
            </div>
          </el-form-item>
          <el-form-item>
            <div class="toggle-row">
              <div style="flex: 1">
                <strong>自动沉淀模板（L1）</strong>
                <p>
                  把"答对了"的成功 SQL 离线挖成参数化模板（换路线/换病害/换阈值直接复用，0 次大模型）；
                  命中后仍要你在卡片上确认才会执行。
                </p>
              </div>
              <el-switch v-model="chatOptions.auto_template_enabled" />
            </div>
          </el-form-item>
          <el-form-item>
            <div class="toggle-row">
              <div style="flex: 1">
                <strong>自动挖掘新模板</strong>
                <p>
                  离线把"答对了"的成功 SQL 挖成模板：挖掘器修好前默认关。新挖出来的是"待观察"，
                  要在「管理 → 自动沉淀模板」里人工核对口径后转正，才会进对话推荐。
                </p>
              </div>
              <el-switch v-model="chatOptions.auto_template_mine_enabled" />
            </div>
          </el-form-item>
          <el-form-item>
            <div class="toggle-row">
              <div style="flex: 1">
                <strong>相似问法直达（L2）</strong>
                <p>
                  换个说法的问题先用便宜模型做一次"严格同问"判定，过了门槛才摆成候选卡；不过门槛直接走 AI。
                </p>
              </div>
              <el-switch v-model="chatOptions.rag_direct_enabled" />
            </div>
          </el-form-item>
          <el-form-item v-if="chatOptions.rag_direct_enabled">
            <div class="toggle-row">
              <div style="flex: 1">
                <strong>相似度阈值（结构同型直接摆卡）</strong>
                <p>
                  相似度 ≥ 阈值 且结构同型 → 直接摆确认卡，不再调打分模型、不看与次名分差；最终由你在卡上点确认。
                </p>
              </div>
              <el-switch v-model="chatOptions.rag_direct_fast_enabled" />
              <el-input-number
                v-model="chatOptions.rag_direct_fast_threshold"
                :min="0.5"
                :max="1"
                :step="0.01"
                size="small"
                style="width: 120px; margin-left: 8px"
              />
            </div>
          </el-form-item>
          <div class="form-actions">
            <el-button type="primary" @click="saveChatBehaviour">保存对话行为</el-button>
          </div>
        </el-form>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, onActivated, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const models = ref([])
const roleChoices = ref([])
const catalog = ref({
  models: [],
  updated_at: '',
  refreshing: false,
  stale: true,
  last_error: '',
  ttl_days: 30,
})
const catalogLoading = ref(false)
let catalogPollTimer = null
// 每个角色「跟随默认」时实际用的是哪个模型：值由后端从 .env 算出来（payload._defaults），
// 这样用户一眼能看出默认是什么，而不是只看到一个空下拉框。
const roleDefaults = ref({})

function defaultLabel(role) {
  const name = roleDefaults.value[role] || roleDefaults.value.default_model || ''
  return name ? `默认（.env：${name}）` : '默认（跟随 .env）'
}

function effective(role) {
  return roles[role] || roleDefaults.value[role] || roleDefaults.value.default_model || '-'
}

const roles = reactive({
  sql_model: '',
  knowledge_model: '',
  verify_model: '',
  semantic_model: '',
  planner_model: '',
  controller_model: '',
})

const PROVIDER_LABELS = {
  dashscope: '百炼',
  zhipu: '智谱',
  deepseek: 'DeepSeek',
  moonshot: 'Moonshot',
}

function providerLabel(value) {
  return PROVIDER_LABELS[value] || value || '其他'
}

function formatContext(value) {
  const number = Number(value || 0)
  if (number >= 1_000_000) return `${Math.round(number / 100_000) / 10}M`
  if (number >= 1000) return `${Math.round(number / 1000)}K`
  return ''
}

function catalogLabel(item) {
  const context = formatContext(item.context_window)
  const vendor = item.vendor && item.vendor !== item.gateway_provider
    ? ` · ${item.vendor}`
    : ''
  const capability = (item.capabilities || []).includes('Reasoning') ? ' · 推理' : ''
  return `${item.name} · ${item.id}${vendor}${context ? ` · ${context}` : ''}${capability}`
}

const roleOptionGroups = computed(() => {
  const groups = new Map()
  const add = (group, item) => {
    if (!groups.has(group)) groups.set(group, [])
    groups.get(group).push(item)
  }
  const seen = new Set()
  for (const model of catalog.value.models || []) {
    if (!model.role_choice || seen.has(model.role_choice)) continue
    seen.add(model.role_choice)
    add(providerLabel(model.gateway_provider), {
      label: catalogLabel(model),
      value: model.role_choice,
    })
  }
  for (const name of roleChoices.value || []) {
    if (!name || seen.has(name)) continue
    seen.add(name)
    const prefix = String(name).includes('/') ? String(name).split('/', 1)[0] : '原有 / 手动配置'
    add(providerLabel(prefix), { label: name, value: name })
  }
  return [...groups.entries()].map(([label, items]) => ({ label, items }))
})
const chatOptions = reactive({
  enable_thinking: true,
  enable_search: true,
  reflect_check: true,
  followup_suggestions: true,
  reuse_mode: 'ask',
  auto_template_enabled: true,
  auto_template_mine_enabled: false,
  rag_direct_enabled: true,
  rag_direct_threshold: 0.75,
  rag_direct_margin: 0.08,
  rag_direct_fast_enabled: true,
  rag_direct_fast_threshold: 0.85,
})
const form = reactive({
  name: '',
  base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  api_key: '',
  model: 'qwen-plus',
  is_default: false,
})

async function load() {
  try {
    models.value = await api.models()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

function clearCatalogPoll() {
  if (catalogPollTimer) {
    window.clearTimeout(catalogPollTimer)
    catalogPollTimer = null
  }
}

function scheduleCatalogPoll() {
  clearCatalogPoll()
  catalogPollTimer = window.setTimeout(async () => {
    await loadCatalog()
  }, 4000)
}

async function loadCatalog(force = false) {
  catalogLoading.value = true
  try {
    const data = force ? await api.refreshModelCatalog() : await api.modelCatalog()
    catalog.value = {
      ...catalog.value,
      ...data,
      models: Array.isArray(data.models) ? data.models : [],
    }
    if (data.refreshing) scheduleCatalogPoll()
    else clearCatalogPoll()
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    catalogLoading.value = false
  }
}

async function refreshCatalog() {
  clearCatalogPoll()
  await loadCatalog(true)
}

function formatCatalogTime(value) {
  if (!value) return '尚未更新'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

async function loadRoles() {
  try {
    const data = await api.chatSettings()
    // 后端给的「跟随默认时实际用哪个模型」（来自 .env），用来把默认值显示出来
    roleDefaults.value = data._defaults || {}
    roles.sql_model = data.sql_model || ''
    roles.knowledge_model = data.knowledge_model || ''
    roles.verify_model = data.verify_model || ''
    roles.semantic_model = data.semantic_model || ''
    roles.planner_model = data.planner_model || ''
    roles.controller_model = data.controller_model || ''
    const saved = models.value.map((item) => item.model).filter(Boolean)
    roleChoices.value = [...new Set([...(data.model_choices || []), ...saved])]
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function saveRoles() {
  try {
    await api.updateChatSettings({
      sql_model: roles.sql_model || '',
      knowledge_model: roles.knowledge_model || '',
      verify_model: roles.verify_model || '',
      semantic_model: roles.semantic_model || '',
      planner_model: roles.planner_model || '',
      controller_model: roles.controller_model || '',
    })
    ElMessage.success('已保存角色模型，下一次提问生效')
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function loadChatBehaviour() {
  try {
    const data = await api.chatSettings()
    chatOptions.enable_thinking = data.enable_thinking !== false
    chatOptions.enable_search = data.enable_search !== false
    chatOptions.reflect_check = data.reflect_check !== false
    chatOptions.followup_suggestions = data.followup_suggestions !== false
    chatOptions.reuse_mode = data.reuse_mode || 'ask'
    chatOptions.auto_template_enabled = data.auto_template_enabled !== false
    chatOptions.auto_template_mine_enabled = data.auto_template_mine_enabled === true
    chatOptions.rag_direct_enabled = data.rag_direct_enabled !== false
    chatOptions.rag_direct_threshold = Number(data.rag_direct_threshold ?? 0.75)
    chatOptions.rag_direct_margin = Number(data.rag_direct_margin ?? 0.08)
    chatOptions.rag_direct_fast_enabled = data.rag_direct_fast_enabled !== false
    chatOptions.rag_direct_fast_threshold = Number(data.rag_direct_fast_threshold ?? 0.85)
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function saveChatBehaviour() {
  try {
    await api.updateChatSettings({ ...chatOptions })
    ElMessage.success('已保存对话行为，下一次提问生效')
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function test() {
  try {
    const res = await api.testModel(form)
    ElMessage.success(res.message || '连接成功')
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function save() {
  try {
    await api.saveModel(form)
    ElMessage.success('已保存')
    form.api_key = ''
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function remove(id) {
  try {
    await api.deleteModel(id)
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

onMounted(async () => {
  await loadCatalog()
  await load()
  await loadRoles()
  await loadChatBehaviour()
})

onActivated(loadCatalog)

onBeforeUnmount(clearCatalogPoll)
</script>

<style scoped>
.role-hint {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.settings-grid {
  display: grid;
  grid-template-columns: minmax(320px, 420px) minmax(0, 1fr);
  gap: 18px;
}

.form-card,
.table-card {
  padding: 20px;
}

.catalog-card {
  grid-column: 1 / -1;
  padding: 20px;
}

.catalog-card .card-heading {
  align-items: flex-start;
}

.catalog-card .card-heading > div {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.catalog-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  margin-bottom: 12px;
  color: var(--ink-soft);
  font-size: 12px;
}

.catalog-summary strong {
  color: var(--blue);
  font-family: var(--font-mono);
  font-size: 16px;
}

.catalog-note {
  margin: 10px 0 0;
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.65;
}

.card-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 18px;
}

/* 角色模型下拉下面那行「当前生效：xxx」 */
.param-hint {
  display: block;
  margin-top: 4px;
  color: var(--muted, #7a8699);
  font-size: 12px;
}

.card-heading h2 {
  margin: 0;
  font-size: 16px;
}

.card-heading span {
  color: var(--ink-soft);
  font-size: 12px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.toggle-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.toggle-row strong {
  display: block;
  font-size: 14px;
}

.toggle-row p {
  margin: 2px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}

@media (max-width: 1000px) {
  .settings-grid {
    grid-template-columns: 1fr;
  }
}
</style>
