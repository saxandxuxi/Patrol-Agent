<template>
  <section class="result-panel panel-card">
    <div class="result-header">
      <div>
        <div class="result-eyebrow">结果工作台</div>
        <h2 class="result-title">查询结果</h2>
      </div>
      <div class="result-actions">
        <el-tag v-if="result?.answer_format" effect="plain">{{ result.answer_format }}</el-tag>
        <el-button
          v-if="canApprove(result)"
          size="small"
          type="success"
          plain
          @click="approve()"
        >
          结果正确，存入示例库
        </el-button>
        <el-button v-if="result?.result_id && !result?.sections" size="small" @click="download()">下载 CSV</el-button>
      </div>
    </div>

    <div v-if="!result" class="result-empty">
      <div class="empty-road">
        <span class="road-line"></span>
        <span class="road-node">K20</span>
      </div>
      <h3>等待一次查询</h3>
      <p>表格、图表、图片和来源会在这里展开。</p>
    </div>

    <template v-else>
      <div class="result-tabs">
        <template v-if="resultSections.length">
          <button
            v-for="(section, index) in resultSections"
            :key="section.id"
            type="button"
            :class="{ active: activeSection === index }"
            :title="sectionQuestion(section)"
            @click="activeSection = index"
          >
            Query {{ index + 1 }}
          </button>
        </template>
        <template v-else>
          <button
            v-for="tab in availableTabs"
            :key="tab.key"
            type="button"
            :class="{ active: activeTab === tab.key }"
            @click="activeTab = tab.key"
          >
            {{ tab.label }}
          </button>
        </template>
      </div>

      <div class="result-content">
        <template v-if="resultSections.length">
          <section
            v-for="(section, index) in resultSections"
            v-show="activeSection === index"
            :key="section.id"
            class="sub-section"
          >
            <div class="sub-head">
              <div>
                <span class="result-eyebrow">{{ section.type }}</span>
                <h3>子问题 {{ index + 1 }}</h3>
              </div>
              <div class="sub-actions">
                <el-tag effect="plain">{{ engineLabel(section.engine) }}</el-tag>
                <el-button
                  v-if="canApprove(section)"
                  size="small"
                  type="success"
                  plain
                  @click="approve(section)"
                >
                  结果正确，存入示例库
                </el-button>
                <el-button v-if="section.result_id" size="small" @click="download(section)">下载 CSV</el-button>
              </div>
            </div>

            <div class="sub-question">
              <span>对应子问题</span>
              <p>{{ sectionQuestion(section) }}</p>
            </div>

            <div v-if="section.shape === 'count'" class="count-card compact">
              <span class="count-label">子问题结果</span>
              <div class="count-value">
                <strong>{{ sectionCountValue(section) }}</strong>
                <span>处</span>
              </div>
              <p>{{ section.reply }}</p>
            </div>
            <div v-else class="summary-card">
              <span class="summary-label">结果摘要</span>
              <p>{{ section.reply }}</p>
            </div>

            <div
              v-if="section.chart_data"
              :key="`echarts-${index}`"
              :ref="(el) => setSectionChartRef(el, index)"
              class="chart"
            ></div>

            <div v-if="section.images?.length" class="image-grid">
              <el-image
                v-for="(img, imgIdx) in section.images"
                :key="imgIdx"
                :src="`/api/image?path=${encodeURIComponent(img)}`"
                :preview-src-list="[`/api/image?path=${encodeURIComponent(img)}`]"
                fit="cover"
              />
            </div>

            <el-table
              v-if="section.rows?.length"
              :data="section.rows.slice(0, 100)"
              border
              stripe
              max-height="420"
              size="small"
              class="section-table"
            >
              <el-table-column
                v-for="col in section.columns || []"
                :key="col"
                :prop="col"
                :label="col"
                min-width="120"
                sortable
                show-overflow-tooltip
              />
            </el-table>

            <div v-if="section.references?.length" class="section-references">
              <span class="ref-label">引用</span>
              <el-tag v-for="(ref, refIdx) in dedupe(section.references)" :key="refIdx" effect="plain">
                {{ ref.document_keyword || ref.document_id || 'RAGFlow 文档' }}
              </el-tag>
            </div>

            <details v-if="section.sql" class="sql-details">
              <summary>查看执行 SQL</summary>
              <div class="sql-block">
                <button type="button" class="sql-copy" @click="copySql(section.sql)">复制</button>
                <button
                  type="button"
                  class="sql-probe"
                  :disabled="probingSection === index"
                  @click="probeSection(index, section.sql)"
                >
                  {{ probingSection === index ? '测试中…' : '在测试库上试' }}
                </button>
                <pre>{{ section.sql }}</pre>
              </div>
              <div
                v-if="sectionProbes[index]"
                class="fixture-probe"
                :class="'probe-' + sectionProbes[index].status"
              >
                <strong>{{ sectionProbes[index].label }}</strong>
                <span v-if="sectionProbes[index].rows">
                  · 返回 {{ sectionProbes[index].rows }} 行
                </span>
                <span v-if="sectionProbes[index].sample">
                  · 首行 {{ formatSample(sectionProbes[index].sample) }}
                </span>
                <div v-if="sectionProbes[index].reason" class="probe-reason">
                  {{ sectionProbes[index].reason }}
                </div>
                <div class="probe-note">
                  测试库是按字段目录生成的模拟数据（20 张表 × 12 行），只用来验这条子查询的 SQL 结构。
                </div>
              </div>
            </details>
          </section>
        </template>

        <template v-else>
          <div v-if="activeTab === 'overview'" key="tab-overview" class="overview-tab">
            <div v-if="result.shape === 'count'" class="count-card">
              <span class="count-label">匹配结果</span>
              <div class="count-value">
                <strong>{{ countValue }}</strong>
                <span>处</span>
              </div>
              <p>{{ result.reply }}</p>
            </div>
            <div v-else class="summary-card">
              <span class="summary-label">结果摘要</span>
              <p>{{ result.reply || '查询完成。' }}</p>
            </div>

            <div class="meta-grid">
              <div class="meta-item">
                <span>数据来源</span>
                <strong>{{ result.source || '—' }}</strong>
              </div>
              <div class="meta-item">
                <span>查询形态</span>
                <strong>{{ shapeLabel }}</strong>
              </div>
              <div class="meta-item">
                <span>执行引擎</span>
                <strong>{{ engineLabel(result.engine) }}</strong>
                <!-- 直出（模板/已确认口径/校准）亮这个标：点完卡没亮就说明没走你选的口径 -->
                <em v-if="isDirectResult" class="direct-badge">零大模型直出</em>
              </div>
              <div class="meta-item">
                <span>返回行数</span>
                <strong>{{ result.rows?.length || 0 }}</strong>
              </div>
            </div>

            <div v-if="result.required_conditions?.length" class="condition-list">
              <span>已校验条件</span>
              <div>
                <el-tag v-for="item in result.required_conditions" :key="item" type="success" effect="plain">
                  {{ conditionLabel(item) }}
                </el-tag>
              </div>
            </div>

            <details v-if="result.lineage" class="sql-details">
              <summary>查看数据血缘</summary>
              <pre>{{ JSON.stringify(result.lineage, null, 2) }}</pre>
            </details>
          </div>

          <div v-else-if="activeTab === 'table'" key="tab-table" class="table-tab">
            <el-table :data="pagedRows" border stripe max-height="560" size="small">
              <el-table-column
                v-for="col in result.columns || []"
                :key="col"
                :prop="col"
                :label="col"
                min-width="120"
                sortable
                show-overflow-tooltip
                :align="isNumericColumn(col) ? 'right' : 'left'"
              >
                <template #default="{ row }">
                  <span v-if="isNumericColumn(col)" class="cell-num">
                    {{ row[col] }}<em v-if="unitOf(col)">{{ unitOf(col) }}</em>
                  </span>
                  <span v-else>{{ row[col] }}</span>
                </template>
              </el-table-column>
            </el-table>
            <div v-if="result.rows?.length > pageSize" class="pagination-row">
              <el-pagination
                v-model:current-page="page"
                :page-size="pageSize"
                :total="result.rows.length"
                layout="prev, pager, next, total"
                background
              />
            </div>
          </div>

          <div v-else-if="activeTab === 'chart'" key="tab-chart" class="chart-tab">
            <div v-if="result.chart_data" key="echarts-chart" ref="chartRef" class="chart"></div>
            <el-empty v-else description="这次查询没有可绘制的数值图表" />
          </div>

          <div v-else-if="activeTab === 'images'" key="tab-images" class="images-tab">
            <template v-if="imagePaths.length">
              <!-- 一张表可能有多个图片列（路段封面/路段图片），按列分组并标注，别混成一堆 -->
              <section v-for="(group, gi) in imageGroups" :key="gi" class="image-group">
                <header v-if="showColumnLabels && group.label" class="image-group-title">
                  {{ group.label }}
                  <span class="image-group-count">{{ group.images.length }} 张</span>
                </header>
                <div class="image-grid">
                  <div v-for="(img, idx) in group.images" :key="idx" class="image-cell">
                    <el-image
                      :src="imageSrc(img)"
                      :preview-src-list="[imageSrc(img)]"
                      fit="cover"
                    />
                    <!-- 同一张图被多少条记录共用：采集端路径重复时，用户会以为"每次就那几张图" -->
                    <span v-if="sharedCount(img) > 1" class="image-shared-badge">
                      共用 {{ sharedCount(img) }} 条
                    </span>
                  </div>
                </div>
              </section>
            </template>
            <el-empty v-else description="这次查询没有图片结果" />
          </div>

          <div v-else key="tab-sources" class="sources-tab">
            <div v-if="result.references?.length" class="source-list">
              <article v-for="(ref, idx) in dedupe(result.references)" :key="idx" class="source-card">
                <div class="source-title">
                  <span>{{ ref.document_keyword || '知识库文档' }}</span>
                  <el-tag v-if="ref.similarity" size="small" type="info" effect="plain">
                    {{ Number(ref.similarity).toFixed(2) }}
                  </el-tag>
                </div>
                <p>{{ ref.content }}</p>
              </article>
            </div>
            <el-empty v-else description="这次查询没有知识引用" />
          </div>

          <details v-if="result.sql" class="sql-details">
            <summary>查看执行 SQL</summary>
            <div class="sql-block">
              <button type="button" class="sql-copy" @click="copySql(result.sql)">复制</button>
              <button type="button" class="sql-probe" :disabled="probing" @click="probeFixture">
                {{ probing ? '测试中…' : '在测试库上试' }}
              </button>
              <pre>{{ result.sql }}</pre>
            </div>
            <div v-if="fixtureProbe" class="fixture-probe" :class="'probe-' + fixtureProbe.status">
              <strong>{{ fixtureProbe.label }}</strong>
              <span v-if="fixtureProbe.rows"> · 返回 {{ fixtureProbe.rows }} 行</span>
              <span v-if="fixtureProbe.sample"> · 首行 {{ formatSample(fixtureProbe.sample) }}</span>
              <div v-if="fixtureProbe.reason" class="probe-reason">{{ fixtureProbe.reason }}</div>
              <div class="probe-note">
                测试库是按字段目录生成的模拟数据（18 张表 × 12 行），只用来验 SQL 结构：
                0 行值得核对 join/条件，模板跑不通多为方言差异，都不是真答案。
              </div>
            </div>
          </details>
        </template>
      </div>
    </template>
  </section>
