<template>
  <section class="page-card">
    <div class="page-heading">
      <div>
        <div class="page-eyebrow">Deterministic Query</div>
        <h1 class="page-title">快捷查询</h1>
        <p class="page-description">
          全部是写死的 SQL（不含大模型）：人工预置 + 从问答库自动沉淀的参数化模板，选好参数一键出数；
          通用查询可以自己挑表、字段和条件。复杂或随意的问法请回「对话」。
        </p>
      </div>
    </div>

    <el-tabs v-model="tab" class="preset-tabs">
      <el-tab-pane label="标准模板（带参数）" name="template">
        <div class="preset-layout">
          <div class="panel-card preset-form">
            <div class="card-heading">
              <h2>选一条查询</h2>
              <span>共 {{ activeQueries.length }} 条模板</span>
            </div>

            <el-form label-position="top">
              <el-form-item label="来源">
                <el-radio-group v-model="source" @change="onSourceChange">
                  <!-- Element Plus 3.0 起 `label` 不再当值用（控制台已报 deprecation 警告），
                       统一改成 value（SchemaExplorer.vue 早就是新写法）。 -->
                  <el-radio-button value="preset">人工预置</el-radio-button>
                  <el-radio-button value="auto">
                    自动沉淀{{ autoQueries.length ? `（${autoQueries.length}）` : '' }}
                  </el-radio-button>
                </el-radio-group>
                <el-button
                  v-if="source === 'auto'"
                  size="small"
                  text
                  :loading="autoLoading"
                  @click="loadAuto"
                >
                  刷新
                </el-button>
              </el-form-item>
              <el-form-item v-if="source === 'preset'" label="分类">
                <el-select v-model="category" style="width: 100%" @change="onCategoryChange">
                  <el-option v-for="item in categories" :key="item" :label="item" :value="item" />
                </el-select>
              </el-form-item>
              <el-form-item label="查询">
                <el-select v-model="queryId" filterable style="width: 100%" @change="onQueryChange">
                  <el-option
                    v-for="item in activeQueries"
                    :key="item.id"
                    :label="item.title"
                    :value="item.id"
                  />
                </el-select>
              </el-form-item>
              <el-form-item
                v-for="spec in currentParamSpecs"
                v-show="isParamVisible(spec)"
                :key="spec.name"
                :label="paramLabel(spec)"
              >
                <!-- 时间按天选（起止各自一个）；有枚举就下拉；数值阈值用输入框 -->
                <el-date-picker
                  v-if="spec.type === 'time'"
                  :key="`${spec.name}-${values.date_granularity || 'day'}`"
                  v-model="values[spec.name]"
                  :type="datePickerType()"
                  :value-format="dateValueFormat()"
                  :format="dateDisplayFormat()"
                  :placeholder="datePlaceholder()"
                  :disabled-date="disableFutureDate"
                  style="width: 100%"
                  @change="onDateValueChange(spec.name)"
                />
                <el-select
                  v-else-if="(spec.options || []).length"
                  v-model="values[spec.name]"
                  filterable
                  style="width: 100%"
                  @change="spec.name === 'date_granularity' && onDateGranularityChange()"
                >
                  <el-option
                    v-for="opt in spec.options"
                    :key="opt.value"
                    :label="opt.label"
                    :value="opt.value"
                  />
                </el-select>
                <el-input v-else v-model="values[spec.name]" placeholder="填数值" />
                <span v-if="spec.name === primaryTimeParamName" class="param-hint">
                  {{ dateRangeHint }}
                </span>
                <span v-else-if="spec.hint" class="param-hint">{{ spec.hint }}</span>
              </el-form-item>
              <div class="form-actions">
                <el-button type="primary" :loading="loading" :disabled="!queryId" @click="run">
                  运行（不走大模型）
                </el-button>
              </div>
            </el-form>

            <p v-if="current" class="preset-scope">
              <strong>口径</strong>：{{ current.scope || '—' }}<br />
              <strong>等价问法</strong>：{{ current.question }}
              <template v-if="source === 'auto' && (current.variants || []).length">
                <br />
                <strong>沉淀来源</strong>：{{ current.variants.length }} 条已验证问法
              </template>
              <template v-if="source === 'auto' && hasDefaults">
                <br />
                <span class="param-hint">参数默认值来自这条模板验证过的那次问法，可直接改。</span>
              </template>
            </p>
          </div>

          <div class="panel-card preset-result">
            <div class="card-heading">
              <h2>结果</h2>
              <span v-if="result">{{ (result.rows || []).length }} 行 · {{ result.engine }}</span>
            </div>
            <p v-if="!result" class="preset-empty">左边选一条查询，点「运行」。</p>
            <template v-else>
              <pre class="preset-reply">{{ result.reply }}</pre>
              <div v-if="!(result.rows || []).length" class="empty-result">
                没有符合条件的数据（0 行）。换个参数再试，或回「对话」用自然语言问。
              </div>
              <el-table
                v-else
                :data="result.rows.slice(0, 200)"
                border
                stripe
                size="small"
                max-height="520"
              >
                <el-table-column
                  v-for="col in result.columns || []"
                  :key="col"
                  :prop="col"
                  :label="col"
                  min-width="140"
                  show-overflow-tooltip
                />
              </el-table>
              <p v-if="(result.rows || []).length > 200" class="preset-empty">
                已展示前 200 行，共 {{ result.rows.length }} 行。
              </p>
            </template>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="通用查询（任意表 / 字段 / 条件）" name="generic">
        <div class="generic-panel">
          <div class="panel-card generic-form-card">
            <div class="card-heading">
              <h2>我要查什么</h2>
              <span>表 / 字段 / 条件都从数据库注释来；编码字段可直接选中文值</span>
            </div>
            <el-form label-position="top" class="generic-form">
              <el-form-item label="要做什么">
                <el-select v-model="gen.operator" style="width: 100%" @change="onOperatorChange">
                  <el-option v-for="op in operators" :key="op.id" :label="op.label" :value="op.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="库 / 表">
                <el-select v-model="gen.kind" style="width: 110px" @change="onKindChange">
                  <el-option label="MySQL" value="mysql" />
                  <el-option label="PostgreSQL" value="postgresql" />
                </el-select>
                <el-select v-model="gen.table" filterable style="flex: 1" placeholder="选表">
                  <el-option
                    v-for="item in tables"
                    :key="item.table"
                    :label="`${item.label}（${item.table}）`"
                    :value="item.table"
                  />
                </el-select>
              </el-form-item>
              <el-form-item v-if="needColumns" label="要看的字段（可多选，不选=默认列）">
                <el-select v-model="gen.columns" multiple filterable style="width: 100%">
                  <el-option
                    v-for="col in columns"
                    :key="col.name"
                    :label="`${col.label}（${col.name}）`"
                    :value="col.name"
                  />
                </el-select>
              </el-form-item>
              <el-form-item
                v-if="needGroupColumn"
                :label="gen.operator === 'time_series' ? '时间列' : '分组字段'"
              >
                <el-select v-model="gen.group_column" filterable style="width: 100%">
                  <el-option
                    v-for="col in columns"
                    :key="col.name"
                    :label="`${col.label}（${col.name}）`"
                    :value="col.name"
                  />
                </el-select>
              </el-form-item>
              <el-form-item v-if="gen.operator === 'time_series'" label="时间粒度">
                <el-select v-model="gen.time_unit" style="width: 160px">
                  <el-option label="按年" value="year" />
                  <el-option label="按季" value="quarter" />
                  <el-option label="按月" value="month" />
                  <el-option label="按周" value="week" />
                  <el-option label="按日" value="day" />
                  <el-option label="按小时" value="hour" />
                </el-select>
              </el-form-item>
              <el-form-item v-if="gen.operator === 'rank'" label="指标与取前几名">
                <el-select v-model="gen.agg" style="width: 110px">
                  <el-option label="条数" value="count" />
                  <el-option label="合计" value="sum" />
                  <el-option label="平均" value="avg" />
                  <el-option label="最大" value="max" />
                  <el-option label="最小" value="min" />
                </el-select>
                <el-select
                  v-model="gen.metric_column"
                  clearable
                  filterable
                  style="flex: 1"
                  placeholder="指标列（条数不用选）"
                >
                  <el-option
                    v-for="col in columns"
                    :key="col.name"
                    :label="`${col.label}（${col.name}）`"
                    :value="col.name"
                  />
                </el-select>
                <el-input-number v-model="gen.top" :min="1" :max="200" size="small" />
              </el-form-item>
              <el-form-item v-if="gen.operator === 'detail'" label="明细上限与排序">
                <el-input-number v-model="gen.limit" :min="1" :max="5000" size="small" />
                <el-select
                  v-model="gen.order_column"
                  clearable
                  filterable
                  style="flex: 1"
                  placeholder="排序字段（可不选）"
                >
                  <el-option
                    v-for="col in columns"
                    :key="col.name"
                    :label="`${col.label}（${col.name}）`"
                    :value="col.name"
                  />
                </el-select>
                <el-select v-model="gen.order_direction" style="width: 100px">
                  <el-option label="降序" value="desc" />
                  <el-option label="升序" value="asc" />
                </el-select>
              </el-form-item>
              <el-form-item v-if="gen.operator === 'profile'" label="范围">
                <el-checkbox v-model="gen.only_empty">只看整列为空的字段</el-checkbox>
              </el-form-item>
              <el-form-item v-if="supportsFilters" label="过滤条件（编码字段可直接选中文）">
                <div class="filter-rows">
                  <div v-for="(row, index) in gen.filters" :key="index" class="filter-row">
                    <el-select v-model="row.column" filterable style="width: 220px" placeholder="字段">
                      <el-option
                        v-for="col in columns"
                        :key="col.name"
                        :label="`${col.label}（${col.name}）`"
                        :value="col.name"
                      />
                    </el-select>
                    <el-select v-model="row.op" style="width: 120px">
                      <el-option v-for="op in OPS" :key="op.value" :label="op.label" :value="op.value" />
                    </el-select>
                    <el-select
                      v-if="codeOptions(row.column).length"
                      v-model="row.value"
                      style="width: 200px"
                      placeholder="取值（中文）"
                    >
                      <el-option
                        v-for="opt in codeOptions(row.column)"
                        :key="opt.value"
                        :label="opt.label"
                        :value="opt.value"
                      />
                    </el-select>
                    <el-input v-else v-model="row.value" style="width: 200px" placeholder="取值" />
                    <el-button text size="small" @click="gen.filters.splice(index, 1)">删除</el-button>
                  </div>
                  <el-button size="small" @click="addFilter">+ 加条件</el-button>
                </div>
              </el-form-item>
              <div class="form-actions">
                <el-button
                  type="primary"
                  :loading="genericLoading"
                  :disabled="!gen.table || !gen.kind"
                  @click="runGeneric"
                >
                  运行（不走大模型）
                </el-button>
              </div>
            </el-form>
          </div>

          <div class="panel-card generic-result-card">
            <div class="card-heading">
              <h2>结果</h2>
              <span v-if="genericResult">{{ (genericResult.rows || []).length }} 行</span>
            </div>
            <p v-if="!genericResult" class="preset-empty">左边选好表和字段，点「运行」。</p>
            <template v-else>
              <div class="result-head">
                <span class="result-count">{{ (genericResult.rows || []).length }} 行</span>
                <span class="result-scope">{{ genericResult.scope }}</span>
              </div>
              <div v-if="!(genericResult.rows || []).length" class="empty-result">
                没有符合条件的数据（0 行）。放宽条件、换张表，或去掉某个过滤条件再试。
              </div>
              <el-table
                v-else
                :data="genericResult.rows.slice(0, 200)"
                border
                stripe
                size="small"
                max-height="520"
              >
                <el-table-column
                  v-for="col in genericResult.columns || []"
                  :key="col"
                  :prop="col"
                  :label="col"
                  min-width="130"
                  show-overflow-tooltip
                />
              </el-table>
              <details v-if="genericResult.sql" class="sql-details">
                <summary>查看 SQL</summary>
                <pre>{{ genericResult.sql }}</pre>
              </details>
            </template>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<script setup>
