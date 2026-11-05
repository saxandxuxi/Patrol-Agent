<template>
  <section class="page-card">
    <div class="page-heading">
      <div>
        <div class="page-eyebrow">Admin</div>
        <h1 class="page-title">管理</h1>
        <p class="page-description">
          预置查询维护：改完即时生效（也仍然可以直接改 backend/app/data/preset_queries.json）。
          带参数的模板给「快捷查询」用，不带参数的会出现在「固定看板」的系统模板区。
        </p>
      </div>
    </div>

    <section class="panel-card">
      <div class="card-heading">
        <h2>内置模板（只读）</h2>
        <span>代码里的确定性模板，登记在 resources/sql_templates/_index.yaml；只读展示，照着写自己的模板</span>
      </div>
      <el-table :data="registry.builtin || []" size="small" max-height="300">
        <el-table-column prop="id" label="模板 id" width="260" />
        <el-table-column prop="title" label="中文名" width="240" />
        <el-table-column prop="trigger" label="触发语义" show-overflow-tooltip />
        <el-table-column prop="dialect" label="方言" width="100" />
      </el-table>
      <p class="hint" style="margin-top: 12px">
        通用能力（可传参，在「快捷查询 → 通用查询」里直接用）：{{ (registry.generic || []).length }} 个 ·
        前端自建模板：{{ (registry.presets || []).length }} 条
      </p>
    </section>

    <section class="panel-card">
      <div class="card-heading">
        <h2>自动沉淀模板（L1）</h2>
        <span>把"答对了"的成功 SQL 离线挖成参数化模板；命中后仍然要用户在卡片上确认才会执行</span>
        <el-button
          size="small"
          type="primary"
          style="margin-left: auto"
          :loading="refreshing"
          @click="refreshAuto"
        >
          模板刷新（只挖新问答）
        </el-button>
        <el-button size="small" :loading="rebuilding" @click="rebuildAuto">
          全量重挖
        </el-button>
        <el-button size="small" :loading="recalibrating" @click="recalibrateAuto">
          spec 重校准
        </el-button>
        <el-button size="small" :loading="indexing" @click="syncQuestionIndex">
          同步问题索引集
        </el-button>
        <el-button size="small" :loading="reviewing" @click="reviewTemplates">
          LLM 审核（3 条）
        </el-button>
        <el-button size="small" :loading="sweeping" @click="sweepTemplates">
          生命周期巡检
        </el-button>
      </div>
      <p class="hint">
        共 <strong>{{ (registry.auto || []).length }}</strong> 条；停用后不再进对话候选。
        "槽位来源"里 domain=规则按取值域确认（路线/病害类型/时间/阈值），sample=跨样本取过不同值确认（枚举类）。
        <br />
        「模板刷新」只处理新加进示例库 / RAGFlow 的问答对（已挖过的跳过，新成的模板会自己出现在快捷查询）；
        「全量重挖」会清掉台账重算全部；「同步问题索引集」对账 RAGFlow 的「SQL问题索引」集。
      </p>
      <el-table :data="registry.auto || []" size="small" max-height="320">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="auto-detail">
              <div>
                <strong>参数化 SQL</strong>
                <pre class="auto-sql">{{ row.sql }}</pre>
              </div>
              <div>
                <strong>匹配自画像</strong>
                <p class="hint">
                  形态 {{ (row.match?.shapes || []).join('、') || '-' }} ·
                  主体 {{ (row.match?.subjects || []).join('、') || '-' }} ·
                  分组 {{ (row.match?.group_by || []).join('、') || '-' }} ·
                  指标 {{ (row.match?.metrics || []).join('、') || '-' }} ·
                  条件 {{ (row.match?.conditions || []).join('、') || '-' }}
                </p>
              </div>
              <div v-if="(row.variants || []).length">
                <strong>问法变体</strong>
                <p class="hint">{{ (row.variants || []).join(' ｜ ') }}</p>
              </div>
              <div v-if="(row.samples || []).length">
                <strong>样本（原始问法 + 当时槽值）</strong>
                <p v-for="(sample, index) in row.samples" :key="index" class="hint">
                  {{ sample.question }} → {{ JSON.stringify(sample.values) }}
                </p>
              </div>
              <!-- 审核建议：只展示不自动降级（LLM 误报率高），误报可一键忽略 -->
              <div v-if="row.review && (row.review.issues || []).length">
                <strong>审核建议（不自动降级，由你判断）</strong>
                <p v-for="(issue, index) in row.review.issues" :key="index" class="hint">
                  [{{ issue.severity || '-' }}] {{ issue.type || '-' }}：{{ issue.description || '' }}
                  <span v-if="issue.location">（{{ issue.location }}）</span>
                </p>
                <el-button
                  v-if="!row.review.ignored"
                  size="small"
                  text
                  @click="ignoreReview(row)"
                >
                  忽略这条建议（判为误报）
                </el-button>
                <span v-else class="hint">已忽略（{{ row.review.ignored_at || '' }}）</span>
              </div>
              <p class="hint">指纹 {{ row.fingerprint }} · 生成于 {{ row.created_at }}</p>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="模板" width="220" show-overflow-tooltip />
        <el-table-column prop="question" label="来源问法" show-overflow-tooltip />
        <el-table-column label="槽位" width="260">
          <template #default="{ row }">
            <span v-for="param in row.params || []" :key="param.name" class="chip">
              {{ param.label }}·{{ param.confirmed_by }}
            </span>
            <span v-if="!(row.params || []).length" class="hint">（无可换参数）</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="row.status === 'mature' ? 'success' : 'info'" size="small">
              {{ statusLabel(row.status) }}
            </el-tag>
            <el-tooltip v-if="row.risk" :content="row.risk" placement="top">
              <el-tag type="warning" size="small" style="margin-left: 4px">口径存疑</el-tag>
            </el-tooltip>
            <el-tooltip
              v-if="row.review && row.review.high && !row.review.ignored"
              :content="reviewText(row)"
              placement="top"
            >
              <el-tag
                :type="row.review.demote ? 'danger' : 'warning'"
                size="small"
                style="margin-left: 4px"
              >
                {{ row.review.demote ? '审核建议（已降级）' : `审核建议 ${(row.review.issues || []).length} 条` }}
              </el-tag>
            </el-tooltip>
            <el-tag v-if="row.review && row.review.ignored" type="info" size="small" style="margin-left: 4px">
              建议已忽略
            </el-tag>
            <el-tag v-if="row.partial" type="warning" size="small" style="margin-left: 4px">
              只答一部分
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="usage" label="命中次数" width="90" />
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button
              v-if="row.status !== 'mature'"
              size="small"
              text
              type="primary"
              @click="promoteAuto(row)"
            >
              转正
            </el-button>
            <el-button size="small" text @click="toggleAuto(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </el-button>
            <el-button size="small" text @click="remineAuto(row)">重挖槽位</el-button>
            <el-button size="small" text type="danger" @click="deleteAuto(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="(registry.rejects || []).length" style="margin-top: 14px">
        <strong>挖掘审计：这些成功问答没沉淀成模板</strong>
        <p class="hint">原因能看出来是哪一类（缺条件通道/不可泛化/结构不支持），方便人工补救。</p>
        <el-table :data="registry.rejects || []" size="small" max-height="220">
          <el-table-column prop="question" label="问题" show-overflow-tooltip />
          <el-table-column prop="reason" label="没沉淀的原因" show-overflow-tooltip />
        </el-table>
      </div>
    </section>

    <section class="panel-card">
      <div class="card-heading">
        <h2>示例库同步</h2>
        <span>固定看板的卡片来自这里：RAGFlow 问答集 + 你固化进来的题</span>
        <el-button size="small" type="primary" style="margin-left: auto" :loading="syncing" @click="sync">
          立即同步
        </el-button>
      </div>
      <p class="hint">
        本地示例条目 <strong>{{ status.local ?? '-' }}</strong> ·
        带 SQL <strong>{{ status.with_sql ?? '-' }}</strong> ·
        RAGFlow 文档 <strong>{{ status.ragflow ?? '-' }}</strong>
      </p>
      <p class="hint">同步是幂等的：只补 RAGFlow 里有、本地没有的问答对，不会重复、不会删本地。</p>
      <div style="margin-top: 8px">
        <el-button size="small" :loading="backfilling" @click="backfillPhysical">
          给 Wren 示例补物理 SQL
        </el-button>
        <el-button size="small" :loading="repairing" @click="repairSpecOrder">
          修复示例参数顺序
        </el-button>
        <span class="hint" style="margin-left: 8px">
          Wren 生成的 MDL-SQL 只能过 Wren 才能跑；补一份物理 SQL 后，重放和挖模板都不再依赖 Wren。
        </span>
      </div>
    </section>

    <section class="panel-card">
      <div class="card-heading">
        <h2>预置查询</h2>
        <span>{{ queries.length }} 条</span>
        <el-button size="small" type="primary" style="margin-left: auto" @click="openEditor(null)">
          新增
        </el-button>
      </div>
      <el-table :data="queries" size="small" max-height="420">
        <el-table-column prop="category" label="分类" width="110" />
        <el-table-column prop="title" label="标题" width="220" />
        <el-table-column prop="question" label="问法" show-overflow-tooltip />
        <el-table-column label="参数" width="150">
          <template #default="{ row }">
            {{ (row.params || []).map((name) => paramLabel(name)).join('、') || '无（进固定看板）' }}
          </template>
        </el-table-column>
        <el-table-column prop="scope" label="口径说明" show-overflow-tooltip />
        <el-table-column label="操作" width="130">
          <template #default="{ row }">
            <el-button text size="small" @click="openEditor(row)">编辑</el-button>
            <el-button text size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-dialog v-model="editorVisible" title="预置查询" width="min(920px, 94vw)" top="6vh">
      <el-form label-width="90px" size="small">
        <el-form-item label="id"><el-input v-model="draft.id" placeholder="英文短 id，唯一" /></el-form-item>
        <el-form-item label="分类"><el-input v-model="draft.category" /></el-form-item>
        <el-form-item label="标题"><el-input v-model="draft.title" /></el-form-item>
        <el-form-item label="问法"><el-input v-model="draft.question" /></el-form-item>
        <el-form-item label="参数">
          <el-select v-model="draft.params" multiple style="width: 100%">
            <el-option v-for="name in Object.keys(params)" :key="name" :label="paramLabel(name)" :value="name" />
          </el-select>
        </el-form-item>
        <el-form-item label="SQL 模板">
          <el-input
            v-model="draft.sql"
            type="textarea"
            :rows="6"
            placeholder="参数写成 {route_id} 这种占位；只允许 SELECT/WITH"
          />
        </el-form-item>
        <el-form-item label="口径说明"><el-input v-model="draft.scope" /></el-form-item>
      </el-form>

      <!-- 仿写样例：一屏内能同时看到"字段 + 一条完整样例 SQL"，不会写的人照着改 -->
      <el-collapse>
        <el-collapse-item :title="`仿写样例（${sameCategory.length} 条同分类 · 参数取值来源）`" name="ref">
          <p class="hint">
            参数占位直接写在 SQL 里：<code>{route_id}</code>、<code>{month}</code>、
            <code>{damage_type}</code>、<code>{up_or_down}</code>、<code>{bucket_m}</code>、
            <code>{top_n}</code>。取值来源：路线/月份/病害类型**连库现查**，上下行/桩号分段/取前几名
            是固定选项。
          </p>
          <div v-for="item in sameCategory" :key="item.id" class="sample">
            <div class="sample-title">{{ item.title }} <span>{{ item.question }}</span></div>
            <pre class="sample-sql">{{ item.sql }}</pre>
          </div>
          <div v-if="!sameCategory.length" class="hint">当前分类没有其它样例，先看别的分类。</div>
        </el-collapse-item>
      </el-collapse>

      <template #footer>
        <el-button @click="editorVisible = false">取消</el-button>
        <el-button :loading="drafting" @click="aiDraft">让 AI 写草稿</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const queries = ref([])