</template>

<script setup>
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const props = defineProps({
  result: { type: Object, default: null },
  images: { type: Array, default: () => [] },
})

// 「在测试库上试」：把当前这条结果的 SQL 丢给后端，在**夹具库**（本地 SQLite）上跑一遍。
// 只影响这一小块展示，不改答案、不改排序（后端也只会碰夹具库，不碰业务库）。
const probing = ref(false)
const fixtureProbe = ref(null)
// 复合题（拆成多个子问题）的每条 SQL 各自探测：index → 结论，另记哪一条正在跑
const probingSection = ref(-1)
const sectionProbes = ref({})

// 多列图片：后端 image_items 带了 column_label（路段封面 / 路段图片 / 病害图片…），
// 这里按列分组，让用户知道每张图出自哪一列。只有一列时不加标题，保持原来的平铺观感。
// 图片来源：优先用传入的 images；没传就看结果本身带的（看板刷新 / "查看这次结果"走这条）。
const imagePaths = computed(() => {
  const given = props.images || []
  if (given.length) return given
  return props.result?.images || []
})
const imageGroups = computed(() => {
  const labels = new Map()
  for (const item of props.result?.image_items || []) {
    const path = String(item?.path || '')
    if (path) labels.set(path, String(item?.column_label || item?.column || ''))
  }
  const groups = []
  for (const img of imagePaths.value) {
    const label = labels.get(String(img)) || ''
    let group = groups.find((item) => item.label === label)
    if (!group) {
      group = { label, images: [] }
      groups.push(group)
    }
    group.images.push(img)
  }
  return groups
})
const showColumnLabels = computed(
  () => imageGroups.value.filter((group) => group.label).length > 1,
)
// 同一张图被多少条记录共用（后端 image_items 里的 shared_records）：库里的图片路径
// 高度重复时，标出来用户才知道"不是系统只给了这几张"。（路径不在 map 里就按 1 处理）
const sharedCounts = computed(() => {
  const counts = {}
  for (const item of props.result?.image_items || []) {
    const path = String(item?.path || '')
    if (path) counts[path] = Number(item?.shared_records || 1)
  }
  return counts
})
function sharedCount(path) {
  return sharedCounts.value[String(path)] || 1
}