import { computed, onActivated, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const queries = ref([])
const params = ref({})
// 通用查询：算子 + 表 + 字段 + 条件，全部来自字段目录（可传参、任意组合）
const operators = ref([])
const tab = ref('template')
const catalogData = ref({ mysql: [], postgresql: [] })
const genericResult = ref(null)
const genericLoading = ref(false)
const OPS = [
  { label: '等于', value: '=' },
  { label: '不等于', value: '!=' },
  { label: '大于', value: '>' },
  { label: '大于等于', value: '>=' },
  { label: '小于', value: '<' },
  { label: '小于等于', value: '<=' },
  { label: '区间', value: 'between' },
  { label: '包含于', value: 'in' },
  { label: '为空', value: 'is null' },
  { label: '不为空', value: 'is not null' },
  { label: '模糊匹配', value: 'like' },
]
const gen = reactive({
  kind: 'mysql',
  operator: 'detail',
  table: '',
  columns: [],
  filters: [],
  group_column: '',
  metric_column: '',
  agg: 'count',
  time_unit: 'month',
  top: 10,
  order_column: '',
  order_direction: 'desc',
  limit: 500,
  only_empty: false,
})

const tables = computed(() => catalogData.value[gen.kind] || [])
const columns = computed(() => tables.value.find((item) => item.table === gen.table)?.columns || [])
const operatorLabel = computed(
  () => (operators.value.find((item) => item.id === gen.operator) || {}).label || '',
)
const needColumns = computed(() => ['detail', 'stats', 'nulls'].includes(gen.operator))
const needGroupColumn = computed(() => ['group', 'time_series', 'rank', 'distinct'].includes(gen.operator))
const supportsFilters = computed(() => ['detail', 'geo'].includes(gen.operator))

function codeOptions(columnName) {
  const col = columns.value.find((item) => item.name === columnName)
  const map = (col || {}).code_map || {}
  return Object.entries(map).map(([value, label]) => ({ value, label }))
}

function addFilter() {
  gen.filters.push({ column: '', op: '=', value: '' })
}

function onKindChange() {
  gen.table = ''
  gen.columns = []
  gen.group_column = ''
  gen.metric_column = ''
  gen.filters = []
  genericResult.value = null
}

function onOperatorChange() {
  gen.columns = []
  gen.group_column = ''
  gen.filters = []
  genericResult.value = null
}

async function loadGenericCatalog() {
  try {
    const data = await api.genericCatalog()
    catalogData.value = data.catalog || {}
    operators.value = data.operators || []
    gen.operator = operators.value[0]?.id || 'detail'
  } catch {
    catalogData.value = { mysql: [], postgresql: [] }
  }
}

async function runGeneric() {
  genericLoading.value = true
  try {
    genericResult.value = await api.runGenericQuery({ ...gen })
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    genericLoading.value = false
  }
}
const category = ref('')
const queryId = ref('')
const values = reactive({})
const result = ref(null)
const loading = ref(false)
// 快捷查询的来源：人工预置（preset_queries.json）/ 自动沉淀（对话里挖出来的参数化模板）
const source = ref('preset')
const autoQueries = ref([])
const autoLoading = ref(false)

const categories = computed(() => [...new Set(queries.value.map((q) => q.category || '其他'))])
const visibleQueries = computed(() =>
  queries.value.filter((q) => (q.category || '其他') === category.value),
)
const activeQueries = computed(() =>
  source.value === 'auto' ? autoQueries.value : visibleQueries.value,
)
const current = computed(() => activeQueries.value.find((q) => q.id === queryId.value) || null)
// 预置模板的参数是「名字数组 + 全局 options 表」，自动模板自带完整 spec（含 options/type），
// 这里统一成同一种 spec 列表给表单渲染。
const currentParamSpecs = computed(() => {
  const item = current.value
  if (!item) return []
  if (source.value === 'auto') return item.params || []
  return (item.params || []).map((name) => ({
    name,
    ...(params.value[name] || {}),
    label: params.value[name]?.label || name,
  }))
})
const timeParamSpecs = computed(() =>
  currentParamSpecs.value.filter((spec) => spec.type === 'time'),
)
const primaryTimeParamName = computed(
  () => timeParamSpecs.value.find((spec) => !isTimeEnd(spec.name))?.name || timeParamSpecs.value[0]?.name || '',
)
const endTimeParamName = computed(
  () => timeParamSpecs.value.find((spec) => isTimeEnd(spec.name))?.name || '',
)
const hasDefaults = computed(() => currentParamSpecs.value.some((spec) => spec.value))

function onSourceChange() {
  queryId.value = activeQueries.value[0]?.id || ''
  onQueryChange()
}

function onCategoryChange() {
  queryId.value = visibleQueries.value[0]?.id || ''
  onQueryChange()
}

function onQueryChange() {
  result.value = null
  syncParams()
}

function datePickerType() {
  return {
    day: 'date',
    week: 'week',
    month: 'month',
    quarter: 'quarter',
    year: 'year',
  }[values.date_granularity || 'day'] || 'date'
}

function dateValueFormat() {
  return {
    day: 'YYYY-MM-DD',
    week: 'YYYY-MM-DD',
    month: 'YYYY-MM',
    quarter: 'YYYY-MM',
    year: 'YYYY',
  }[values.date_granularity || 'day'] || 'YYYY-MM-DD'
}

function dateDisplayFormat() {
  return {
    day: 'YYYY-MM-DD',
    week: 'YYYY年第W周',
    month: 'YYYY年MM月',
    quarter: 'YYYY年Q季度',
    year: 'YYYY年',
  }[values.date_granularity || 'day'] || 'YYYY-MM-DD'
}

function datePlaceholder() {
  return {
    day: '选择日期',
    week: '选择周',
    month: '选择月份',
    quarter: '选择季度',
    year: '选择年份',
  }[values.date_granularity || 'day'] || '选择日期'
}

function dateMatchesGranularity(value, granularity = values.date_granularity || 'day') {
  const text = String(value || '')
  if (granularity === 'week') return /^\d{4}-\d{2}-\d{2}$/.test(text)
  if (granularity === 'month') return /^\d{4}-\d{2}$/.test(text)
  if (granularity === 'quarter') return /^\d{4}-\d{2}$/.test(text)
  if (granularity === 'year') return /^\d{4}$/.test(text)
  return /^\d{4}-\d{2}-\d{2}$/.test(text)
}

const dateRangeHint = computed(() => {
  return {
    day: '按天选择，起止日期都包含当天。',
    week: '按周选择，系统自动覆盖所选整周（周一至周日）。',
    month: '按月选择，系统自动覆盖起止月份之间的完整月份。',
    quarter: '选择一个季度，系统自动覆盖整个自然季度。',
    year: '选择一个年份，系统自动覆盖整个自然年。',
  }[values.date_granularity || 'day']
})

function isParamVisible(spec) {
  if (spec.type !== 'time') return true
  if (
    ['quarter', 'year'].includes(values.date_granularity) &&
    spec.name === endTimeParamName.value
  ) {
    return false
  }
  return true
}

function paramLabel(spec) {
  if (spec.type === 'time' && spec.name === primaryTimeParamName.value) {
    return {
      day: '起始日期',
      week: '起始周',
      month: '起始月份',
      quarter: '季度',
      year: '年份',
    }[values.date_granularity || 'day'] || spec.label
  }
  if (spec.type === 'time' && spec.name === endTimeParamName.value) {
    return {
      day: '结束日期',
      week: '结束周',
      month: '结束月份',
    }[values.date_granularity || 'day'] || spec.label
  }
  return spec.label
}

function isTimeEnd(name) {
  return String(name || '').toLowerCase().endsWith('_end') ||
    String(name || '').toLowerCase().endsWith('_to')
}

function disableFutureDate(dateValue) {
  if (!dateValue) return false
  const today = new Date()
  today.setHours(23, 59, 59, 999)
  return new Date(dateValue).getTime() > today.getTime()
}

function onDateValueChange(name) {
  if (!isTimeEnd(name) && ['quarter', 'year'].includes(values.date_granularity)) {
    const endName = endTimeParamName.value
    if (endName) values[endName] = values[name]
  }
}

function onDateGranularityChange() {
  for (const spec of timeParamSpecs.value) values[spec.name] = ''
  result.value = null
}

// 参数选项是现查的：把还存在的选择保留下来，已经不在库里的换成第一个
function syncParams() {
  const next = {}
  for (const spec of currentParamSpecs.value) {
    const options = spec.options || []
    const previous = values[spec.name]
    const allowed = (value) => !options.length || options.some((opt) => opt.value === value)
    // 优先级：用户填过的 > 模板里验证过的默认值 > 选项第一个。
    // 默认值很关键——自动沉淀的模板有桩号米数、置信度阈值这类槽，空着用户根本不知道该填什么。
    let picked = previous && allowed(previous) ? previous : spec.value || ''
    if (spec.name === 'patrol_task_b' && !picked && options.length > 1) {
      picked = options[1].value
    }
    if (!picked || !allowed(picked)) picked = options[0]?.value || picked || ''
    if (spec.type === 'time') {
      const grainValues = next.date_granularity ? { ...values, ...next } : values
      if (!dateMatchesGranularity(picked, grainValues.date_granularity || 'day')) picked = ''
    }
    next[spec.name] = picked
  }
  for (const key of Object.keys(values)) delete values[key]
  Object.assign(values, next)
}

async function load() {
  try {
    const data = await api.presetQueries()
    queries.value = data.queries || []
    params.value = data.params || {}
    const keep = queries.value.find((q) => q.id === queryId.value)
    category.value = keep?.category || categories.value[0] || ''
    queryId.value = (source.value === 'auto' ? null : keep?.id) || activeQueries.value[0]?.id || ''
    syncParams()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function loadAuto() {
  autoLoading.value = true
  try {
    const data = await api.autoTemplates()
    autoQueries.value = data.templates || []
    if (source.value === 'auto') {
      queryId.value = autoQueries.value.find((q) => q.id === queryId.value)?.id || autoQueries.value[0]?.id || ''
      syncParams()
    }
  } catch {
    autoQueries.value = []
  } finally {
    autoLoading.value = false
  }
}

async function run() {
  if (!current.value) return
  const startName = primaryTimeParamName.value
  const endName = endTimeParamName.value
  if (startName && endName && values[startName] && values[endName] && values[startName] > values[endName]) {
    ElMessage.warning('起始日期不能晚于结束日期')
    return
  }
  loading.value = true
  try {
    result.value =
      source.value === 'auto'
        ? await api.runAutoTemplate(queryId.value, { ...values })
        : await api.runPreset(queryId.value, { ...values })
  } catch (err) {
    result.value = null
    ElMessage.error(err.message)
  } finally {
    loading.value = false
  }
}

// 页面被 KeepAlive 缓存：每次切回这个板块都重新拉一次选项（路线/月份/病害类型都是现查的）
onActivated(async () => {
  await load()
  await loadAuto()
  await loadGenericCatalog()
})
</script>

<style scoped>
.card-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 18px;
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
}

.preset-layout {
  display: grid;
  gap: 18px;
  grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
  align-items: start;
}

.preset-form,
.preset-result {
  padding: 20px;
}

.generic-form :deep(.el-form-item__content) {
  display: flex;
  gap: 8px;
  align-items: center;
}

.filter-rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.filter-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.inline-sql {
  display: block;
  margin-top: 4px;
  padding: 8px 10px;
  color: #1b2432;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
}

/* 结果区：有数就表格，没数也给一句明确结论 */
.generic-result {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed var(--line);
}

.result-head {
  display: flex;
  gap: 10px;
  align-items: baseline;
  margin-bottom: 8px;
}

.result-count {
  padding: 2px 10px;
  color: #14507a;
  font-size: 12px;
  font-weight: 600;
  border-radius: 999px;
  background: #eaf4fd;
}

.result-scope {
  color: var(--ink-soft);
  font-size: 12px;
}

.empty-result {
  padding: 14px 16px;
  color: var(--ink-mid, #4a566b);
  font-size: 13px;
  border: 1px dashed var(--line);
  border-radius: 10px;
  background: #fbfcfe;
}

.sql-details {
  margin-top: 10px;
}

.sql-details summary {
  color: var(--ink-soft);
  font-size: 12px;
  cursor: pointer;
}

.sql-details pre {
  max-height: 260px;
  margin: 6px 0 0;
  padding: 12px;
  overflow: auto;
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

.preset-scope,
.preset-empty {
  color: var(--muted, #6b7280);
  font-size: 13px;
  line-height: 1.7;
}

/* 参数下面的一句说明：这类槽要填什么（桩号米数、阈值区间、起止日期…） */
.param-hint {
  display: block;
  margin-top: 4px;
  color: var(--muted, #6b7280);
  font-size: 12px;
  line-height: 1.5;
}

.preset-reply {
  background: #f6f8fb;
  border-radius: 10px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  margin: 0 0 12px;
  padding: 12px;
  white-space: pre-wrap;
}

@media (max-width: 1100px) {
  .preset-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