const params = ref({})
const editorVisible = ref(false)
const status = ref({})
const registry = ref({})
const syncing = ref(false)
const rebuilding = ref(false)
const recalibrating = ref(false)
const refreshing = ref(false)
const indexing = ref(false)
const backfilling = ref(false)
const repairing = ref(false)
const reviewing = ref(false)
const sweeping = ref(false)

// 模板状态 → 中文（mature 可推荐 / candidate 待观察 / cold 长期没人用 / broken 表结构变了）
function statusLabel(status) {
  return {
    mature: '可推荐',
    candidate: '待观察',
    cold: '冷门（不推荐）',
    broken: '已失效（表结构变了）',
  }[status] || status || '-'
}

// 审核未过时，把 high 级问题拼出来给 tooltip
function reviewText(row) {
  const issues = (row.review && row.review.issues) || []
  return (
    issues
      .filter((item) => String(item.severity || '').toLowerCase() === 'high')
      .map((item) => item.description || item.type || '')
      .join('；') || 'LLM 审核发现高风险问题'
  )
}
const drafting = ref(false)
const draft = ref({ id: '', category: '病害统计', title: '', question: '', params: [], sql: '', scope: '' })

const sameCategory = computed(() =>
  queries.value.filter((item) => item.category === draft.value.category && item.id !== draft.value.id),
)

