<template>
  <div class="workspace" :class="{ 'right-collapsed': rightCollapsed, embedded }">
    <section class="chat-pane panel-card" :style="{ flexBasis: leftWidth + '%' }">
      <div v-if="embedded" class="embed-head">
        <div>
          <strong>巡检助手</strong>
          <span>问数据，不替平台做操作</span>
        </div>
        <div class="embed-head-actions">
          <button type="button" title="最近对话" @click="embeddedHistoryOpen = true">◷</button>
          <button
            type="button"
            class="embed-help"
            title="查看助手能力说明"
            @click="embedHelpOpen = !embedHelpOpen"
          >?</button>
        </div>
        <div v-if="embedHelpOpen" class="embed-help-card">
          我负责数据库查询：可以问病害数量、分布、明细、趋势、任务和规范。固定看板与快捷查询会打开完整助手。
        </div>
      </div>
      <div v-if="!embedded" class="pane-heading">
        <div>
          <div class="pane-eyebrow">自然语言查询</div>
          <h2 class="pane-title">对话</h2>
        </div>
        <div class="pane-actions">
          <!-- 思考 / 联网 / 反思自检 / 后续建议 都挪到「模型设置 → 对话行为」了，
               对话页只留最高频的「沿用上轮」，别把操作区挤满。 -->
          <label
            class="switch-label"
            title="多轮对话：追问会沿用上一条条件。单轮独立：只隔离上下文，旧问题不会影响新问题，但对话记录仍会保存。"
          >
            <el-switch
              v-model="chatOptions.inherit_context"
              size="small"
              @change="saveChatOptions"
            />
            <span>{{ chatOptions.inherit_context ? '多轮对话' : '单轮独立' }}</span>
          </label>
          <el-select
            v-model="chatOptions.engine_mode"
            size="small"
            style="width: 132px"
            @change="saveChatOptions"
          >
            <el-option label="引擎：混合" value="hybrid" />
            <el-option label="引擎：仅 Wren" value="wren" />
          </el-select>
          <!-- 收藏夹下线：常用的题在「固定看板」点刷新（零生成），或在「快捷查询」换参数 -->
          <el-button text @click="newConversation">新对话</el-button>
          <el-button text @click="toggleRight">
            {{ rightCollapsed ? '展开结果' : '收起结果' }}
          </el-button>
        </div>
      </div>

      <div class="chat-body">
        <aside v-if="!embedded" class="conversation-list">
          <div class="conversation-head">
            <span>对话记录</span>
            <button type="button" class="conv-manage" @click="toggleManage">
              {{ managing ? '完成' : '管理' }}
            </button>
          </div>
          <div v-if="managing" class="conversation-actions">
            <el-checkbox
              :model-value="allSelected"
              :indeterminate="someSelected"
              @change="toggleAll"
            >
              全选
            </el-checkbox>
            <span class="conv-count">已选 {{ selected.length }}</span>
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="!selected.length"
              @click="removeSelected"
            >
              删除选中
            </el-button>
            <el-button size="small" type="danger" :disabled="!conversations.length" @click="removeAll">
              清空全部
            </el-button>
          </div>
          <button
            v-for="item in conversations"
            :key="item.id"
            type="button"
            class="conversation-item"
            :class="{ active: item.id === sessionId }"
            @click="managing ? toggleSelect(item.id) : openConversation(item.id)"
          >
            <span v-if="managing" class="conv-check">
              {{ selected.includes(item.id) ? '☑' : '☐' }}
            </span>
            <span class="conv-title">{{ item.title || '新对话' }}</span>
            <span class="conv-meta">{{ item.message_count }} 条 · {{ formatTime(item.updated_at) }}</span>
            <span
              v-if="!managing"
              class="conv-delete"
              title="删除这个对话"
              @click.stop="removeConversation(item.id)"
            >
              ×
            </span>
          </button>
          <p v-if="!conversations.length" class="conversation-empty">还没有对话记录</p>
        </aside>

        <div class="message-area">
          <div ref="listRef" class="message-list">
            <div v-if="messages.length === 0" class="chat-empty">
              <div class="milepost-mark">
                <span>K0</span>
                <i></i>
                <span>K∞</span>
              </div>
              <h3>从一段路、一处病害开始</h3>
              <p>可以问数量、分布、明细，也可以问养护规范和处置建议。</p>
              <div class="example-chips">
                <button
                  v-for="example in examples"
                  :key="example"
                  type="button"
                  @click="question = example"
                >
                  {{ example }}
                </button>
              </div>
            </div>

            <div
              v-for="(msg, idx) in messages"
              :key="idx"
              class="message-row"
              :class="{ 'is-user': msg.role === 'user' }"
            >
              <div class="message-avatar">{{ msg.role === 'user' ? '你' : 'AI' }}</div>
              <div class="message-body">
                <div class="message-meta">
                  <span>{{ msg.role === 'user' ? '你' : '巡检助手' }}</span>
                  <time>{{ msg.time }}</time>
                  <!-- 这条回答从"提出问题"到"出答案"实际用了多久（确认卡的两段也算在一起） -->
                  <span v-if="msg.role === 'assistant' && msg.elapsed_ms" class="message-cost">
                    用时 {{ formatElapsed(msg.elapsed_ms) }}
                  </span>
                </div>
                <div class="message-bubble markdown-body" v-html="renderMarkdown(msg.content)"></div>
                <div
                  v-if="msg.role === 'assistant' && msg.review_gate"
                  class="review-gate-card"
                >
                  <div class="review-gate-head">
                    <span class="review-gate-mark">?</span>
                    <div>
                      <strong>{{ msg.review_gate.title }}</strong>
                      <p>{{ msg.review_gate.reason }}</p>
                    </div>
                  </div>
                  <div
                    v-for="node in msg.review_gate.nodes || []"
                    :key="node.node_id"
                    class="review-gate-node"
                  >
                    <div class="review-gate-question">
                      <span>{{ node.node_id }}</span>
                      {{ node.question }}
                    </div>
                    <div class="review-gate-result">
                      返回 {{ node.result_sample?.length || 0 }} 行样本
                      <template v-if="node.columns?.length">
                        · 字段：{{ node.columns.join('、') }}
                      </template>
                    </div>
                    <el-collapse v-if="node.sql" class="review-gate-evidence">
                      <el-collapse-item title="查看 SQL 和疑点" :name="`review-${node.node_id}`">
                        <p class="review-gate-reason">{{ node.reason }}</p>
                        <pre>{{ node.sql }}</pre>
                        <pre v-if="node.result_sample?.length">{{ JSON.stringify(node.result_sample, null, 2) }}</pre>
                      </el-collapse-item>
                    </el-collapse>
                  </div>
                  <div class="review-gate-actions">
                    <el-button
                      type="primary"
                      size="small"
                      :loading="msg.review_busy"
                      @click="decideReview(msg, 'accept')"
                    >
                      回答正确，继续
                    </el-button>
                    <el-button
                      size="small"
                      :loading="msg.review_busy"
                      @click="decideReview(msg, 'reject')"
                    >
                      回答错误，重新规划
                    </el-button>
                  </div>
                </div>
                <!-- 执行链路：像看 skill 的步骤一样看这次回答的数据流——
                     每步的问法 / 类型 / 走的引擎 / 取到多少行、多少图，以及链式题的回填键值。
                     结构化数据来自后端的 payload.process；**默认折叠**，标题里先给一眼可见的线路。 -->
                <el-collapse
                  v-if="msg.role === 'assistant' && (msg.process?.steps?.length || hasChainValues(msg.process) || msg.workflow_events?.length)"
                  class="pipeline-collapse"
                >
                  <el-collapse-item
                    v-if="msg.process?.steps?.length || hasChainValues(msg.process)"
                    :title="processTitle(msg.process)"
                    name="pipeline"
                  >
                    <ol class="pipeline-steps">
                      <li v-for="(step, idx) in msg.process?.steps || []" :key="idx" class="pipeline-step">
                        <span class="pipeline-index">{{ idx + 1 }}</span>
                        <span class="pipeline-type">{{ stepTypeLabel(step.type) }}</span>
                        <span class="pipeline-engine" :class="`engine-${step.engine || 'none'}`">
                          {{ step.engine || '未执行' }}
                        </span>
                        <span class="pipeline-count">
                          {{ step.rows ?? 0 }} 行<template v-if="step.images"> · {{ step.images }} 张图</template>
                        </span>
                        <span v-if="step.wants_images" class="pipeline-tag">要图</span>
                        <span class="pipeline-q" :title="step.question">{{ step.question }}</span>
                        <span
                          v-if="step.error || step.failure_code"
                          class="pipeline-error"
                          :title="step.error"
                        >
                          {{ step.failure_code || '执行失败' }}<template v-if="step.error"> · {{ step.error }}</template>
                        </span>
                      </li>
                    </ol>
                    <div v-if="hasChainValues(msg.process)" class="pipeline-chain">
                      <span class="pipeline-chain-label">链路回填</span>
                      <span
                        v-for="(value, key) in msg.process.chain_values"
                        :key="key"
                        class="pipeline-chip"
                      >{{ key }} → {{ value }}</span>
                    </div>
                  </el-collapse-item>
                  <el-collapse-item
                    v-if="msg.workflow_events?.length"
                    :title="`工作流过程（${msg.workflow_events.length} 条事件）`"
                    name="workflow"
                  >
                    <div class="workflow-event-list">
                      <div
                        v-for="event in msg.workflow_events"
                        :key="event.seq || `${event.type}-${event.timestamp}`"
                        class="live-event"
                        :class="`live-${event.type}`"
                      >
                        <span class="live-event-icon">{{ liveEventIcon(event) }}</span>
                        <div class="live-event-body">
                          <div class="live-event-title">{{ liveEventTitle(event) }}</div>
                          <div v-if="liveEventDetail(event)" class="live-event-detail">
                            {{ liveEventDetail(event) }}
                          </div>
                        </div>
                      </div>
                    </div>
                  </el-collapse-item>
                </el-collapse>
                <el-collapse v-if="msg.role === 'assistant' && msg.thinking" class="thinking-box">
                  <el-collapse-item title="查看思考过程" name="thinking">
                    <pre class="thinking-text">{{ msg.thinking }}</pre>
                  </el-collapse-item>
                </el-collapse>
                <div
                  v-if="msg.role === 'assistant' && msg.references?.length"
                  class="answer-references"
                >
                  <span class="ref-label">引用</span>
                  <button
                    v-for="(item, refIdx) in msg.references"
                    :key="refIdx"
                    type="button"
                    class="ref-chip"
                    @click="focusReference"
                  >
                    {{ item.document_keyword || item.document_id || 'RAGFlow 文档' }}
                  </button>
                </div>
                <div v-if="msg.role === 'assistant' && msg.result_id" class="message-actions">
              <!-- 固化 = 入示例库（approve）：之后在「固定看板」点刷新直接重跑这条 SQL，不再生成 -->
              <el-button text size="small" @click="pinToBoard(msg)">⭐ 固化到看板</el-button>
              <el-button text size="small" @click="sendFeedback(msg, 'good')">答对了</el-button>
              <el-button text size="small" @click="sendFeedback(msg, 'bad')">答错了</el-button>
                  <button
                    type="button"
                    class="result-link"
                    :class="{ active: activeResultId === msg.result_id }"
                    @click="showResult(msg.result_id)"
                  >
                    {{ activeResultId === msg.result_id ? '正在查看这次结果' : '查看这次结果' }}
                  </button>
                </div>
                <!-- 选择式澄清（方案第七章 §7）：按钮里带回执，不用再打字 -->
                <div v-if="msg.choices?.length" class="choice-row">
                  <el-button
                    v-for="choice in msg.choices"
                    :key="choice.key"
                    size="small"
                    type="primary"
                    plain
                    @click="pickChoice(msg, choice)"
                  >
                    {{ choice.label }}
                  </el-button>
                </div>
              </div>
            </div>

            <div v-if="loading" class="message-row">
              <div class="message-avatar">AI</div>
              <div class="message-body">
                <div class="message-meta"><span>巡检助手</span><time>分析中</time></div>
                <div v-if="liveEvents.length" class="live-workflow">
                  <div class="live-workflow-head">
                    <span class="live-pulse"></span>
                    正在执行工作流
                  </div>
                  <div
                    v-for="event in liveEvents"
                    :key="event.seq || `${event.type}-${event.timestamp}`"
                    class="live-event"
                    :class="`live-${event.type}`"
                  >
                    <span class="live-event-icon">{{ liveEventIcon(event) }}</span>
                    <div class="live-event-body">
                      <div class="live-event-title">{{ liveEventTitle(event) }}</div>
                      <div v-if="liveEventDetail(event)" class="live-event-detail">
                        {{ liveEventDetail(event) }}
                      </div>
                    </div>
                  </div>
                </div>
                <div v-else class="message-bubble typing">
                  <span></span><span></span><span></span>
                </div>
              </div>
            </div>

            <!-- 能力确认卡：当成一条助手消息卡，跟着对话流滚动，不顶输入框 -->
            <div
              v-if="capabilityChoices.length"
              class="message-row capability-row"
              @keydown.enter.prevent="confirmCapability"
            >
              <div class="message-avatar">AI</div>
              <div class="message-body">
                <div class="decision-card">
                  <div class="decision-head">
                    <span class="decision-icon" aria-hidden="true">⌘</span>
                    <div>
                      <div class="decision-title">选择查询口径</div>
                      <div class="decision-sub">
                        <template v-if="stepCount > 1">
                          这条问题可以拆成 {{ stepCount }} 步。下面这些口径里，有的能一次答对整条问题
                          （覆盖全部 {{ stepCount }} 步），有的只覆盖其中一部分——选中后我会如实说明覆盖多少；
                          覆盖不全或都不合适，就让 AI 用 Wren 查。
                        </template>
                        <template v-else>
                        找到 {{ capabilityChoices.length }} 个现成口径，确认后直接查（不重新生成 SQL）；
                        不对就让 AI 自己查。
                        </template>
                      </div>
                    </div>
                  </div>

                  <div class="cand-list">
                    <label
                      v-for="item in capabilityChoices"
                      :key="item.kind + ':' + item.id"
                      class="cand"
                      :class="{
                        active: capabilityPick === item.kind + ':' + item.id,
                        warn: item.warn,
                      }"
                    >
                      <input
                        v-model="capabilityPick"
                        class="cand-radio"
                        type="radio"
                        :value="item.kind + ':' + item.id"
                      />
                      <span class="cand-mark" aria-hidden="true"></span>
                      <span class="cand-body">
                        <span class="cand-title">
                          <em class="src-badge" :data-src="item.kind">{{ sourceLabel(item.kind) }}</em>
                          <span v-if="item.recommended" class="pill pill-green">推荐</span>
                          <span v-if="item.warn" class="pill pill-amber">口径存疑</span>
                          {{ item.title }}
                        </span>
                        <span class="cand-chips">
                          <em v-for="chip in scopeChips(item.scope)" :key="chip" class="chip">{{ chip }}</em>
                        </span>
                        <span v-if="item.recommended && item.reasons?.length" class="cand-why">
                          {{ item.reasons.join('；') }}
                        </span>
                        <span v-if="(item.slots || []).length" class="cand-params">
                          <span class="cand-params-label">参数</span>
                          <span class="cand-params-grid">
                            <span v-for="slot in item.slots || []" :key="slot.name" class="cand-param">
                              <span class="cand-param-label">
                                {{ slot.label }}
                                <em v-if="slot.missing" class="pill pill-amber">待补</em>
                              </span>
                              <el-select
                                v-if="slot.editable && (slot.options || []).length"
                                :model-value="slotValueOf(item, slot)"
                                size="small"
                                filterable
                                @update:model-value="(v) => setSlotValue(item, slot, v)"
                                @click.stop
                              >
                                <el-option v-for="opt in slot.options" :key="opt.value" :label="opt.label" :value="opt.value" />
                              </el-select>
                              <!-- 没有候选值的槽（时间范围 / 桩号区间 / 设备号）用输入框：
                                   以前一律渲染成空下拉，用户既看不到当前值也改不了。 -->
                              <el-input
                                v-else-if="slot.editable"
                                :model-value="slotValueOf(item, slot)"
                                size="small"
                                :placeholder="slot.name === 'time_range' ? '2026-08 或 2026-08-01 ~ 2026-09-01' : (slot.name === 'pile_range' ? 'K0+800 ~ K0+900' : '')"
                                @update:model-value="(v) => setSlotValue(item, slot, v)"
                                @click.stop
                              />
                              <strong v-else>{{ slot.value }}</strong>
                            </span>
                          </span>
                        </span>
                        <span class="cand-sql" @click.stop>
                          <button type="button" class="sql-toggle" @click="toggleSql(item)">
                            {{ sqlOpen[item.kind + ':' + item.id] ? '收起 SQL' : '查看 SQL' }}
                          </button>
                          <span v-if="sqlOpen[item.kind + ':' + item.id]" class="sql-box">
                            <button type="button" class="sql-copy" @click="copySql(item.sql_preview)">
                              复制
                            </button>
                            <code>{{ item.sql_preview }}</code>
                          </span>
                        </span>
                      </span>
                    </label>
                  </div>

                  <div class="cand-actions">
                    <!-- 注意必须写成 confirmCapability()：写成 @click="confirmCapability" 时 Vue 会把
                         点击事件对象当第一个参数传进去，forceAi 恒为真 → 永远发 capability_id="ai"，
                         结果"用这口径查询"实际走的是让 AI 重新查（实测跳到 Wren 语义层）。 -->
                    <el-button
                      size="small"
                      type="primary"
                      :loading="submitting"
                      :disabled="submitting"
                      @click="confirmCapability()"
                    >
                      用这口径查询
                    </el-button>
                    <el-button
                      size="small"
                      plain
                      :disabled="submitting"
                      @click="confirmCapability(true)"
                    >
                      让 AI 自己查
                    </el-button>
                    <el-button size="small" text @click="cancelCapability">取消</el-button>
                  </div>
                </div>

                <!-- 「按步骤答 / 每步选口径」已删除：复合题只有整题卡（上面那张）+ Wren 兜底 -->
              </div>
            </div>
          </div>

          <div v-if="confirmPrompt && !capabilityChoices.length" class="confirm-bar">
            <span class="confirm-text">{{ confirmPrompt }}</span>
            <el-button size="small" type="primary" @click="confirmQuery">确认查询</el-button>
            <el-button size="small" @click="rejectQuery">口径不对，我改</el-button>
          </div>

        <div class="composer">
            <div class="composer-hint">
              <span>Enter 发送</span>
              <span>Shift + Enter 换行</span>
              <button type="button" class="ai-direct" :disabled="loading" @click="sendDirect">
                智能回答（跳过模板，直接让 AI 查）
              </button>
            </div>
            <div class="composer-box">
              <el-input
                v-model="question"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 4 }"
                resize="none"
                placeholder="例如：宝安大道西乡段 K0+000-K1+000 有多少处泛油？"
                @keydown.enter.exact.prevent="send"
              />
              <el-button class="send-button" type="primary" :loading="loading" circle @click="send">
                <span v-if="!loading">↑</span>
              </el-button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <div v-if="!embedded" class="split-handle" @mousedown="startResize" @dblclick="resetResize">
      <span></span>
    </div>

    <ResultPanel
      v-if="!embedded"
      ref="resultPanelRef"
      :result="result"
      :images="result?.images || []"
      class="result-pane"
    />

    <el-drawer
      v-if="embedded"
      v-model="resultDrawerOpen"
      class="embed-result-drawer"
      direction="rtl"
      size="100%"
      :with-header="false"
    >
      <div class="embed-history-head">
        <strong>结果详情</strong>
        <button type="button" aria-label="关闭" @click="resultDrawerOpen = false">×</button>
      </div>
      <ResultPanel
        ref="resultPanelRef"
        :result="result"
        :images="result?.images || []"
      />
    </el-drawer>

    <el-drawer
      v-if="embedded"
      v-model="embeddedHistoryOpen"
      class="embed-history-drawer"
      direction="ltr"
      size="100%"
      :with-header="false"
    >
      <div class="embed-history-head">
        <strong>最近对话</strong>
        <button type="button" aria-label="关闭" @click="embeddedHistoryOpen = false">×</button>
      </div>
      <button
        v-for="item in conversations"
        :key="item.id"
        type="button"
        class="conversation-item"
        :class="{ active: item.id === sessionId }"
        @click="selectEmbeddedConversation(item.id)"
      >
        <span class="conv-title">{{ item.title || '历史对话' }}</span>
        <span class="conv-meta">{{ item.message_count }} 条 · {{ formatTime(item.updated_at) }}</span>
      </button>
      <p v-if="!conversations.length" class="conversation-empty">还没有对话记录</p>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import ResultPanel from './ResultPanel.vue'