// 图片墙取图：逐张请求是"一张一次 HTTP + 一次 SSH 往返"（后端串行加锁），几十张要十几秒；
// 改成按批请求（后端一条远程命令取回一批），拿回 data URL 直接渲染。取不到时回退单张接口。
const imageData = ref({})
const imageDataLoading = ref(false)
async function loadImageData() {
  const todo = imagePaths.value.filter((path) => path && !imageData.value[path])
  if (!todo.length || imageDataLoading.value) return
  imageDataLoading.value = true
  try {
    for (let index = 0; index < todo.length; index += 30) {
      const chunk = todo.slice(index, index + 30)
      const payload = await api.imagesBatch(chunk)
      const merged = { ...imageData.value }
      for (const item of payload.items || []) merged[item.path] = item.data
      imageData.value = merged
    }
  } catch {
    // 批量取图失败就继续用单张 /api/image，不影响看图
  } finally {
    imageDataLoading.value = false
  }
}
function imageSrc(path) {
  return imageData.value[path] || `/api/image?path=${encodeURIComponent(path)}`
}

async function probeSection(index, sql) {
  if (!sql) return
  probingSection.value = index
  try {
    sectionProbes.value = {
      ...sectionProbes.value,
      [index]: await api.fixtureProbe(String(sql), 'mysql'),
    }
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    probingSection.value = -1
  }
}

