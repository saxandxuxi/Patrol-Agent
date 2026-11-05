<template>
  <section class="page-card">
    <div class="page-heading">
      <div>
        <div class="page-eyebrow">Knowledge Base</div>
        <h1 class="page-title">知识库镜像</h1>
        <p class="page-description">本地文件与 RAGFlow 数据集同步状态。SQL 规范和养护规范分别独立管理。</p>
      </div>
      <div class="page-actions">
        <el-button @click="load">刷新状态</el-button>
        <el-button type="primary" :loading="loading" @click="sync">重新同步</el-button>
        <el-button type="danger" plain :loading="rechunking" @click="rechunk">强制重新分块</el-button>
      </div>
    </div>

    <el-table :data="rows" border stripe>
      <el-table-column prop="name" label="数据集" min-width="200">
        <template #default="{ row }">
          <div class="dataset-name">
            <span class="dataset-dot" :class="{ online: row.exists }"></span>
            <strong>{{ row.name }}</strong>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="exists" label="RAGFlow 状态" width="140">
        <template #default="{ row }">
          <el-tag :type="row.exists ? 'success' : 'info'" effect="plain">{{ row.exists ? '已就绪' : '未创建' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="local_files" label="本地文件" width="110" sortable />
      <el-table-column prop="sync_docs" label="已同步文档" width="130" sortable />
      <el-table-column label="解析完成" width="120">
        <template #default="{ row }">
          <span v-if="row.remote_docs">{{ row.parsed_docs }}/{{ row.remote_docs }}</span>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column prop="ragflow_id" label="RAGFlow ID" min-width="230" show-overflow-tooltip />
    </el-table>

    <div class="trace-heading">
      <div>
        <div class="page-eyebrow">Retrieval Trace</div>
        <h2 class="trace-heading-title">检索记录</h2>
        <p class="page-description">每次提问实际命中的文档与片段内容，用于核对知识库准确性。</p>
      </div>
      <div class="page-actions">
        <el-button @click="loadRetrievals">刷新记录</el-button>
      </div>
    </div>

    <el-empty v-if="retrievals.length === 0" description="暂无检索记录，先在对话里提一个问题。" />
    <el-collapse v-else class="trace-list">
      <el-collapse-item v-for="record in retrievals" :key="record.id" :name="record.id">
        <template #title>
          <div class="trace-title">
            <strong>{{ record.question }}</strong>
            <span class="trace-meta">
              {{ record.intent || '未知意图' }} · {{ formatTime(record.created_at) }} · {{ record.references.length }} 个片段
            </span>
          </div>
        </template>

        <div class="trace-datasets">
          <el-tag v-for="name in record.datasets" :key="name" size="small" effect="plain">{{ name }}</el-tag>
          <span v-if="record.datasets.length === 0" class="trace-empty">未标注数据集</span>
        </div>

        <el-empty v-if="record.references.length === 0" description="本次未检索到片段" :image-size="60" />
        <div v-for="(ref, index) in record.references" :key="index" class="trace-chunk">
          <div class="trace-chunk-head">
            <span class="trace-doc">{{ ref.document_keyword || ref.document_id || '未知文档' }}</span>
            <span v-if="ref.dataset_name" class="trace-badge">{{ ref.dataset_name }}</span>
            <span v-if="ref.similarity != null" class="trace-badge">相似度 {{ Number(ref.similarity).toFixed(3) }}</span>
          </div>
          <pre class="trace-content">{{ ref.content }}</pre>
        </div>
      </el-collapse-item>
    </el-collapse>

    <div class="trace-heading">
      <div>
        <div class="page-eyebrow">Example Pairs</div>
        <h2 class="trace-heading-title">已入池问答示例</h2>
        <p class="page-description">
          这里只列出已经真正进入「SQL问答示例」数据集的条目，它们会被 few-shot 召回，答错的请删除。
          只有你在对话里点过「结果正确，存入示例库」的案例才会进这里，任何引擎的 SQL 都不会自动入池。
        </p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="pairFilter"
          size="small"
          placeholder="按问题关键词过滤"
          style="width: 190px"
          clearable
        />
        <el-button size="small" @click="purgeFiltered">删除匹配项</el-button>
        <el-button size="small" type="danger" plain @click="purgeAll">清空全部</el-button>
      </div>
    </div>

    <el-empty v-if="pairs.length === 0" description="示例库目前是空的。" :image-size="60" />
    <el-table v-else :data="filteredPairs" border stripe size="small" max-height="320">
      <el-table-column type="expand" width="40">
        <template #default="{ row }">
          <div class="pair-detail">
            <div class="pair-detail-block">
              <span class="pair-detail-label">查询意图 (JSON)</span>
              <pre>{{ row.spec ? JSON.stringify(row.spec, null, 2) : '（该条目没有存 JSON，多为修复前的历史数据）' }}</pre>
            </div>
            <div class="pair-detail-block">
              <span class="pair-detail-label">SQL</span>
              <pre>{{ row.sql }}</pre>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="110">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ sourceLabel(row.source) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="question" label="问题" min-width="220" show-overflow-tooltip />
      <el-table-column label="SQL" min-width="280">
        <template #default="{ row }">
          <code class="pair-sql">{{ row.sql }}</code>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="时间" width="170" />
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="danger" @click="purgeOne(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<script setup>
import { computed, onActivated, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const rows = ref([])
const retrievals = ref([])
const pairs = ref([])
const pairFilter = ref('')
const loading = ref(false)
const rechunking = ref(false)

const filteredPairs = computed(() => {
  const keyword = pairFilter.value.trim()
  if (!keyword) return pairs.value
  return pairs.value.filter((item) => (item.question || '').includes(keyword))
})

async function load() {
  try {
    rows.value = await api.knowledgeStatus()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function sync() {
  loading.value = true
  try {
    await api.syncKnowledge()
    ElMessage.success('同步完成')
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    loading.value = false
  }
}

async function rechunk() {
  try {
    await ElMessageBox.confirm(
      '会删除全部远端文档，并按当前分块配置（父块-子块）重新上传解析；期间对应知识库暂时检索不到内容。确定继续？',
      '确认强制重新分块',
      { type: 'warning' },
    )
  } catch {
    return
  }
  rechunking.value = true
  try {
    await api.rechunkKnowledge()
    ElMessage.success('已提交重新分块，解析在后台进行')
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    rechunking.value = false
  }
}

async function loadRetrievals() {
  try {
    retrievals.value = await api.retrievals(20)
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function loadPairs() {
  try {
    pairs.value = await api.examplePairs(50)
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function purge(payload, tip) {
  try {
    await ElMessageBox.confirm(tip, '确认清理', { type: 'warning' })
  } catch {
    return
  }
  try {
    const result = await api.purgeExamplePairs(payload)
    ElMessage.success(
      `已删除 ${result.deleted_docs} 个示例文档，清理 ${result.removed_records ?? 0} 条登记记录、${result.removed_history} 条历史记录`,
    )
    await loadPairs()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

function purgeOne(row) {
  return purge({ doc_names: [row.doc_name] }, '确定删除这条问答示例？')
}

function purgeFiltered() {
  const keyword = pairFilter.value.trim()
  if (!keyword) {
    ElMessage.warning('请先输入要清理的问题关键词')
    return
  }
  return purge({ question_contains: keyword }, `确定删除所有问题包含「${keyword}」的示例？`)
}

function purgeAll() {
  return purge({ all: true }, '确定清空全部问答示例？此操作不可恢复。')
}

function sourceLabel(source) {
  // 现在只剩人工确认入池；历史遗留的「模板自动」仍照实显示，方便辨认旧数据。
  return { template: '模板自动（旧）', approved: '人工确认' }[source] || source || '—'
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

// 组件被 KeepAlive 缓存，每次切回知识库时都刷新检索记录。
onActivated(() => {
  load()
  loadRetrievals()
  loadPairs()
})
</script>

<style scoped>
.pair-detail {
  padding: 6px 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pair-detail-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.pair-detail-label {
  font-size: 12px;
  color: var(--ink-soft);
  font-weight: 600;
}

.pair-detail pre {
  margin: 0;
  padding: 8px 10px;
  background: var(--surface-muted, #f6f7f9);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow: auto;
}

.page-actions {
  display: flex;
  gap: 8px;
}

.dataset-name {
  display: flex;
  align-items: center;
  gap: 9px;
}

.dataset-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--line-strong);
}

.dataset-dot.online {
  background: #16a34a;
  box-shadow: 0 0 0 4px rgba(22, 163, 74, 0.12);
}

.trace-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin: 30px 0 16px;
  padding-top: 22px;
  border-top: 1px solid var(--line);
}

.trace-heading-title {
  margin: 5px 0 0;
  color: var(--ink);
  font-size: 20px;
  line-height: 1.2;
}

.trace-list {
  border-top: 0;
}

.trace-title {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-right: 12px;
}

.trace-title strong {
  color: var(--ink);
  font-size: 14px;
}

.trace-meta {
  color: var(--ink-soft);
  font-size: 12px;
}

.trace-datasets {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}

.trace-empty {
  color: var(--ink-soft);
  font-size: 12px;
}

.trace-chunk {
  padding: 12px;
  margin-bottom: 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface-soft);
}

.trace-chunk-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.trace-doc {
  color: var(--blue-deep);
  font-size: 12px;
  font-weight: 600;
}

.trace-badge {
  padding: 2px 8px;
  color: var(--ink-soft);
  font-size: 11px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
}

.trace-content {
  max-height: 260px;
  margin: 0;
  padding: 10px;
  overflow: auto;
  color: var(--ink-mid);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  border-radius: 8px;
  background: #fff;
}

.pair-sql {
  color: var(--ink-mid);
  font-family: var(--font-mono);
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