const props = defineProps({
  embedded: { type: Boolean, default: false },
})
const question = ref('')
const messages = ref([])
const result = ref(null)
const resultPanelRef = ref(null)
const sessionId = ref(
  props.embedded
    ? crypto.randomUUID()
    : localStorage.getItem('inspection_session_id') || crypto.randomUUID(),
)

function persistSessionId() {
  if (!props.embedded) localStorage.setItem('inspection_session_id', sessionId.value)
}

persistSessionId()
const loading = ref(false)
const liveEvents = ref([])
// 确认卡提交锁：点一次就锁住，本次请求回来前不能再点（防连点发两次、防重复气泡）
const submitting = ref(false)
const listRef = ref(null)
const leftWidth = ref(50)
const rightCollapsed = ref(false)
const embedded = computed(() => props.embedded)
const resultDrawerOpen = ref(false)
const embeddedHistoryOpen = ref(false)
const embedHelpOpen = ref(false)
const conversations = ref([])
const chatOptions = ref({
  enable_thinking: true,
  enable_search: true,
  inherit_context: true,
  reflect_check: true,
  followup_suggestions: true,
  engine_mode: 'wren',
})
const activeResultId = ref('')
const confirmPrompt = ref('')
let confirmedNext = false
const pendingQuestion = ref('')
let conversationEpoch = 0
let activeRequestId = 0
let messageLoadEpoch = 0
// 能力确认卡：候选列表 / 当前选中 / 用户改过的槽位值 / 暂存 token
const capabilityChoices = ref([])
const capabilityPick = ref('')
const capabilityToken = ref('')
// 复合题的整题卡：只用于如实说明"这条问题其实分几步"（不再有按步骤选的交互）
const stepCount = ref(0)
const slotValues = ref({})
const sqlOpen = ref({})