async function probeFixture() {
  const sql = String(props.result?.sql || '')
  if (!sql) return
  probing.value = true
  try {
    fixtureProbe.value = await api.fixtureProbe(sql, String(props.result?.source || 'mysql'))
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    probing.value = false
  }
}

// 首行摘要（聚合题看值、明细题看第一行字段）——别把整行糊上去，卡片要能一眼看完
function formatSample(sample) {
  if (!sample) return ''
  const parts = Object.entries(sample).slice(0, 4).map(([key, value]) => `${key}=${value}`)
  return parts.join('，')
}

const activeTab = ref('overview')
const activeSection = ref(0)
const chartRef = ref(null)
const page = ref(1)
const pageSize = ref(25)
const sectionChartRefs = new Map()
const approvedIds = ref([])

// 单轮编译链曾把子段写成 answer/preview_rows，而 ReAct/看板写 reply/rows。
// 在展示层做一次兼容归一，历史结果不需要重跑也能正常显示。
const resultSections = computed(() => {
  const sections = props.result?.sections || []
  return sections.map((section, index) => {
    const rows = Array.isArray(section.rows)
      ? section.rows
      : Array.isArray(section.preview_rows)
        ? section.preview_rows
        : []
    return {
      ...section,
      id: section.id ?? section.node_id ?? index,
      type: section.type || 'data',
      reply: section.reply ?? section.answer ?? '',
      rows,
      row_count: Number(section.row_count ?? section.rows_fetched ?? rows.length),
      columns: Array.isArray(section.columns) ? section.columns : [],
    }
  })
})

