<template>
  <section class="page-card">
    <div class="page-heading">
      <div>
        <div class="page-eyebrow">Pinned Dashboard</div>
        <h1 class="page-title">固定看板</h1>
        <p class="page-description">
          这里放的是**所有已经验证过的问答对**（RAGFlow 示例库里的 + 你点"固化到看板"的），
          不区分是谁的：点刷新直接重跑当初那条 SQL，不重新生成。要去问新问题去「对话」，
          要换参数的标准模板去「快捷查询」。
        </p>
      </div>
    </div>

    <section class="panel-card">
      <div class="card-heading">
        <h2>已验证的问答卡片</h2>
        <span>{{ filtered.length }} / {{ cards.length }} 张 · 最近一次结果与刷新时间都记在卡片上</span>
        <el-input
          v-model="keyword"
          size="small"
          clearable
          style="margin-left: auto; width: 260px"
          placeholder="按关键词搜问题（路线名、病害类型…）"
        />
      </div>
      <div v-if="filtered.length" class="board-grid">
        <article v-for="card in filtered" :key="card.doc_name" class="board-card">
          <div class="board-card-head">
            <span class="intent-tag" :data-intent="card.origin">{{ originLabel(card) }}</span>
            <template v-if="card.origin === 'example'">
              <el-button v-if="card.pinned" text size="small" @click="pin(card, false)">取消置顶</el-button>
              <el-button v-else text size="small" @click="pin(card, true)">置顶</el-button>
            </template>
          </div>
          <h3 class="board-card-title">{{ card.title }}</h3>
          <p class="board-card-summary">{{ card.last_summary || '还没刷新过，点一下试试' }}</p>
          <p class="board-card-meta">
            {{ card.group_name || card.scope }}
            <template v-if="card.last_run_at"> · {{ card.last_run_at.slice(5, 16) }}</template>
          </p>
          <div class="board-card-actions">
            <el-button size="small" type="primary" :loading="running === card.doc_name" @click="run(card)">
              刷新
            </el-button>
            <el-button size="small" @click="expand(card)">展开</el-button>
            <el-button
              v-if="card.origin === 'example'"
              size="small"
              text
              @click="removeFromBoard(card)"
            >
              移出
            </el-button>
            <el-button
              size="small"
              text
              type="danger"
              @click="deleteCard(card)"
            >
              删除
            </el-button>
          </div>
        </article>
      </div>
      <el-empty
        v-else
        :description="keyword ? `没有匹配「${keyword}」的卡片` : '还没有卡片：去「对话」提问，答对了点「⭐ 固化到看板」'"
      />
    </section>

    <el-dialog v-model="detailVisible" :title="detailTitle" width="min(1100px, 92vw)" top="6vh">
      <ResultPanel :result="detail" />
    </el-dialog>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import ResultPanel from './ResultPanel.vue'

const cards = ref([])
const keyword = ref('')
const running = ref('')
const detail = ref(null)
const detailVisible = ref(false)
const detailTitle = ref('')

// 关键词搜卡片：问题、口径、分组、最近结果都搜
const filtered = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  if (!needle) return cards.value
  return cards.value.filter((card) =>
    [card.title, card.question, card.group_name, card.scope, card.last_summary, ...(card.variants || [])]
      .filter(Boolean)
      .some((text) => String(text).toLowerCase().includes(needle)),
  )
})

const INTENT_LABEL = { data: '数据', list: '明细', analysis: '分析' }
function originLabel(card) {
  if (card.origin === 'template') return '模板'
  return INTENT_LABEL[card.intent] || '示例'
}

async function load() {
  try {
    const data = await api.dashboardCards()
    cards.value = data.cards || []
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function run(card) {
  running.value = card.doc_name
  try {
    const payload = await api.runDashboardCard(card.doc_name)
    if (card.origin === 'example') {
      card.last_summary = payload.summary || ''
      card.last_run_at = new Date().toISOString()
    }
    detail.value = { ...payload }
    detailTitle.value = card.title
    detailVisible.value = true
    ElMessage.success(payload.summary || '已刷新')
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    running.value = ''
  }
}

async function expand(card) {
  running.value = card.doc_name
  try {
    detail.value = { ...(await api.runDashboardCard(card.doc_name)) }
    detailTitle.value = card.title
    detailVisible.value = true
    if (card.origin === 'example' && detail.value.summary) card.last_summary = detail.value.summary
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    running.value = ''
  }
}

async function pin(card, pinned) {
  try {
    await api.setDashboardCard({ doc_name: card.doc_name, pinned })
    card.pinned = pinned
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function removeFromBoard(card) {
  try {
    await api.setDashboardCard({ doc_name: card.doc_name, on_board: false })
    ElMessage.success('已移出看板（示例本身还留着，不影响复用）')
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

// 真删：示例答错了才用。RAGFlow「SQL问答示例」+「SQL问题索引」+ 本地示例表一起清，
// 和"移出"（只是不在看板显示）不是一回事
async function deleteCard(card) {
  try {
    if (card.origin === 'template') {
      await ElMessageBox.confirm(
        `彻底删除这个模板？\n\n${card.title}\n\n` +
          '删除后会同时从「固定看板」和「快捷查询」中移除，数据库里的业务数据不会受影响。',
        '删除模板',
        { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
      )
      await api.deletePreset(card.preset_id)
      ElMessage.success('模板已删除')
      await load()
      return
    }
    await ElMessageBox.confirm(
      `彻底删除这张卡片？\n\n${card.title}\n\n` +
        '会把这条问答从 RAGFlow「SQL问答示例」+「SQL问题索引」和本地示例表里一起删掉，' +
        '之后不再被 few-shot / 示例直达召回。只是不想在看板看到它，请用「移出」。',
      '删除示例（不可撤销）',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
    const out = await api.deleteDashboardCard(card.doc_name)
    ElMessage.success(
      `已删除：RAGFlow 文档 ${out.deleted_docs ?? 0} + 问题索引 ${out.deleted_index ?? 0}，本地示例 ${out.removed_records ?? 0} 条`,
    )
    await load()
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err.message)
  }
}

onMounted(load)
</script>

<style scoped>
.panel-card {
  padding: 18px 20px;
  margin-bottom: 16px;
}

.card-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 14px;
}

.card-heading h2 {
  margin: 0;
  font-size: 16px;
}

.card-heading span {
  color: var(--ink-soft);
  font-size: 12px;
}

.board-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}

.board-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: #fff;
}

.board-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 22px;
}

.board-card-title {
  margin: 0;
  font-size: 14px;
  line-height: 1.5;
}

.board-card-summary {
  margin: 0;
  color: var(--blue-deep);
  font-size: 16px;
  font-weight: 600;
}

.board-card-meta {
  margin: 0;
  color: var(--ink-soft);
  font-size: 12px;
}

.board-card-actions {
  display: flex;
  gap: 6px;
  margin-top: auto;
  padding-top: 6px;
}

.intent-tag {
  padding: 2px 8px;
  color: var(--ink-mid);
  font-size: 11px;
  border: 1px solid var(--line);
  border-radius: 999px;
}

.intent-tag[data-intent='analysis'] {
  color: #7a4d00;
  border-color: #f0d9a8;
  background: #fdf6e7;
}

.intent-tag[data-intent='data'] {
  color: #14507a;
  border-color: #cfe3f2;
  background: #eef6fc;
}
</style>