const exampleGroups = [
  { label: '病害统计', items: ['宝安大道西乡段泛油有多少处？', '西乡段各病害类型分别有多少处？', '全库面积最大的病害在哪条路？'] },
  { label: '巡检任务', items: ['7月份西乡段巡检了几次？每次发现多少处？', '哪次巡检发现的病害最多？'] },
  { label: '路线对比', items: ['各路线病害密度排名', '西乡段和恒明珠7月各多少处病害？'] },
  { label: '养护分析', items: ['结合数据给西乡段一个整体评价', '纵向裂缝的养护处置规范是什么？'] },
]
const examples = computed(() =>
  props.embedded
    ? [
        '11月2日长度大于0.5m的裂缝长度有多少？',
        '二连浩特-广州各病害类型分别有多少处？',
        '任务编号 XJJL202610310000 检出的病害，按路线分别有多少处？',
        '西乡段泛油哪个星期最多？把那周的泛油明细列出来',
      ]
    : exampleGroups.flatMap((group) => group.items),
)

const nowText = () => new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })

// 执行链路里的步骤类型 → 中文（带原因时也看得懂这一步在干什么）
const STEP_TYPE_LABELS = {
  data: '数据查询',
  aggregate: '聚合测算',
  distribution: '分布统计',
  image: '取图',
  knowledge: '规范依据',
  analysis: '综合分析',
  post_calc: '后置计算',
}

function stepTypeLabel(type) {
  return STEP_TYPE_LABELS[type] || type || '步骤'
}

function hasChainValues(process) {
  return Boolean(process && process.chain_values && Object.keys(process.chain_values).length)
}

// 折叠标题：一眼看到"这次走了什么线路"——几步、每步什么引擎（模板/Wren/未执行）
function processTitle(process) {
  const steps = process?.steps || []
  if (!steps.length) return '执行链路'
  const engines = steps.map((step) => step.engine || '未执行').join(' → ')
  return `执行链路（${steps.length} 步：${engines}）`
}

function liveEventIcon(event) {
  return {
    phase: '◈',
    thought: '◇',
    action: '↳',
    observation: '✓',
    clarify: '?',
    final: '●',
    error: '!',
    done: '·',
  }[event.type] || '·'
}

const TOOL_LABELS = {
  query_data: '数据查询',
  fetch_images: '图片查询',
  execute_sql: 'SQL 执行',
  ask_user: '询问用户',
  retry_atomic: '小工具纠偏',
  skip_step: '跳过步骤',
}

function toolLabel(name) {
  return TOOL_LABELS[name] || name || '未知工具'
}

function compactEventValue(value) {
  if (value == null || value === '') return ''
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > 120 ? `${text.slice(0, 117)}...` : text
}

function liveEventTitle(event) {
  if (event.type === 'phase') return event.title || '执行阶段'
  if (event.type === 'thought') return '思考'
  if (event.type === 'action') {
    const layer =
      event.layer === 'atomic'
        ? '小工具'
        : event.layer === 'controller'
          ? '纠偏决策'
          : event.layer === 'sql'
            ? 'SQL'
            : '主工具'
    return `${layer}：${toolLabel(event.tool)}`
  }
  if (event.type === 'observation') {
    const rows = event.row_count ?? event.rows ?? 0
    return event.ok ? `${toolLabel(event.tool)}完成 · ${rows} 行` : `${toolLabel(event.tool)}异常`
  }
  if (event.type === 'clarify') return '等待确认查询口径'
  if (event.type === 'final') return '最终结果已生成'
  if (event.type === 'error') return '执行失败'
  if (event.type === 'done') return '工作流结束'
  return event.type || '事件'
}