// 切到「图片」页签才去批量取图（放在 activeTab 声明**之后**：watch 在 setup 阶段就会执行，
// 抢在 const 前面引用会直接抛 "Cannot access 'activeTab' before initialization"，
// 整个结果面板渲染不出来——实测空白过一次）。
watch([imagePaths, activeTab], () => {
  if (activeTab.value === 'images') loadImageData()
})

function canApprove(target) {
  if (!target?.reviewable || !target?.result_id) return false
  return !approvedIds.value.includes(target.result_id)
}

// 引擎名要如实反映 SQL 是谁生成的：模板 / Wren 语义层 / 内置大模型。
const ENGINE_LABELS = {
  template: '确定性模板',
  template_match: '确定性模板（结构匹配）',
  chosen_code_template: '确定性模板（已确认）',
  chosen_preset: '预置模板（已确认）',
  chosen_auto: '自动沉淀模板（已确认）',
  chosen_generic: '通用算子（已确认）',
  chosen_example: '已存示例（已确认）',
  chosen_example_direct: '相似问法（已确认）',
  preset: '预置模板',
  auto: '自动沉淀模板',
  generic: '通用算子',
  saved: '已存示例',
  saved_example: '已存示例',
  calibrated: '范围校准（未执行查询）',
  post_calc: '后置计算',
  'dialogue-state': '对话状态',
  metadata: '数据库元数据查询',
  profiling: '数据画像',
  wren: 'Wren 语义层',
  llm: '内置大模型',
  builtin: '内置引擎',
}

function engineLabel(value) {
  return ENGINE_LABELS[value] || value || ENGINE_LABELS.builtin
}

// 确定性直出：模板/已确认口径/校准结果，全程不调大模型生成 SQL
const DIRECT_ENGINES = new Set([
  'template',
  'template_match',
  'preset',
  'auto',
  'generic',
  'calibrated',
  'post_calc',
  'saved',
  'saved_example',
])
const isDirectResult = computed(() => {
  const engine = String(props.result?.engine || '')
  if (props.result?.direct) return true
  return engine.startsWith('chosen_') || DIRECT_ENGINES.has(engine)
})

async function approve(section = null) {
  const resultId = section?.result_id || props.result?.result_id
  if (!resultId) return
  try {
    await api.approveResult(resultId)
    approvedIds.value = [...approvedIds.value, resultId]
    ElMessage.success('已存入问答示例，后续相似问题会参考它')
  } catch (err) {
    ElMessage.error(err.message)
  }
}
// key -> { el, instance }：Vue 重建元素时同步重建实例，避免画到已失效的节点。
const echartsTargets = new Map()

function firstCountValue(target) {
  const row = target?.rows?.[0] || {}
  return row.count ?? row.COUNT ?? Object.values(row)[0] ?? 0
}

const countValue = computed(() => firstCountValue(props.result))

function sectionCountValue(section) {
  return firstCountValue(section)
}

const shapeLabel = computed(() => {
  const labels = {
    count: '计数',
    distribution: '分布',
    list: '明细',
    photo: '图片',
    trend: '趋势',
    detail: '明细/聚合',
  }
  return labels[props.result?.shape] || '查询'
})

const pagedRows = computed(() => {
  const rows = props.result?.rows || []
  const start = (page.value - 1) * pageSize.value
  return rows.slice(start, start + pageSize.value)
})

