<template>
  <section class="page-card">
    <div class="page-heading">
      <div>
        <div class="page-eyebrow">Data Sources</div>
        <h1 class="page-title">数据源</h1>
        <p class="page-description">
          MySQL / PostgreSQL 的结构由系统表实时读取（information_schema / pg_catalog）；SeaweedFS 是对象存储，
          按「桶 → 目录 → 文件」组织，没有表结构，数据库里只保存文件路径。
        </p>
      </div>
      <div class="page-actions">
        <el-radio-group v-model="source" size="small" @change="onSourceChange">
          <el-radio-button value="mysql">MySQL</el-radio-button>
          <el-radio-button value="postgresql">PostgreSQL</el-radio-button>
          <el-radio-button value="seaweedfs">SeaweedFS</el-radio-button>
        </el-radio-group>
        <el-checkbox
          v-if="source !== 'seaweedfs'"
          v-model="includeRows"
          size="small"
          @change="load"
        >
          显示行数
        </el-checkbox>
        <el-button :loading="loading || storageLoading" @click="refresh">刷新</el-button>
        <!-- 语义层生成器：两套并存，方便 A/B 对比（默认取 .env 的 MDL_GENERATOR） -->
        <el-select
          v-if="source !== 'seaweedfs'"
          v-model="mdlGenerator"
          size="small"
          style="width: 168px"
          :title="generatorNote"
        >
          <el-option
            v-for="item in mdlGenerators"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
        <el-button
          v-if="source !== 'seaweedfs'"
          type="primary"
          plain
          :loading="mdlLoading"
          @click="syncMdl"
        >
          重建语义层
        </el-button>
      </div>
    </div>

    <template v-if="source !== 'seaweedfs'">
      <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon />
      <template v-else>
        <div class="schema-meta">
          <div class="schema-meta-item">
            <span>数据库</span><strong>{{ catalog?.database || '—' }}</strong>
          </div>
          <div class="schema-meta-item">
            <span>表数量</span><strong>{{ catalog?.table_count ?? 0 }}</strong>
          </div>
          <div class="schema-meta-item">
            <span>字段总数</span><strong>{{ catalog?.column_count ?? 0 }}</strong>
          </div>
          <div class="schema-meta-item">
            <span>读取时间</span><strong>{{ catalog?.generated_at || '—' }}</strong>
          </div>
        </div>

        <el-input
          v-model="filter"
          class="schema-filter"
          placeholder="按表名 / 字段名 / 说明过滤"
          clearable
        />

        <el-collapse v-model="activeTables">
          <el-collapse-item v-for="table in visibleTables" :key="table.table" :name="table.table">
            <template #title>
              <div class="schema-title">
                <strong>{{ table.table }}</strong>
                <span class="schema-comment">{{ table.comment || '无表说明' }}</span>
                <el-tag size="small" effect="plain">{{ table.columns.length }} 字段</el-tag>
                <el-tag v-if="table.rows != null" size="small" type="info" effect="plain">
                  {{ table.rows }} 行
                </el-tag>
              </div>
            </template>
            <el-table :data="table.columns" border stripe size="small" max-height="420">
              <el-table-column prop="name" label="字段" min-width="170" />
              <el-table-column prop="type" label="类型" min-width="150" />
              <el-table-column prop="nullable" label="可空" width="80" />
              <el-table-column prop="key" label="键" width="80" />
              <el-table-column prop="comment" label="说明" min-width="240" show-overflow-tooltip />
            </el-table>
          </el-collapse-item>
        </el-collapse>

        <el-empty
          v-if="!loading && visibleTables.length === 0"
          :description="catalog ? '没有匹配的表' : '暂无数据'"
        />
      </template>
    </template>

    <template v-else>
      <el-alert
        class="storage-tip"
        type="info"
        :closable="false"
        show-icon
        title="SeaweedFS 负责存文件，数据库只存路径"
      >
        <template #default>
          <p>
            下表是<b>按字段名自动发现</b>的引用字段（匹配 url / pic / image / photo / path / file），不是写死的表名。
          </p>
          <p>
            「非空 / 总数」= 该表总行数里，这个字段有值的行数。例如 <b>1703 / 1703</b> 表示记录都有路径；
            <b>0 / 88</b> 表示表里有 88 行但这些记录都没配图；<b>无数据（0 行）</b> 表示这张表还没有记录。
          </p>
          <p>注意：<b>字段里有路径 ≠ 文件真的在存储里</b>，两者要分开看。</p>
        </template>
      </el-alert>

      <div class="schema-section">
        <div class="page-eyebrow">References</div>
        <h2 class="schema-section-title">引用对象存储的字段</h2>
      </div>

      <el-table v-loading="refLoading" :data="references" border stripe size="small" max-height="260">
        <el-table-column label="字段" min-width="230">
          <template #default="{ row }">
            <span class="schema-mono">{{ row.table }}.{{ row.column }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="comment" label="说明" min-width="200" show-overflow-tooltip />
        <el-table-column label="非空 / 总数" width="150">
          <template #default="{ row }">{{ fillLabel(row) }}</template>
        </el-table-column>
      </el-table>

      <div class="schema-section">
        <div class="page-eyebrow">Objects</div>
        <h2 class="schema-section-title">对象浏览</h2>
      </div>

      <div class="storage-browser">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item v-for="crumb in breadcrumbs" :key="crumb.path">
            <a class="storage-link" @click.prevent="loadStorage(crumb.path)">{{ crumb.label }}</a>
          </el-breadcrumb-item>
        </el-breadcrumb>
        <el-table
          v-loading="storageLoading"
          :data="storageEntries"
          border
          stripe
          size="small"
          max-height="360"
        >
          <el-table-column label="名称" min-width="300">
            <template #default="{ row }">
              <a v-if="row.is_dir" class="storage-link" @click.prevent="loadStorage(row.path)">
                📁 {{ row.name }}
              </a>
              <span v-else>{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="90">
            <template #default="{ row }">{{ row.is_dir ? '目录' : '文件' }}</template>
          </el-table-column>
          <el-table-column label="大小" width="110">
            <template #default="{ row }">{{ row.is_dir ? '—' : formatSize(row.size) }}</template>
          </el-table-column>
          <el-table-column prop="mtime" label="修改时间" width="230" />
        </el-table>
      </div>
    </template>
  </section>
</template>

<script setup>
import { computed, onActivated, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const source = ref('mysql')
const includeRows = ref(false)
const catalog = ref(null)
const filter = ref('')
const activeTables = ref([])
const loading = ref(false)
const error = ref('')
const mdlLoading = ref(false)
// 语义层生成器：legacy（原）与 official（官方流程版），默认值来自后端 .env
const mdlGenerator = ref('legacy')
const mdlGenerators = ref([
  { value: 'legacy', label: '经典', note: '' },
  { value: 'official', label: '官方流程版', note: '' },
])
const generatorNote = computed(
  () => mdlGenerators.value.find((item) => item.value === mdlGenerator.value)?.note || '',
)

const references = ref([])
const refLoading = ref(false)
const storagePath = ref('buckets')
const storageEntries = ref([])
const storageLoading = ref(false)

const visibleTables = computed(() => {
  const keyword = filter.value.trim().toLowerCase()
  const tables = catalog.value?.tables || []
  if (!keyword) return tables
  return tables
    .map((table) => {
      const tableHit =
        table.table.toLowerCase().includes(keyword) ||
        (table.comment || '').toLowerCase().includes(keyword)
      if (tableHit) return table
      const columns = table.columns.filter(
        (col) =>
          col.name.toLowerCase().includes(keyword) ||
          (col.comment || '').toLowerCase().includes(keyword),
      )
      return columns.length ? { ...table, columns } : null
    })
    .filter(Boolean)
})

const breadcrumbs = computed(() => {
  const parts = storagePath.value.split('/').filter(Boolean)
  const crumbs = []
  let acc = ''
  for (const part of parts) {
    acc = acc ? `${acc}/${part}` : part
    crumbs.push({ label: part === 'buckets' ? '对象存储' : part, path: acc })
  }
  return crumbs
})

async function syncMdl() {
  mdlLoading.value = true
  try {
    const data = await api.syncMdl(
      source.value === 'postgresql' ? 'postgresql' : 'mysql',
      mdlGenerator.value,
    )
    const parts = (data.synced || []).map(
      (item) => {
        const diffs = (item.type_diffs || []).length
        const rel = item.relationships
          ? `，关系 ${(item.relationships.manual || 0) + (item.relationships.auto || 0)} 条`
          : ''
        const valid = item.valid === false ? '（validate 没过）' : ''
        return `${item.project}：${item.models} 个模型 / ${item.columns} 个字段${rel}${
          diffs ? `，类型与经典版不同 ${diffs} 处` : ''
        }${valid}`
      },
    )
    ElMessage.success(`语义层已重建（${data.generator || mdlGenerator.value}：${parts.join('；')}）`)
    await load()
  } catch (exc) {
    ElMessage.error(`重建语义层失败：${exc.message || exc}`)
  } finally {
    mdlLoading.value = false
  }
}

function fillLabel(row) {
  if (row.total == null) return '—'
  if (Number(row.total) === 0) return '无数据（0 行）'
  return `${row.filled ?? 0} / ${row.total}`
}

function formatSize(bytes) {
  if (!bytes) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${index === 0 ? value : value.toFixed(1)} ${units[index]}`
}

async function load() {
  if (source.value === 'seaweedfs') return
  loading.value = true
  error.value = ''
  try {
    catalog.value = await api.schema(source.value, includeRows.value)
    activeTables.value = []
  } catch (err) {
    catalog.value = null
    error.value = err.message
    ElMessage.error(err.message)
  } finally {
    loading.value = false
  }
}

async function loadReferences() {
  refLoading.value = true
  try {
    references.value = await api.storageReferences()
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    refLoading.value = false
  }
}

async function loadStorage(path = storagePath.value) {
  storageLoading.value = true
  try {
    const data = await api.storage(path)
    storagePath.value = data.path || path
    storageEntries.value = data.entries || []
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    storageLoading.value = false
  }
}

function onSourceChange() {
  if (source.value === 'seaweedfs') {
    loadReferences()
    loadStorage('buckets')
  } else {
    load()
  }
}

function refresh() {
  if (source.value === 'seaweedfs') {
    loadReferences()
    loadStorage()
    return
  }
  load()
}

// 语义层生成器清单与默认值都来自后端（.env 的 MDL_GENERATOR 定默认）
async function loadGenerators() {
  try {
    const data = await api.mdlGenerators()
    if (data.options?.length) mdlGenerators.value = data.options
    if (data.default) mdlGenerator.value = data.default
  } catch {
    // 拿不到就用内置的两个选项
  }
}

onActivated(() => {
  loadGenerators()
  if (source.value === 'seaweedfs') {
    loadReferences()
    loadStorage()
    return
  }
  load()
})
</script>

<style scoped>
.page-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.schema-meta {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.schema-meta-item {
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fff;
}

.schema-meta-item span {
  display: block;
  margin-bottom: 5px;
  color: var(--ink-soft);
  font-size: 11px;
}

.schema-meta-item strong {
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: 13px;
}

.schema-filter {
  margin-bottom: 14px;
}

.schema-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.schema-title strong {
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: 13px;
}

.schema-comment {
  color: var(--ink-soft);
  font-size: 12px;
}

.schema-section {
  margin: 24px 0 12px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
}

.schema-section-title {
  margin: 5px 0 0;
  color: var(--ink);
  font-size: 20px;
  line-height: 1.2;
}

.schema-mono {
  font-family: var(--font-mono);
  font-size: 12px;
}

.storage-tip {
  margin-bottom: 6px;
}

.storage-tip p {
  margin: 4px 0;
  line-height: 1.7;
}

.storage-browser {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.storage-link {
  color: var(--blue-deep);
  cursor: pointer;
  text-decoration: none;
}

.storage-link:hover {
  text-decoration: underline;
}

@media (max-width: 900px) {
  .schema-meta {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