function liveEventDetail(event) {
  if (event.type === 'thought') return event.text || ''
  if (event.type === 'action') {
    const parts = []
    const question = event.sub_question || event.args?.question || ''
    if (question) parts.push(question)
    if (Array.isArray(event.values) && event.values.length) {
      parts.push(`回填键：${event.values.join('、')}`)
    }
    const produces = event.args?.produces || event.produces || []
    if (Array.isArray(produces) && produces.length) {
      parts.push(`需产出：${produces.map(compactEventValue).join('、')}`)
    }
    if (event.args?.kind) parts.push(`数据源：${event.args.kind}`)
    if (event.args?.sql_chars) parts.push(`SQL ${event.args.sql_chars} 字符`)
    if (event.args?.wren_first) parts.push('优先走 Wren')
    return parts.join(' · ')
  }
  if (event.type === 'observation') {
    const parts = []
    if (event.summary) parts.push(event.summary)
    if (event.produced && Object.keys(event.produced).length) {
      parts.push(
        `产出：${Object.entries(event.produced)
          .map(([key, value]) => `${key}=${compactEventValue(value)}`)
          .join('，')}`,
      )
    }
    if (event.truncated) parts.push('结果已截断')
    if (event.empty) parts.push('空结果')
    if (event.duration_ms != null) parts.push(`${event.duration_ms} ms`)
    return parts.join(' · ')
  }
  if (event.type === 'error') return event.message || ''
  return ''
}

// 本轮提问的累计系统耗时（毫秒）：确认卡会分两次请求，两段都算在一起。
// 只计请求往返时间，不含用户在确认卡上做决定的那段时间。
let turnElapsedMs = 0

// "用时 3.2 秒" / 超过一分钟给"1 分 05 秒"
function formatElapsed(ms) {
  const seconds = Math.max(0, ms) / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds - minutes * 60)
  return `${minutes} 分 ${String(rest).padStart(2, '0')} 秒`
}