const availableTabs = computed(() => {
  const tabs = [{ key: 'overview', label: '概览' }]
  if (props.result?.columns?.length) tabs.push({ key: 'table', label: '表格' })
  // 没有可画的内容就不给「图表」入口，避免"每个问题都出图"。
  if (props.result?.chart_data) {
    tabs.push({ key: 'chart', label: '图表' })
  }
  if (imagePaths.value.length) tabs.push({ key: 'images', label: '图片' })
  tabs.push({ key: 'sources', label: '来源' })
  return tabs
})

function dedupe(references = []) {
  const seen = new Set()
  return references.filter((ref) => {
    const key = ref.document_keyword || ref.document_id || ref.id
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function conditionLabel(value) {
  return {
    route: '路线',
    pile_range: '桩号',
    damage_type: '病害类型',
    time_range: '时间',
  }[value] || value
}

function sectionQuestion(section) {
  const question = String(section?.question || '').trim()
  if (question) return question
  return String(section?.title || '').replace(/^\d+\.\s*/, '').trim() || '未记录子问题'
}

function chartOption(data) {
  const isPie = data.type === 'pie'
  if (isPie) {
    return {
      tooltip: { trigger: 'item' },
      legend: { type: 'scroll', orient: 'vertical', right: 10, top: 20, bottom: 20 },
      series: (data.series || []).map((item) => ({
        name: item.name,
        type: 'pie',
        radius: ['35%', '65%'],
        data: (data.x || []).map((name, index) => ({
          name,
          value: item.data?.[index] ?? 0,
        })),
      })),
    }
  }
  return {
    tooltip: { trigger: 'axis' },
    legend: { show: true },
    grid: { left: 44, right: 24, top: 36, bottom: 42 },
    xAxis: { type: 'category', data: data.x || [] },
    yAxis: { type: 'value' },
    series: (data.series || []).map((item) => ({
      name: item.name,
      type: data.type === 'line' ? 'line' : 'bar',
      data: item.data || [],
    })),
  }
}

function setSectionChartRef(el, index) {
  if (el) sectionChartRefs.set(index, el)
  else sectionChartRefs.delete(index)
}

function renderEChart(key, el, data) {
  if (!el || !data) return
  let holder = echartsTargets.get(key)
  if (!holder || holder.el !== el) {
    holder?.instance?.dispose()
    holder = { el, instance: echarts.init(el) }
    echartsTargets.set(key, holder)
  }
  holder.instance.setOption(chartOption(data), true)
  holder.instance.resize()
}

function renderCharts() {
  nextTick(() => {
    if (!resultSections.value.length) {
      renderEChart('main', chartRef.value, props.result?.chart_data)
      return
    }
    for (const [index, el] of sectionChartRefs.entries()) {
      renderEChart(`section-${index}`, el, resultSections.value[index]?.chart_data)
    }
  })
}

// 数字列右对齐 + 带单位（单位灰色小字），别让"面积/处数"看着像文本
const NUMERIC_LABELS = {
  处数: '处',
  数量: '处',
  病害数量: '处',
  面积: '㎡',
  总面积: '㎡',
  平均面积: '㎡',
  最大面积: '㎡',
  占比: '%',
  密度: '处/km',
  里程: 'km',
  平均速度: 'km/h',
  置信度: '',
}

function isNumericColumn(col) {
  const name = String(col || '')
  if (name in NUMERIC_LABELS) return true
  if (/面积|数量|处数|占比|密度|里程|速度|置信度|总数|合计/.test(name)) return true
  const rows = props.result?.rows || []
  const sample = rows.slice(0, 5).map((row) => row?.[col]).filter((v) => v !== null && v !== undefined && v !== '')
  return sample.length > 0 && sample.every((v) => !Number.isNaN(Number(v)) && String(v).trim() !== '')
}

function unitOf(col) {
  const name = String(col || '')
  if (name in NUMERIC_LABELS) return NUMERIC_LABELS[name]
  if (/面积/.test(name)) return '㎡'
  if (/处数|数量|总数/.test(name)) return '处'
  if (/占比|百分比/.test(name)) return '%'
  return ''
}

async function copySql(sql) {
  try {
    await navigator.clipboard.writeText(String(sql || ''))
    ElMessage.success('SQL 已复制')
  } catch {
    ElMessage.error('复制失败，请手动选中')
  }
}

function download(section = null) {
  const resultId = section?.result_id || props.result?.result_id
  if (resultId) {
    window.open(`/api/export?result_id=${encodeURIComponent(resultId)}`, '_blank')
  }
}

function focusSources() {
  if (resultSections.value.length) return
  activeTab.value = 'sources'
}

function resizeCharts() {
  echartsTargets.forEach((holder) => holder.instance?.resize())
}

watch(
  () => props.result,
  async () => {
    page.value = 1
    activeTab.value = props.result?.shape === 'list' ? 'table' : 'overview'
    activeSection.value = 0
    await nextTick()
    renderCharts()
  },
)

watch([activeTab, activeSection], async () => {
  await nextTick()
  renderCharts()
})

defineExpose({ focusSources })

// KeepAlive 切回对话模块时容器尺寸可能变化，重新渲染一次。
onActivated(() => {
  renderCharts()
})

onMounted(() => window.addEventListener('resize', resizeCharts))
onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeCharts)
  echartsTargets.forEach((holder) => holder.instance?.dispose())
  echartsTargets.clear()
})
</script>