const paramLabel = (name) => params.value[name]?.label || name

async function load() {
  try {
    // 管理页要带 SQL：编辑框回填、并且当仿写样例
    const data = await api.presetQueries(true)
    queries.value = data.queries || []
    params.value = data.params || {}
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function loadStatus() {
  try {
    status.value = await api.dashboardSyncStatus()
  } catch {
    status.value = {}
  }
}

async function loadRegistry() {
  try {
    registry.value = await api.templateRegistry()
  } catch {
    registry.value = {}
  }
}

// LLM 审核（方案 §5.1 + 模板挖槽位与LLM审核逻辑重构方案.md 方案 A）：
// 只审不修、**只出建议**（默认不自动降级——实测误报率高，降级会丢好模板）
async function reviewTemplates() {
  reviewing.value = true
  try {
    const out = await api.reviewAutoTemplates(3)
    await loadRegistry()
    const items = out.items || []
    const demoted = items.filter((item) => item.demoted).length
    const restored = items.filter((item) => item.restored).length
    // 审核发现"这处该参数化却没参数化"时，系统会当场把意见喂给语义挖槽去修（见 templatizer.review_template）
    const remined = items.filter((item) => (item.remined || {}).ok).length
    ElMessage.success(
      `待审 ${out.pending ?? 0} 条，本次审了 ${out.reviewed ?? 0} 条` +
        (remined ? `，按审核意见重挖修好 ${remined} 条` : '') +
        (demoted ? `，降级 ${demoted} 条` : '') +
        (restored ? `，恢复 ${restored} 条` : ''),
    )
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    reviewing.value = false
  }
}

// 审核建议是 LLM 生成的，误报是常态 → 用户判为误报时一键忽略（结论留着备查）
async function ignoreReview(row) {
  try {
    await api.ignoreAutoTemplateReview(row.id)
    await loadRegistry()
    ElMessage.success('已忽略这条审核建议')
  } catch (err) {
    ElMessage.error(err.message)
  }
}

// 单条重挖槽位：存量模板是旧版挖掘器挖的，槽位不会自动跟着变
// （实测"看交安事件的病害样图"当初把大类当成固定值、显示"无可换参数"）
async function remineAuto(row) {
  try {
    const out = await api.remineAutoTemplate(row.id, true)
    await loadRegistry()
    if (out.ok) {
      ElMessage.success(`槽位已重挖：${(out.before || []).length} → ${(out.after || []).length} 个`)
    } else {
      ElMessage.warning(out.reason || '这条没有可重挖的槽位')
    }
  } catch (err) {
    ElMessage.error(err.message)
  }
}

// 生命周期巡检（方案 §5.3）：broken（表结构变了）/ cold（摆了十次没人用）
async function sweepTemplates() {
  sweeping.value = true
  try {
    const out = await api.sweepAutoTemplates()
    await loadRegistry()
    ElMessage.success(
      `检查 ${out.checked ?? 0} 条：失效 ${(out.broken || []).length} 条、冷门 ${(out.cold || []).length} 条` +
        (out.catalog_ready === false ? '（字段目录读不到，本次没判失效）' : ''),
    )
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    sweeping.value = false
  }
}


async function rebuildAuto() {
  rebuilding.value = true
  try {
    const stats = await api.rebuildAutoTemplates()
    await loadRegistry()
    ElMessage.success(
      `全量重挖完成：处理 ${stats.pending ?? stats.pairs} 条，入库 ${stats.templates} 条（可推荐 ${stats.mature}）`,
    )
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    rebuilding.value = false
  }
}

// 模板刷新：只挖"台账里没有"的新问答对，已挖过的跳过（可以放心频繁点）
async function refreshAuto() {
  refreshing.value = true
  try {
    const stats = await api.refreshAutoTemplates()
    await loadRegistry()
    if (!stats.pending) {
      ElMessage.success(`没有新问答要处理（示例库 ${stats.pairs} 条都挖过了）`)
    } else {
      ElMessage.success(
        `模板刷新：新问答 ${stats.pending} 条 → 新增模板 ${stats.fresh} 条` +
          `（跳过已挖 ${stats.skipped} 条，模板库共 ${stats.templates} 条，可推荐 ${stats.mature}）`,
      )
    }
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    refreshing.value = false
  }
}

// 模板 spec 重校准：按当前 SQL 自画像重算形状/主体/分组/指标。先跑一次只看差异，
// 有差异再让用户确认写回（改动的是模板的"能接哪些问法"，不是 SQL 本身）
async function recalibrateAuto() {
  recalibrating.value = true
  try {
    const preview = await api.recalibrateAutoTemplates(false)
    if (!preview.updated) {
      ElMessage.success(`模板 spec 都是最新的（${preview.total} 条，无差异）`)
      return
    }
    const lines = preview.changes
      .slice(0, 5)
      .map((item) => `${item.id}：${JSON.stringify(item.before?.shapes)} → ${JSON.stringify(item.after?.shapes)}`)
      .join('\n')
    // 有差异才写回：这些模板此前按老画像匹配，可能一直没被推荐过
    const applied = await api.recalibrateAutoTemplates(true)
    await loadRegistry()
    ElMessage.success(`已重校准 ${applied.updated} 条（共 ${applied.total} 条）｜${lines}`)
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    recalibrating.value = false
  }
}

// 「SQL问题索引」集对账：补齐缺失的问题、删掉问答示例集里已经没有的
async function syncQuestionIndex() {
  indexing.value = true
  try {
    const out = await api.syncQuestionIndex()
    ElMessage.success(
      `问题索引集已对齐：补 ${out.added ?? 0} 条、清 ${out.removed ?? 0} 条（当前 ${out.indexed ?? 0}/${out.total ?? 0}）`,
    )
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    indexing.value = false
  }
}

// 示例复用是按序号填 {route_id}/{route_id_2} 的：spec 里路线顺序和 SQL 占位符顺序不一致
// 就会把参数填反（实测"标签写西乡、数据其实是恒明珠"）。修不好的一律标成未确认，退回 Wren。
async function repairSpecOrder() {
  repairing.value = true
  try {
    const out = await api.repairExampleSpecOrder()
    ElMessage.success(
      `检查 ${out.scanned ?? 0} 条：修好 ${out.fixed ?? 0} 条` +
        (out.blocked ? `，另有 ${out.blocked} 条修不好已标未确认（不再用于复用）` : ''),
    )
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    repairing.value = false
  }
}


async function backfillPhysical() {
  backfilling.value = true
  try {
    const out = await api.backfillExamplePhysicalSql()
    const failed = (out.failed || []).length
    ElMessage.success(
      `候选 ${out.candidates ?? 0} 条：补上 ${(out.compiled || []).length} 条` +
        (failed ? `，编译不出来 ${failed} 条（原样保留）` : ''),
    )
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    backfilling.value = false
  }
}

async function toggleAuto(row) {
  try {
    await api.setAutoTemplate(row.id, !row.enabled)
    await loadRegistry()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function promoteAuto(row) {
  try {
    await api.promoteAutoTemplate(row.id)
    await loadRegistry()
    ElMessage.success('已转正，下次对话就能推荐这条模板')
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function deleteAuto(row) {
  // 源头示例现在给的是 [{doc_name, question}]；兼容早期的纯字符串
  const sources = (row.source_docs || []).map((item) =>
    typeof item === 'string' ? { doc_name: item, question: '' } : item,
  )
  const detail = sources.length
    ? `这条模板是从 ${sources.length} 条示例问答挖出来的，会一并删除：\n` +
      sources
        .map((item) => `· ${item.question || item.doc_name}（${item.doc_name}）`)
        .join('\n') +
      '\n\n（RAGFlow「SQL问答示例」+「SQL问题索引」+ 本地示例表都会清掉）'
    : '这条模板没记到源头示例（可能是手工建的），只删模板本身。'
  try {
    await ElMessageBox.confirm(`删除模板「${row.title}」？\n\n${detail}`, '删除模板 + 源头示例', {
      type: 'warning',
      confirmButtonText: '一起删除',
      cancelButtonText: '取消',
    })
    const out = await api.deleteAutoTemplate(row.id, true)
    await loadRegistry()
    const purge = out?.purge || {}
    ElMessage.success(
      sources.length
        ? `已删除模板，并清掉源头：RAGFlow 文档 ${purge.deleted_docs ?? 0} + 问题索引 ${purge.deleted_index ?? 0}，本地示例 ${purge.removed_records ?? 0} 条`
        : '已删除模板',
    )
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err.message)
  }
}

async function sync() {
  syncing.value = true
  try {
    const out = await api.syncDashboardExamples()
    status.value = out.status || {}
    ElMessage.success(`同步完成：新导入 ${out.imported?.imported ?? 0} 条`)
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    syncing.value = false
  }
}

async function aiDraft() {
  if (!draft.value.title && !draft.value.question) {
    ElMessage.error('先写个标题或问法，AI 才有东西可写')
    return
  }
  drafting.value = true
  try {
    const out = await api.draftPreset({
      title: draft.value.title,
      question: draft.value.question,
      category: draft.value.category,
      params: draft.value.params,
    })
    draft.value = { ...draft.value, ...out.draft }
    ElMessage.success('草稿已填入表单，请人工核对后再保存（保存才生效）')
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    drafting.value = false
  }
}

function openEditor(row) {
  draft.value = row
    ? {
        id: row.id,
        category: row.category,
        title: row.title,
        question: row.question,
        params: [...(row.params || [])],
        sql: row.sql || '',
        scope: row.scope || '',
      }
    : { id: '', category: '病害统计', title: '', question: '', params: [], sql: '', scope: '' }
  editorVisible.value = true
}

async function save() {
  if (!draft.value.id || !draft.value.sql) {
    ElMessage.error('id 和 SQL 模板必填')
    return
  }
  try {
    await api.savePreset(draft.value)
    editorVisible.value = false
    ElMessage.success('已保存，下次运行即时生效')
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function remove(row) {
  try {
    await api.deletePreset(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

onMounted(async () => {
  await load()
  await loadStatus()
  await loadRegistry()
})
</script>

<style scoped>
.panel-card {
  padding: 18px 20px;
}

.card-heading {
  display: flex;
  align-items: baseline;
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

.hint {
  margin: 0 0 10px;
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.7;
}

.auto-detail {
  display: grid;
  gap: 8px;
  padding: 4px 8px 8px;
}

.auto-sql {
  margin: 4px 0 0;
  padding: 8px 10px;
  background: #fff;
  border: 1px solid var(--line, #e5e8ef);
  border-radius: 8px;
  color: #1f2733;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}

.sample {
  margin-bottom: 12px;
}

.sample-title {
  font-size: 13px;
  font-weight: 600;
}

.sample-title span {
  color: var(--ink-soft);
  font-weight: 400;
}

.sample-sql {
  margin: 4px 0 0;
  padding: 10px;
  overflow: auto;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  border-radius: 8px;
  background: #f6f8fb;
}
</style>