// marked 的删除线规则是 /^(~~?)/——**单个 ~ 也会被当成删除线**，
// 而我们的桩号/时间区间就用 `~` 当分隔符（K0+000~K5+100），整段会被划掉。
// 渲染前把代码块/行内代码之外的 `~` 转义掉，避免数据文本被当成 markdown 语法。
function escapeTildes(markdown) {
  return markdown
    .split(/(```[\s\S]*?```|`[^`]*`)/g)
    .map((part, index) => (index % 2 === 1 ? part : part.replace(/~/g, '\\~')))
    .join('')
}

function renderMarkdown(text) {
  if (!text) return ''
  return DOMPurify.sanitize(marked.parse(escapeTildes(String(text))))
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value).slice(5, 16)
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function scrollToBottom() {
  nextTick(() => {
    if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight
  })
}

async function loadConversations() {
  try {
    conversations.value = await api.conversations(50)
  } catch {
    // 列表拉取失败不影响对话
  }
}

async function loadChatOptions() {
  try {
    chatOptions.value = await api.chatSettings()
  } catch {
    // 用默认值
  }
}

async function saveChatOptions() {
  try {
    chatOptions.value = await api.updateChatSettings({ ...chatOptions.value })
    if (!chatOptions.value.inherit_context) {
      // 切到单轮独立时只清上下文，不删除已保存的对话记录。
      await api.clearSession(sessionId.value)
      ElMessage.success('已切换为单轮独立模式，仅隔离上下文，历史记录仍保留')
    } else {
      ElMessage.success('已切换为多轮对话模式')
    }
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function loadConversationMessages(id) {
  const loadEpoch = ++messageLoadEpoch
  try {
    const detail = await api.conversation(id)
    if (loadEpoch !== messageLoadEpoch || sessionId.value !== id) return
    messages.value = (detail.messages || []).map((item) => ({
      role: item.role,
      content: item.content,
      time: String(item.created_at || '').slice(11, 16),
      references: item.references || [],
      thinking: item.thinking || '',
      result_id: item.result_id || '',
    }))
    scrollToBottom()
  } catch (err) {
    if (loadEpoch !== messageLoadEpoch || sessionId.value !== id) return
    ElMessage.error(err.message)
  }
}

// 固化到看板 = approve：这条问答入示例库（= 默认收藏），看板出现卡片，
// 以后点刷新直接重跑这条 SQL，不再走意图/选表/生成 SQL（方案第二、七章）。
async function pinToBoard(msg) {
  try {
    const saved = await api.approveResult(msg.result_id)
    await api.reportFeedback({
      question: questionOf(msg),
      sql: msg.sql || '',
      reply: msg.content || '',
      comment: '固化到看板',
      kind: 'good',
    })
    ElMessage.success(
      saved?.deduped
        ? '这个问题示例库里已经有了，已把它标成"已确认"（没有重复存）'
        : '已固化到「固定看板」，以后点刷新就直接出最新数',
    )
  } catch (err) {
    ElMessage.error(err.message)
  }
}

function questionOf(msg) {
  const index = messages.value.indexOf(msg)
  for (let i = index; i >= 0; i -= 1) {
    if (messages.value[i]?.role === 'user') return messages.value[i].content || ''
  }
  return ''
}

async function pickChoice(msg, choice) {
  if (!msg.asked) return
  msg.choices = []
  await sendMessage(msg.asked, choice.use_saved ?? null)
}

// 答对/答错：一键反馈，**不要求用户写原因**（docs/模板沉淀标准与复用校验_系统性方案.md §5.2）。
// 上下文（用的哪条模板/示例、本轮条件、模板自带条件、差异）由后端按 result_id 自动补齐——
// 要求填原因用户就懒得点，坏模板会一直留在推荐里。
async function sendFeedback(msg, kind) {
  try {
    const index = messages.value.indexOf(msg)
    let question = ''
    for (let i = index; i >= 0; i -= 1) {
      if (messages.value[i]?.role === 'user') {
        question = messages.value[i].content || ''
        break
      }
    }
    const approved =
      kind === 'good' && msg.reviewable && msg.result_id
        ? await api.approveResult(msg.result_id)
        : null
    const out = await api.reportFeedback({
      question,
      comment: '',
      sql: msg.sql || '',
      reply: msg.content || '',
      kind,
      result_id: msg.result_id || '',
    })
    if (kind === 'bad' && out?.demoted) {
      ElMessage.warning('已记录；这条模板连续答错，已自动降级、不再推荐')
    } else if (approved) {
      ElMessage.success(
        approved.deduped
          ? '答对了；示例库里已有这条，已标记为已确认'
          : '答对了，已存入示例库',
      )
    } else {
      ElMessage.success(kind === 'bad' ? '已记录，谢谢反馈' : '感谢确认')
    }
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function decideReview(msg, decision) {
  if (!msg?.review_gate?.review_token || msg.review_busy) return
  msg.review_busy = true
  try {
    const out = await api.queryReview(msg.review_gate.review_token, decision)
    msg.review_gate = null
    if (decision === 'accept') {
      ElMessage.success('已按你的判断保留这次结果')
    } else if (out?.repair_required) {
      ElMessage.info('已提交错误判断，正在进入受限重新规划')
    }
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    msg.review_busy = false
  }
}

function confirmQuery() {
  if (!pendingQuestion.value) return
  confirmedNext = true
  confirmPrompt.value = ''
  sendMessage(pendingQuestion.value)
}

// 用户在确认卡里选了口径（或"让 AI 自己查"）→ 带着选择重发，后端不再重新规划
async function confirmCapability(forceAi = false) {
  if (submitting.value) return
  const text = pendingQuestion.value
  if (!text) return
  const key = capabilityPick.value
  const [kind, ...rest] = String(key).split(':')
  const id = rest.join(':')
  // 选了"待补"槽还没填的口径 → 先让用户补齐，别发一个注定绑不上的请求
  if (!forceAi) {
    const picked = (capabilityChoices.value || []).find((item) => `${item.kind}:${item.id}` === key)
    const pendingSlots = (picked?.slots || []).filter(
      (slot) => slot.missing && !String(slotValueOf(picked, slot) || '').trim(),
    )
    if (pendingSlots.length) {
      ElMessage.warning(`还有 ${pendingSlots.length} 个条件要补：${pendingSlots.map((s) => s.label).join('、')}`)
      return
    }
  }
  const values = {}
  for (const [slotKey, value] of Object.entries(slotValues.value || {})) {
    if (slotKey.startsWith(key + ':')) values[slotKey.slice(key.length + 1)] = value
  }
  const token = capabilityToken.value
  capabilityChoices.value = []
  stepCount.value = 0
  confirmPrompt.value = ''
  pendingQuestion.value = ''
  submitting.value = true
  try {
    await sendMessage(text, null, {
      choose_token: token,
      capability_id: forceAi ? 'ai' : id,
      slot_values: forceAi ? {} : values,
    })
  } finally {
    submitting.value = false
  }
}

const SOURCE_LABELS = {
  preset: '模板',
  auto: '自动模板',
  example: '示例',
  example_direct: '相似问法',
  code_template: '代码口径',
  generic: '通用算子',
}
const sourceLabel = (kind) => SOURCE_LABELS[kind] || kind

// 口径拆成小 chip：后端给的是"范围；对象；指标"这样的句子
function scopeChips(scope) {
  return String(scope || '')
    .split(/[；;]/)
    .map((text) => text.trim())
    .filter(Boolean)
    .slice(0, 5)
}

function slotKey(item, slot) {
  return `${item.kind}:${item.id}:${slot.name}`
}

function slotValueOf(item, slot) {
  const key = slotKey(item, slot)
  return slotValues.value[key] ?? slot.value
}

function setSlotValue(item, slot, value) {
  slotValues.value = { ...slotValues.value, [slotKey(item, slot)]: value }
}

function toggleSql(item) {
  const key = `${item.kind}:${item.id}`
  sqlOpen.value = { ...sqlOpen.value, [key]: !sqlOpen.value[key] }
}

async function copySql(sql) {
  try {
    await navigator.clipboard.writeText(String(sql || ''))
    ElMessage.success('SQL 已复制')
  } catch {
    ElMessage.error('复制失败，请手动选中')
  }
}

function cancelCapability() {
  capabilityChoices.value = []
  stepCount.value = 0
  confirmPrompt.value = ''
  question.value = pendingQuestion.value || question.value
  pendingQuestion.value = ''
}

function rejectQuery() {
  confirmPrompt.value = ''
  question.value = pendingQuestion.value
}

async function showResult(resultId) {
  try {
    const requestConversationEpoch = conversationEpoch
    const requestSessionId = sessionId.value
    const data = await api.result(resultId)
    if (
      requestConversationEpoch !== conversationEpoch ||
      requestSessionId !== sessionId.value
    ) return
    result.value = data
    activeResultId.value = resultId
    if (props.embedded) resultDrawerOpen.value = true
  } catch (err) {
    ElMessage.error(err.message)
  }
}

function openConversation(id) {
  if (id === sessionId.value) return
  invalidateConversationActivity()
  sessionId.value = id
  persistSessionId()
  messages.value = []
  result.value = null
  loadConversationMessages(id)
}

function selectEmbeddedConversation(id) {
  embeddedHistoryOpen.value = false
  openConversation(id)
}

async function handleParentMessage(event) {
  if (!props.embedded) return
  const data = event.data || {}
  if (data.type === 'assistant:open-history') {
    embeddedHistoryOpen.value = true
  }
  if (data.type === 'assistant:reset-result') {
    resultDrawerOpen.value = false
  }
  if (data.type === 'assistant:focus') {
    startFreshEmbeddedConversation()
    await nextTick()
    document.querySelector('.composer-box textarea')?.focus()
  }
  if (data.type === 'assistant:ask' && typeof data.text === 'string') {
    const text = data.text.trim()
    if (!text) return
    question.value = text
    await nextTick()
    send()
  }
}

function notifyHostReady() {
  if (!props.embedded || window.parent === window) return
  window.parent.postMessage({ type: 'assistant:ready', protocol: 1 }, '*')
}

watch(resultDrawerOpen, (open) => {
  if (!props.embedded || window.parent === window) return
  window.parent.postMessage(
    { type: open ? 'assistant:result-open' : 'assistant:result-close', protocol: 1 },
    '*',
  )
})

async function newConversation() {
  const previous = sessionId.value
  resetConversationState()
  persistSessionId()
  // 清掉上一轮的会话记忆，避免旧上下文影响新问题
  try {
    await api.clearSession(previous)
  } catch {
    // 清理失败不影响新对话
  }
}

function resetConversationState() {
  invalidateConversationActivity()
  sessionId.value = crypto.randomUUID()
  messages.value = []
  result.value = null
  question.value = ''
  activeResultId.value = ''
  resultDrawerOpen.value = false
  capabilityChoices.value = []
  capabilityPick.value = ''
  capabilityToken.value = ''
  stepCount.value = 0
  confirmPrompt.value = ''
  pendingQuestion.value = ''
}

function invalidateConversationActivity() {
  conversationEpoch += 1
  activeRequestId += 1
  loading.value = false
  liveEvents.value = []
  submitting.value = false
  confirmedNext = false
  turnElapsedMs = 0
  capabilityChoices.value = []
  capabilityPick.value = ''
  capabilityToken.value = ''
  stepCount.value = 0
  confirmPrompt.value = ''
  pendingQuestion.value = ''
}

function startFreshEmbeddedConversation() {
  resetConversationState()
}

const managing = ref(false)
const selected = ref([])

const allSelected = computed(
  () => conversations.value.length > 0 && selected.value.length === conversations.value.length,
)
const someSelected = computed(() => selected.value.length > 0 && !allSelected.value)

function toggleManage() {
  managing.value = !managing.value
  if (!managing.value) selected.value = []
}

function toggleSelect(id) {
  selected.value = selected.value.includes(id)
    ? selected.value.filter((item) => item !== id)
    : [...selected.value, id]
}

function toggleAll(checked) {
  selected.value = checked ? conversations.value.map((item) => item.id) : []
}

async function removeSelected() {
  if (!selected.value.length) return
  try {
    await ElMessageBox.confirm(
      `删除选中的 ${selected.value.length} 个对话？记录会一并清除。`,
      '确认删除',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    const ids = [...selected.value]
    await api.purgeConversations({ ids })
    if (ids.includes(sessionId.value)) await newConversation()
    selected.value = []
    await loadConversations()
    ElMessage.success(`已删除 ${ids.length} 个对话`)
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function removeAll() {
  try {
    await ElMessageBox.confirm(
      `清空全部 ${conversations.value.length} 个对话？记录会一并清除，无法恢复。`,
      '确认清空',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.purgeConversations({ all: true })
    selected.value = []
    await newConversation()
    await loadConversations()
    ElMessage.success('对话记录已清空')
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function removeConversation(id) {
  try {
    await ElMessageBox.confirm('删除这个对话？记录会一并清除。', '确认删除', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api.deleteConversation(id)
    if (id === sessionId.value) {
      await newConversation()
    }
    await loadConversations()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function send() {
  const text = question.value.trim()
  if (!text || loading.value) return
  await sendMessage(text)
}

// 「智能回答」旁路（docs/RAG示例直达与AI直答旁路方案.md §四）：本条跳过模板/示例候选卡
async function sendDirect() {
  const text = question.value.trim()
  if (!text || loading.value) return
  await sendMessage(text, null, { force_ai: true })
}

// useSaved：选择式澄清的回执（true=用已确认过的固化查询，false=让 AI 重新查）
async function sendMessage(text, useSaved = null, extra = {}) {
  if (!text || loading.value) return
  const requestConversationEpoch = conversationEpoch
  const requestSessionId = sessionId.value
  const requestId = ++activeRequestId
  const isCurrentRequest = () =>
    requestConversationEpoch === conversationEpoch &&
    requestSessionId === sessionId.value &&
    requestId === activeRequestId
  // 确认卡上的"用这口径查询"是对**已经上屏的那条问题**选口径，不是又问了一遍：
  // 带 choose_token 的重发不再 push 用户气泡（否则对话里会出现两条一模一样的问题）。
  const reissue = Boolean((extra || {}).choose_token)
  if (!reissue) {
    turnElapsedMs = 0
    messages.value.push({ role: 'user', content: text, time: nowText() })
  }
  question.value = ''
  loading.value = true
  liveEvents.value = []
  scrollToBottom()
  const startedAt = performance.now()
  try {
    const requestPayload = {
      message: text,
      session_id: sessionId.value,
      confirmed: confirmedNext,
      use_saved: useSaved,
      ...extra,
    }
    let payload
    try {
      payload = await api.chatStream(requestPayload, (event) => {
        if (!isCurrentRequest()) return
        liveEvents.value.push(event)
        scrollToBottom()
      })
    } catch {
      if (!isCurrentRequest()) return
      // SSE 被网关拦截、浏览器不支持 ReadableStream，或流在 final 前中断时回退同步。
      liveEvents.value = []
      payload = await api.chat(requestPayload)
    }
    if (!isCurrentRequest()) {
      await loadConversations()
      return
    }
    turnElapsedMs += performance.now() - startedAt
    confirmedNext = false
    // 复杂问题：后端先复述口径等确认，不往下走（方案第二章）
    if (payload.await_confirm) {
      pendingQuestion.value = text
      confirmPrompt.value = payload.reply || ''
      // 能力确认卡：候选 + 推荐项 + 暂存 token（用户改的槽位会随第二次请求回传）
      capabilityChoices.value = payload.capability_choices || []
      capabilityToken.value = payload.choose_token || ''
      stepCount.value = Number(payload.step_count || 0)
      const recommended = (payload.capability_choices || []).find((item) => item.id === payload.recommended_id)
      capabilityPick.value = recommended
        ? `${recommended.kind}:${recommended.id}`
        : (payload.capability_choices || [])[0]
          ? `${payload.capability_choices[0].kind}:${payload.capability_choices[0].id}`
          : ''
      return
    }
    capabilityChoices.value = []
    confirmPrompt.value = ''
    result.value = payload
    messages.value.push({
      role: 'assistant',
      content: payload.reply || '已完成',
      time: nowText(),
      elapsed_ms: Math.round(turnElapsedMs),
      references: dedupeReferences(payload.references || []),
      thinking: payload.thinking || '',
      result_id: payload.result_id || '',
      reviewable: Boolean(payload.reviewable),
      choices: payload.choices || [],
      asked: text,
      workflow_events: liveEvents.value.slice(),
      // 执行链路（复合题才有）：每步的问法/引擎/行数/图片 + 链路回填的键值
      process: payload.process || null,
      review_gate: payload.review_gate || null,
    })
    activeResultId.value = payload.result_id || ''
    await loadConversations()
  } catch (err) {
    if (!isCurrentRequest()) return
    turnElapsedMs += performance.now() - startedAt
    messages.value.push({
      role: 'assistant',
      content: `请求失败：${err.message}`,
      time: nowText(),
      elapsed_ms: Math.round(turnElapsedMs),
    })
  } finally {
    if (isCurrentRequest()) {
      loading.value = false
      liveEvents.value = []
      scrollToBottom()
    }
  }
}

function dedupeReferences(references = []) {
  const seen = new Set()
  return references.filter((ref) => {
    const key = ref.document_keyword || ref.document_id || ref.id
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function focusReference() {
  resultPanelRef.value?.focusSources()
}

function toggleRight() {
  rightCollapsed.value = !rightCollapsed.value
}

function startResize(event) {
  event.preventDefault()
  const startX = event.clientX
  const startWidth = leftWidth.value

  const onMove = (moveEvent) => {
    const delta = ((moveEvent.clientX - startX) / window.innerWidth) * 100
    leftWidth.value = Math.min(65, Math.max(35, startWidth + delta))
  }

  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }

  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

function resetResize() {
  leftWidth.value = 50
}

onMounted(async () => {
  await Promise.all([loadChatOptions(), loadConversations()])
  // 恢复上次的会话记录（新 id 则自然是空的）
  await loadConversationMessages(sessionId.value)
  if (props.embedded) {
    window.addEventListener('message', handleParentMessage)
    notifyHostReady()
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('mousemove', startResize)
  window.removeEventListener('message', handleParentMessage)
})
</script>

<style scoped>
.workspace {
  display: flex;
  height: 100%;
  min-height: 0;
  gap: 12px;
}

.workspace.embedded {
  display: block;
  gap: 0;
}

.workspace.embedded .chat-pane {
  height: 100%;
  min-width: 0;
  border: 0;
  border-radius: 0;
  box-shadow: none;
}

.embed-head {
  position: relative;
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  min-height: 54px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  background: #fff;
}

.embed-head > div:first-child {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.embed-head strong {
  color: var(--ink);
  font-size: 14px;
}

.embed-head span {
  color: var(--ink-soft);
  font-size: 10px;
}

.embed-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.embed-head-actions button {
  width: 30px;
  height: 30px;
  color: var(--ink-mid);
  font-size: 16px;
  border: 1px solid var(--line);
  border-radius: 50%;
  background: #fff;
  cursor: pointer;
}

.embed-head-actions .embed-help {
  display: grid;
  width: 18px;
  height: 18px;
  padding: 0;
  color: var(--blue) !important;
  border: 1px solid rgba(30, 94, 255, 0.45);
  border-radius: 50%;
  place-items: center;
  cursor: help;
}

.embed-help-card {
  position: absolute;
  top: 48px;
  right: 10px;
  z-index: 4;
  width: min(290px, calc(100% - 20px));
  padding: 10px 12px;
  color: var(--ink-mid);
  font-size: 12px;
  line-height: 1.6;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 12px 30px rgba(20, 40, 80, 0.16);
}

.workspace.embedded .message-list {
  padding: 14px 12px 6px;
}

.workspace.embedded .message-row {
  gap: 7px;
  margin-bottom: 13px;
}

.workspace.embedded .message-avatar {
  flex-basis: 26px;
  width: 26px;
  height: 26px;
  border-radius: 8px;
}

.workspace.embedded .message-body {
  max-width: calc(100% - 34px);
}

.workspace.embedded .message-bubble {
  padding: 10px 11px;
  font-size: 13px;
}

.workspace.embedded .chat-empty {
  margin: 20px auto;
  padding: 20px 14px;
}

.workspace.embedded .chat-empty h3 {
  font-size: 17px;
}

.workspace.embedded .example-chips {
  margin-top: 14px;
}

.workspace.embedded .message-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  opacity: 1;
}

.workspace.embedded .message-actions :deep(.el-button) {
  margin-left: 0;
}

.workspace.embedded .composer {
  padding: 8px 10px 10px;
}

.workspace.embedded .composer-hint {
  display: none;
}

.workspace.embedded .composer-box {
  padding: 6px 6px 6px 10px;
}

.embed-history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 4px 12px;
  border-bottom: 1px solid var(--line);
}

.embed-history-head button {
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: 50%;
  background: var(--surface-soft);
  cursor: pointer;
}

:global(.embed-result-drawer .el-drawer__body),
:global(.embed-history-drawer .el-drawer__body) {
  padding: 8px;
  overflow: auto;
}

:global(.embed-result-drawer .result-panel) {
  min-width: 0;
}

.workspace.right-collapsed .result-pane {
  display: none;
}

.workspace.right-collapsed .chat-pane {
  flex-basis: 100% !important;
}

.chat-pane {
  display: flex;
  flex-direction: column;
  min-width: 420px;
  overflow: hidden;
}

.pane-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  padding: 18px 20px 12px;
  border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, #fff, #fbfdff);
}

.pane-eyebrow {
  color: var(--blue);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.pane-title {
  margin: 4px 0 0;
  font-size: 20px;
}

.pane-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.switch-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--ink-soft);
  font-size: 12px;
  cursor: pointer;
}

.chat-body {
  display: flex;
  flex: 1;
  min-height: 0;
}

.conversation-list {
  display: flex;
  flex: 0 0 200px;
  flex-direction: column;
  gap: 4px;
  padding: 10px 8px;
  overflow-y: auto;
  border-right: 1px solid var(--line);
  background: var(--surface-soft);
}

.conversation-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 6px 8px;
  color: var(--ink-soft);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
}

.conv-manage {
  padding: 0;
  border: 0;
  background: none;
  color: var(--blue);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0;
  cursor: pointer;
}

.conversation-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 0 6px 8px;
  font-size: 12px;
}

.conversation-actions .conv-count {
  color: var(--ink-soft);
  font-size: 11px;
}

.conv-check {
  margin-right: 6px;
  color: var(--blue);
  font-size: 12px;
}

.conversation-item {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 9px 26px 9px 10px;
  text-align: left;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  cursor: pointer;
  transition: 140ms ease;
}

.conversation-item:hover {
  border-color: var(--line);
  background: #fff;
}

.conversation-item.active {
  border-color: rgba(30, 94, 255, 0.3);
  background: #fff;
  box-shadow: var(--shadow-sm);
}

.conv-title {
  overflow: hidden;
  color: var(--ink);
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conv-meta {
  color: var(--ink-soft);
  font-size: 11px;
}

.conv-delete {
  position: absolute;
  top: 6px;
  right: 8px;
  display: none;
  color: var(--red);
  font-size: 15px;
  line-height: 1;
}

.conversation-item:hover .conv-delete {
  display: block;
}

.conversation-empty {
  padding: 8px 10px;
  color: var(--ink-soft);
  font-size: 12px;
}

.message-area {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.message-list {
  flex: 1;
  min-height: 0;
  padding: 18px 18px 8px;
  overflow: auto;
  background:
    linear-gradient(180deg, rgba(244, 247, 251, 0.55), rgba(255, 255, 255, 0.2)),
    var(--surface-soft);
}

.chat-empty {
  max-width: 520px;
  margin: 42px auto;
  padding: 28px;
  text-align: center;
  border: 1px dashed var(--line-strong);
  border-radius: var(--radius-lg);
  background: rgba(255, 255, 255, 0.88);
  box-shadow: var(--shadow-sm);
}

.chat-empty h3 {
  margin: 16px 0 8px;
  color: var(--ink);
  font-size: 20px;
}

.chat-empty p {
  margin: 0;
  color: var(--ink-soft);
  font-size: 13px;
  line-height: 1.7;
}

.milepost-mark {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--blue);
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
}

.milepost-mark i {
  position: relative;
  width: 180px;
  height: 3px;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--amber) 0 20px, var(--blue) 20px 100%);
}

.milepost-mark i::after {
  position: absolute;
  top: -5px;
  left: 20px;
  width: 13px;
  height: 13px;
  content: "";
  border: 3px solid #fff;
  border-radius: 50%;
  background: var(--blue);
  box-shadow: 0 0 0 1px var(--blue);
}

.example-chips {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 22px;
}

.example-chips button {
  padding: 8px 12px;
  color: var(--ink-mid);
  font-size: 12px;
  text-align: left;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
  cursor: pointer;
  transition: 150ms ease;
}

.example-chips button:hover {
  color: var(--blue);
  border-color: rgba(30, 94, 255, 0.42);
  background: var(--blue-soft);
}

.message-row {
  display: flex;
  gap: 10px;
  margin-bottom: 18px;
}

.message-row.is-user {
  flex-direction: row-reverse;
}

.message-avatar {
  display: grid;
  flex: 0 0 32px;
  width: 32px;
  height: 32px;
  color: var(--blue);
  font-size: 11px;
  font-weight: 800;
  border: 1px solid rgba(30, 94, 255, 0.18);
  border-radius: 10px;
  background: var(--blue-soft);
  place-items: center;
}

.message-row.is-user .message-avatar {
  color: #fff;
  border-color: var(--blue);
  background: var(--blue);
}

.message-body {
  max-width: min(88%, 720px);
}

/* ── 执行链路（像看 skill 的步骤一样看数据流；默认折叠，标题给线路）──────────── */
.pipeline-collapse {
  margin: 6px 0 0;
  font-size: 12px;
}

.pipeline-collapse :deep(.el-collapse-item__header) {
  height: 30px;
  line-height: 30px;
  border-bottom: none;
  font-size: 12px;
  color: var(--ink-soft);
}

.pipeline-collapse :deep(.el-collapse-item__wrap) {
  border-bottom: none;
}

.pipeline-collapse :deep(.el-collapse-item__content) {
  padding: 8px 10px 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fbfcfe;
}

.pipeline-steps {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.pipeline-step {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.pipeline-index {
  flex: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--blue);
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
}

.pipeline-type {
  flex: none;
  color: var(--ink);
}

.pipeline-engine {
  flex: none;
  padding: 0 6px;
  border-radius: 999px;
  background: #eef2f7;
  color: #5a6b85;
}

.pipeline-engine.engine-wren {
  background: #eaf3ff;
  color: #1b62c4;
}

.pipeline-engine.engine-template,
.pipeline-engine.engine-template_match {
  background: #eafaf0;
  color: #1a7f45;
}

.pipeline-count {
  flex: none;
  color: var(--ink-soft);
}

.pipeline-tag {
  flex: none;
  padding: 0 5px;
  border-radius: 4px;
  background: #fff4e5;
  color: #a8640a;
}

.pipeline-q {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--ink-soft);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pipeline-error {
  flex: 0 0 calc(100% - 22px);
  margin-left: 22px;
  color: #b42318;
  line-height: 1.45;
  white-space: normal;
}

.pipeline-chain {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
}

.pipeline-chain-label {
  color: var(--ink-soft);
}

.pipeline-chip {
  padding: 1px 6px;
  border-radius: 999px;
  background: #eef7ff;
  color: #1b62c4;
}

.message-row.is-user .message-body {
  text-align: right;
}

.message-meta {
  display: flex;
  gap: 8px;
  margin: 0 0 5px;
  color: var(--ink-soft);
  font-size: 11px;
}

.message-row.is-user .message-meta {
  justify-content: flex-end;
}

/* 这条回答的系统耗时（提问 → 出答案，确认卡的两段也算在一起） */
.message-cost {
  padding: 0 6px;
  border-radius: 8px;
  background: var(--blue-soft, rgba(64, 128, 255, 0.1));
  color: var(--blue-strong, #2f6fed);
  font-variant-numeric: tabular-nums;
}

.message-bubble {
  display: inline-block;
  padding: 12px 14px;
  color: var(--ink-mid);
  line-height: 1.7;
  text-align: left;
  border: 1px solid var(--line);
  border-radius: 4px 14px 14px 14px;
  background: #fff;
  box-shadow: var(--shadow-sm);
}

.message-row.is-user .message-bubble {
  color: #fff;
  border-color: var(--blue);
  border-radius: 14px 4px 14px 14px;
  background: linear-gradient(135deg, var(--blue-deep), var(--blue));
}

.live-workflow {
  width: min(680px, 72vw);
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 4px 14px 14px 14px;
  background: #fff;
  box-shadow: var(--shadow-sm);
}

.live-workflow-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  color: var(--ink-mid);
  font-size: 13px;
  font-weight: 600;
}

.live-pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--blue);
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--blue) 45%, transparent);
  animation: live-pulse 1.5s infinite;
}

.live-event {
  display: flex;
  gap: 9px;
  padding: 6px 0;
  color: var(--ink-mid);
}

.live-event + .live-event {
  border-top: 1px dashed color-mix(in srgb, var(--line) 75%, transparent);
}

.live-event-icon {
  flex: 0 0 18px;
  color: var(--blue);
  text-align: center;
  font-size: 13px;
}

.live-event-body {
  min-width: 0;
}

.live-event-title {
  font-size: 12.5px;
  font-weight: 600;
}

.live-event-detail {
  margin-top: 2px;
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.live-thought .live-event-icon,
.live-thought .live-event-title {
  color: var(--ink-soft);
}

.live-observation .live-event-icon {
  color: var(--green);
}

.live-error .live-event-icon,
.live-error .live-event-title {
  color: var(--red);
}

.workflow-event-list .live-event:first-child {
  padding-top: 0;
}

@keyframes live-pulse {
  70% {
    box-shadow: 0 0 0 6px transparent;
  }
  100% {
    box-shadow: 0 0 0 0 transparent;
  }
}

.typing {
  display: inline-flex;
  gap: 5px;
  padding: 14px 16px;
}

.typing span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--blue);
  animation: blink 1.1s infinite ease-in-out;
}

.typing span:nth-child(2) {
  animation-delay: 0.15s;
}

.typing span:nth-child(3) {
  animation-delay: 0.3s;
}

.composer {
  flex: 0 0 auto;
  padding: 12px 16px 16px;
  border-top: 1px solid var(--line);
  background: #fff;
}

.composer-hint {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 14px;
  margin-bottom: 7px;
  color: var(--ink-soft);
  font-size: 11px;
}

.ai-direct {
  margin-right: auto;
  border: none;
  background: transparent;
  color: var(--blue, #2f6fed);
  font-size: 11px;
  cursor: pointer;
  padding: 0;
}

.ai-direct:hover:not(:disabled) {
  text-decoration: underline;
}

.ai-direct:disabled {
  color: var(--ink-soft);
  cursor: not-allowed;
}

.composer-box {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 8px 8px 8px 12px;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-md);
  background: var(--surface-soft);
  transition: 160ms ease;
}

.composer-box:focus-within {
  border-color: var(--blue);
  box-shadow: 0 0 0 3px rgba(30, 94, 255, 0.1);
  background: #fff;
}

.composer-box :deep(.el-textarea__inner) {
  padding: 4px 0;
  border: 0;
  background: transparent;
  box-shadow: none;
}

.send-button {
  flex: 0 0 40px;
  width: 40px;
  height: 40px;
  font-size: 20px;
  font-weight: 800;
}

.split-handle {
  display: flex;
  flex: 0 0 10px;
  align-items: center;
  justify-content: center;
  cursor: col-resize;
}

.split-handle span {
  width: 3px;
  height: 54px;
  border-radius: 999px;
  background: var(--line-strong);
  transition: 160ms ease;
}

.split-handle:hover span {
  height: 86px;
  background: var(--blue);
}

.result-pane {
  flex: 1;
  min-width: 0;
}

.answer-references {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}

.ref-label {
  color: var(--ink-soft);
  font-size: 11px;
}

.ref-chip {
  max-width: 240px;
  padding: 4px 8px;
  overflow: hidden;
  color: var(--blue-deep);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
  border: 1px solid rgba(30, 94, 255, 0.24);
  border-radius: 6px;
  background: var(--blue-soft);
  cursor: pointer;
}

.ref-chip:hover {
  border-color: var(--blue);
  background: #dce9ff;
}

.thinking-box {
  margin-top: 6px;
  border: 0;
}

.thinking-box :deep(.el-collapse-item__header) {
  height: 30px;
  padding: 0 10px;
  color: var(--ink-soft);
  font-size: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
}

.thinking-box :deep(.el-collapse-item__wrap) {
  border: 0;
}

.thinking-text {
  max-height: 280px;
  margin: 0;
  padding: 10px;
  overflow: auto;
  color: var(--ink-soft);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
}

.review-gate-card {
  margin-top: 10px;
  padding: 13px 14px;
  border: 1px solid #f0c36a;
  border-radius: 14px;
  background: #fffaf0;
  box-shadow: 0 8px 22px rgba(120, 82, 10, 0.08);
}

.review-gate-head {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.review-gate-mark {
  flex: none;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #b7791f;
  color: #fff;
  font-weight: 700;
  line-height: 24px;
  text-align: center;
}

.review-gate-head p {
  margin: 3px 0 0;
  color: #7b5b1d;
  line-height: 1.5;
}

.review-gate-node {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid #f3dfb2;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.72);
}

.review-gate-question {
  color: var(--ink);
  font-weight: 600;
  line-height: 1.5;
}

.review-gate-question span {
  display: inline-block;
  margin-right: 6px;
  padding: 0 6px;
  border-radius: 999px;
  background: #f6e5bd;
  color: #80540d;
  font-size: 11px;
}

.review-gate-result {
  margin-top: 4px;
  color: var(--ink-soft);
  font-size: 12px;
}

.review-gate-evidence {
  margin-top: 4px;
}

.review-gate-evidence :deep(.el-collapse-item__header),
.review-gate-evidence :deep(.el-collapse-item__wrap) {
  border-bottom: 0;
  background: transparent;
}

.review-gate-evidence pre {
  max-height: 220px;
  overflow: auto;
  padding: 8px;
  border-radius: 8px;
  background: #fff;
  color: var(--ink-soft);
  font-family: var(--font-mono);
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
}

.review-gate-reason {
  margin: 0 0 6px;
  color: #9a3412;
}

.review-gate-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.message-actions {
  margin-top: 7px;
}

.choice-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

/* ── 决策卡：浅色卡片，不再继承 confirm-bar 的深色底 ───────────────── */
.capability-row .message-body {
  max-width: min(760px, 100%);
}

.decision-card {
  position: relative;
  padding: 14px 16px 12px 18px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 10px 28px rgba(15, 40, 90, 0.08);
  animation: decision-in 220ms ease-out;
}

.decision-card::before {
  position: absolute;
  top: 14px;
  bottom: 14px;
  left: 0;
  width: 3px;
  border-radius: 3px;
  background: linear-gradient(180deg, #1e5eff, #6aa2ff);
  content: '';
}

@keyframes decision-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.decision-head {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-bottom: 10px;
}

.decision-icon {
  display: grid;
  width: 26px;
  height: 26px;
  color: #1e5eff;
  font-size: 15px;
  background: #eef4ff;
  border-radius: 8px;
  place-items: center;
}

.decision-title {
  font-size: 14px;
  font-weight: 650;
  color: var(--ink-strong, #16233c);
}

.decision-sub {
  margin-top: 2px;
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.6;
}

.cand-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 46vh;
  margin-bottom: 10px;
  overflow: auto;
}

.cand {
  position: relative;
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  cursor: pointer;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: #fbfcfe;
  transition: border-color 160ms ease, background 160ms ease, box-shadow 160ms ease,
    transform 160ms ease;
}

.cand:hover {
  border-color: #cfe0ff;
  transform: translateY(-1px);
}

.cand.active {
  border-color: rgba(30, 94, 255, 0.5);
  background: #f2f7ff;
  box-shadow: 0 6px 16px rgba(30, 94, 255, 0.12);
}

.cand.active::before {
  position: absolute;
  top: 8px;
  bottom: 8px;
  left: 0;
  width: 3px;
  border-radius: 3px;
  background: #1e5eff;
  content: '';
}

.cand.warn {
  border-color: #f0d9a8;
  background: #fffdf6;
}

.cand-radio {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.cand-mark {
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
  margin-top: 2px;
  border: 1.5px solid #c3cddf;
  border-radius: 50%;
  background: #fff;
  transition: border-color 160ms ease, background 160ms ease;
}

.cand.active .cand-mark {
  border-color: #1e5eff;
  background: #1e5eff;
  box-shadow: inset 0 0 0 3px #fff;
}

.cand-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  font-size: 12px;
  line-height: 1.6;
}

.cand-title {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  font-size: 13px;
  font-weight: 600;
  color: var(--ink-strong, #16233c);
}

.src-badge {
  padding: 1px 7px;
  font-size: 11px;
  font-style: normal;
  font-weight: 500;
  border-radius: 999px;
}

.src-badge[data-src='preset'] {
  color: #14507a;
  background: #eaf4fd;
}

.src-badge[data-src='example'] {
  color: #1c6b4a;
  background: #e9f7f0;
}

.src-badge[data-src='code_template'] {
  color: #5a6472;
  background: #f1f3f7;
}

.pill {
  padding: 1px 7px;
  font-size: 11px;
  border-radius: 999px;
}

.pill-green {
  color: #1c6b4a;
  background: #e3f6ec;
}

.pill-amber {
  color: #8a5a00;
  background: #fdf3e0;
}

.cand-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chip {
  padding: 2px 8px;
  color: var(--ink-mid, #4a566b);
  font-size: 11px;
  font-style: normal;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
}

.cand-hint {
  display: block;
  margin-top: 4px;
  color: var(--warn-ink, #b88230);
  font-size: 11px;
}

.cand-why {
  color: var(--ink-soft);
  font-size: 11px;
}

.cand-params {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.cand-params-label {
  padding-top: 3px;
  color: var(--ink-soft);
  font-size: 11px;
}

.cand-params-grid {
  display: grid;
  flex: 1;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 6px 10px;
}

.cand-param {
  display: flex;
  gap: 6px;
  align-items: center;
  min-width: 0;
}

.cand-param-label {
  flex: 0 0 auto;
  color: var(--ink-soft);
  font-size: 11px;
}

.cand-param :deep(.el-select) {
  width: 100%;
  min-width: 130px;
}

.sql-toggle {
  padding: 2px 8px;
  color: #14507a;
  font-size: 11px;
  cursor: pointer;
  border: 1px dashed rgba(30, 94, 255, 0.3);
  border-radius: 6px;
  background: #fff;
}

.sql-box {
  position: relative;
  display: block;
  margin-top: 6px;
}

.sql-box code {
  display: block;
  max-height: 210px;
  padding: 10px 44px 10px 12px;
  overflow: auto;
  /* 白纸黑字（深色聊天区里黑底浅蓝字看不清） */
  color: #1b2432;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fff;
}

.sql-copy {
  position: absolute;
  top: 8px;
  right: 8px;
  padding: 2px 8px;
  color: #1b2432;
  font-size: 11px;
  cursor: pointer;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #f4f7fc;
}

.cand-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.result-link {
  padding: 3px 9px;
  color: var(--blue-deep);
  font-size: 11px;
  border: 1px dashed rgba(30, 94, 255, 0.35);
  border-radius: 999px;
  background: #fff;
  cursor: pointer;
  transition: 140ms ease;
}

.result-link:hover {
  border-style: solid;
  background: var(--blue-soft);
}

.result-link.active {
  color: #fff;
  border-style: solid;
  border-color: var(--blue);
  background: var(--blue);
}

@keyframes blink {
  0%,
  80%,
  100% {
    opacity: 0.28;
    transform: translateY(0);
  }
  40% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

@media (max-width: 1200px) {
  .conversation-list {
    flex-basis: 160px;
  }
}

@media (max-width: 900px) {
  .workspace {
    flex-direction: column;
  }

  .chat-pane,
  .result-pane {
    flex: 1 1 auto;
    min-height: 0;
  }

  .split-handle {
    display: none;
  }

  .conversation-list {
    display: none;
  }
}
.confirm-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 12px 0;
  padding: 10px 14px;
  /* 浅色聊天区里再来一条深蓝灰太割裂：统一成浅底 + 蓝边 + 深字，深色只留给顶栏 */
  border: 1px solid #cfe0ff;
  border-left: 3px solid #1e5eff;
  border-radius: 12px;
  background: #f4f8ff;
}
.confirm-text {
  flex: 1;
  font-size: 13px;
  color: #16233c;
  line-height: 1.5;
}

/* 消息操作按钮降噪：平时只留淡淡的，悬停才显出来 */
.message-actions {
  opacity: 0.35;
  transition: opacity 160ms ease;
}

.message-row:hover .message-actions {
  opacity: 1;
}
</style>