<style scoped>
.direct-badge {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 999px;
  background: #e8f6ec;
  color: #1f7a3d;
  font-size: 11px;
  font-style: normal;
}

.result-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}

.result-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  padding: 20px 20px 14px;
  border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, #fff, #fbfdff);
}

.result-title {
  margin: 4px 0 0;
  font-size: 20px;
}

.result-actions,
.sub-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.result-empty {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 0;
  padding: 36px;
  color: var(--ink-soft);
  text-align: center;
}

.result-empty h3 {
  margin: 18px 0 6px;
  color: var(--ink);
  font-size: 18px;
}

.result-empty p {
  margin: 0;
  font-size: 13px;
}

.empty-road {
  position: relative;
  width: 220px;
  height: 48px;
}

.road-line {
  position: absolute;
  top: 22px;
  right: 0;
  left: 0;
  height: 4px;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--amber) 0 42px, transparent 42px 60px, var(--blue) 60px 100%);
  opacity: 0.75;
}

.road-node {
  position: absolute;
  top: 9px;
  left: 42px;
  padding: 3px 7px;
  color: #fff;
  font-family: var(--font-mono);
  font-size: 11px;
  border-radius: 6px;
  background: var(--ink);
}

.result-tabs {
  display: flex;
  gap: 4px;
  padding: 10px 16px 0;
  overflow-x: auto;
  border-bottom: 1px solid var(--line);
  background: #fff;
}

.result-tabs button {
  flex: 0 0 auto;
  padding: 8px 12px 10px;
  color: var(--ink-soft);
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  cursor: pointer;
}

.result-tabs button.active {
  color: var(--blue);
  border-bottom-color: var(--blue);
  font-weight: 700;
}

.result-tabs button {
  display: inline-flex;
  max-width: 280px;
  align-items: center;
  gap: 6px;
}

.result-content {
  flex: 1;
  min-height: 0;
  padding: 18px 20px 20px;
  overflow: auto;
  background: var(--surface-soft);
}

.count-card,
.summary-card {
  padding: 24px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: #fff;
  box-shadow: var(--shadow-sm);
}

.count-card.compact {
  padding: 18px;
}

.count-label,
.summary-label {
  color: var(--blue);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.count-value {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 8px;
}

.count-value strong {
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: clamp(48px, 7vw, 84px);
  line-height: 1;
}

.count-card.compact .count-value strong {
  font-size: clamp(40px, 5vw, 64px);
}

.count-value span {
  color: var(--ink-soft);
  font-size: 18px;
}

.count-card p,
.summary-card p {
  margin: 14px 0 0;
  color: var(--ink-mid);
  line-height: 1.8;
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 14px;
}

.meta-item {
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fff;
}

.meta-item span {
  display: block;
  margin-bottom: 5px;
  color: var(--ink-soft);
  font-size: 11px;
}

.meta-item strong {
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: 13px;
}

.condition-list {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 14px;
  color: var(--ink-soft);
  font-size: 12px;
}

.condition-list > div {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.sub-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.sub-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
}

.sub-head h3 {
  margin: 4px 0 0;
  font-size: 18px;
}

.sub-question {
  padding: 12px 14px;
  border-left: 3px solid var(--blue);
  border-radius: 0 10px 10px 0;
  background: #f5f8ff;
}

.sub-question span {
  color: var(--blue);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.sub-question p {
  margin: 5px 0 0;
  color: var(--ink);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.65;
}

.section-table {
  margin-top: 4px;
}

.section-references {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.ref-label {
  color: var(--ink-soft);
  font-size: 11px;
}

.table-tab,
.chart-tab,
.images-tab,
.sources-tab,
.overview-tab {
  min-height: 100%;
}

.pagination-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.chart {
  height: clamp(300px, 48vh, 520px);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: #fff;
}

.image-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}

.image-group + .image-group {
  margin-top: 14px;
}

.image-group-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-strong, #303133);
}

.image-group-count {
  font-weight: 400;
  font-size: 12px;
  color: var(--muted, #909399);
}

.image-grid :deep(.el-image) {
  height: 190px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
}

.image-cell {
  position: relative;
}

.image-cell :deep(.el-image) {
  width: 100%;
}

.image-shared-badge {
  position: absolute;
  right: 6px;
  bottom: 6px;
  padding: 1px 6px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 11px;
  line-height: 16px;
  pointer-events: none;
}

.source-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.source-card {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: #fff;
}

/* 数字列右对齐 + 灰色小字单位 */
.cell-num {
  display: inline-flex;
  gap: 3px;
  align-items: baseline;
  justify-content: flex-end;
  width: 100%;
}

.cell-num em {
  color: var(--ink-soft);
  font-size: 11px;
  font-style: normal;
}

/* 执行 SQL：深色代码块 + 复制 */
.sql-block {
  position: relative;
}

.sql-block pre {
  /* 白纸黑字：以前黑底浅蓝字在卡片里根本看不清 */
  padding: 12px 46px 12px 12px;
  color: #1b2432;
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
  padding: 2px 8px;
  color: #1b2432;
  font-size: 11px;
  cursor: pointer;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #f4f7fc;
}

.source-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--ink);
  font-size: 13px;
  font-weight: 700;
}

.source-card p {
  margin: 10px 0 0;
  color: var(--ink-mid);
  font-size: 12px;
  line-height: 1.75;
}

.sql-details {
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: #fff;
}

.sql-details summary {
  padding: 11px 14px;
  color: var(--ink-mid);
  font-size: 12px;
  cursor: pointer;
}

.sql-details pre {
  margin: 0;
  padding: 0 14px 14px;
  overflow: auto;
  color: #0b2f56;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.7;
  white-space: pre-wrap;
}

@media (max-width: 900px) {
  .meta-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

/* 「在测试库上试」：按钮 + 三态结果块 */
.sql-probe {
  position: absolute;
  top: 8px;
  right: 64px;
  padding: 2px 8px;
  color: #1b2432;
  font-size: 11px;
  cursor: pointer;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: #fff;
}

.sql-probe:disabled {
  cursor: progress;
  opacity: 0.6;
}

.fixture-probe {
  margin-top: 8px;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.6;
  border-left: 3px solid var(--line);
  border-radius: 4px;
  background: #f7f9fc;
}

.fixture-probe.probe-ok {
  border-left-color: #2f9e44;
  background: #f2fbf4;
}

.fixture-probe.probe-empty,
.fixture-probe.probe-unsupported {
  border-left-color: #e8a33d;
  background: #fff9ee;
}

.fixture-probe.probe-error {
  border-left-color: #d64545;
  background: #fdf2f2;
}

.fixture-probe .probe-reason {
  margin-top: 2px;
  color: #8a4b08;
}

.fixture-probe .probe-note {
  margin-top: 4px;
  color: var(--muted, #7a8699);
  font-size: 11px;
}
</style>
