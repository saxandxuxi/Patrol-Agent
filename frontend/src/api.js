async function request(path, options = {}) {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!resp.ok) {
    const text = await resp.text()
    // 后端错误是 {"detail": "..."} 的 JSON，直接抛原文会让用户看到一串花括号
    let message = text || resp.statusText
    try {
      const parsed = JSON.parse(text)
      if (parsed && typeof parsed.detail === 'string') message = parsed.detail
    } catch {
      // 不是 JSON 就按原文提示
    }
    throw new Error(message)
  }
  return resp.json()
}

export const api = {
  chat: (payload) => request('/api/chat', { method: 'POST', body: JSON.stringify(payload) }),
  chatStream: async (payload, onEvent) => {
    const resp = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!resp.ok) {
      const text = await resp.text()
      throw new Error(text || resp.statusText)
    }
    if (!resp.body) throw new Error('浏览器不支持流式响应')
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let finalPayload = null
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const data = block
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart())
          .join('\n')
        if (data) {
          const event = JSON.parse(data)
          if (event.type === 'error') throw new Error(event.message || '流式查询失败')
          onEvent?.(event)
          if (event.type === 'final') finalPayload = event.payload
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
    if (!finalPayload) throw new Error('流式连接结束，但没有收到最终结果')
    if (finalPayload.result_id) {
      try {
        const detail = await request(`/api/results/${encodeURIComponent(finalPayload.result_id)}`)
        finalPayload = { ...detail, ...finalPayload, reply: finalPayload.reply || detail.reply }
      } catch {
        // 结果缓存过期时仍保留 final 事件里的最终答复
      }
    }
    return finalPayload
  },
  approveResult: (resultId) =>
    request('/api/feedback/approve', { method: 'POST', body: JSON.stringify({ result_id: resultId }) }),
  history: () => request('/api/history'),
  clearSession: (sessionId) =>
    request(`/api/session/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }),
  conversations: (limit = 50) => request(`/api/conversations?limit=${limit}`),
  conversation: (id) => request(`/api/conversations/${encodeURIComponent(id)}`),
  result: (resultId) => request(`/api/results/${encodeURIComponent(resultId)}`),
  deleteConversation: (id) =>
    request(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  purgeConversations: (payload) =>
    request('/api/conversations/purge', { method: 'POST', body: JSON.stringify(payload) }),
  chatSettings: () => request('/api/settings/chat'),
  // 预置查询（验证过的高频题，一键出数、不走 LLM）
  presetQueries: (withSql = false) =>
    request(`/api/preset-queries${withSql ? '?with_sql=1' : ''}`),
  savePreset: (query) =>
    request('/api/preset-queries', { method: 'POST', body: JSON.stringify({ query }) }),
  draftPreset: (payload) =>
    request('/api/preset-queries/draft', { method: 'POST', body: JSON.stringify(payload) }),
  deletePreset: (id) =>
    request(`/api/preset-queries/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  runPreset: (id, params = {}) =>
    request('/api/preset-query', { method: 'POST', body: JSON.stringify({ id, params }) }),
  // 通用查询：任意表 + 任意字段 + 任意条件（字段目录来自库注释）
  genericCatalog: () => request('/api/generic/catalog'),
  runGenericQuery: (payload) =>
    request('/api/generic/query', { method: 'POST', body: JSON.stringify(payload) }),
  templateRegistry: () => request('/api/templates/registry'),
  // 「在测试库上试」：把当前 SQL 丢给夹具库（后端只跑本地 SQLite，绝不碰业务库）
  fixtureProbe: (sql, kind = 'mysql') =>
    request('/api/fixtures/probe', { method: 'POST', body: JSON.stringify({ sql, kind }) }),
  // 图片墙批量取图：一次请求拿回一批 data URL（逐张请求是"一张一次 SSH 往返"，几十张要十几秒）
  imagesBatch: (paths) =>
    request('/api/images/batch', { method: 'POST', body: JSON.stringify({ paths }) }),
  // L1 自动沉淀模板：重挖 / 停用 / 删除（docs/RAG示例直达与AI直答旁路方案.md §2.9）
  // 快捷查询里点查自动模板：清单（默认只回可点查的 mature+启用，SQL 需 with_sql=1）
  autoTemplates: (withSql = false, runnableOnly = true) =>
    request(
      `/api/templates/auto?with_sql=${withSql ? 1 : 0}&runnable_only=${runnableOnly ? 1 : 0}`,
    ),
  runAutoTemplate: (id, params = {}) =>
    request('/api/templates/auto/run', { method: 'POST', body: JSON.stringify({ id, params }) }),
  // 模板刷新（增量）：只尝试新加进示例库/RAGFlow 的问答对，已挖过的跳过
  refreshAutoTemplates: () => request('/api/templates/auto/refresh', { method: 'POST' }),
  // 模板 spec 重校准：按当前 SQL 自画像重算形状/主体/分组/指标（apply=0 只回报差异）
  recalibrateAutoTemplates: (apply = false) =>
    request(`/api/templates/auto/recalibrate?apply=${apply ? 1 : 0}`, { method: 'POST' }),
  // 「SQL问题索引」集对账（两个 RAGFlow 数据集同进同出）
  syncQuestionIndex: (force = false) =>
    request(`/api/examples/question-index/sync?force=${force ? 1 : 0}`, { method: 'POST' }),
  rebuildAutoTemplates: () => request('/api/templates/auto/rebuild', { method: 'POST' }),
  // 给示例库里的 Wren MDL-SQL 补物理 SQL（重放/挖模板不再依赖 Wren）
  backfillExamplePhysicalSql: () => request('/api/examples/backfill', { method: 'POST' }),
  // 修"示例 spec 里的路线顺序和归一化 SQL 占位符顺序不一致"（会把 {route_id}/{route_id_2} 填反）
  repairExampleSpecOrder: () => request('/api/examples/repair-spec-order', { method: 'POST' }),
  // 模板 LLM 审核（方案 §5.1 + 模板挖槽位与LLM审核逻辑重构方案.md 方案 A）：
  // 只审不修、只出建议（默认不自动降级）；每次限量省花费
  reviewAutoTemplates: (limit = 3) =>
    request(`/api/templates/auto/review?limit=${limit}`, { method: 'POST' }),
  // 把这条模板的审核建议标成"已忽略"（用户判定误报）
  ignoreAutoTemplateReview: (id) =>
    request(`/api/templates/auto/${encodeURIComponent(id)}/review/ignore`, { method: 'POST' }),
  // 单条重挖槽位：存量模板是旧版挖掘器挖的，槽位不会自动跟着变（apply=0 只回报差异）
  remineAutoTemplate: (id, apply = true) =>
    request(`/api/templates/auto/${encodeURIComponent(id)}/remine?apply=${apply ? 1 : 0}`, {
      method: 'POST',
    }),
  // 生命周期巡检（方案 §5.3）：broken（表结构变了）/ cold（摆了多次没人用）
  sweepAutoTemplates: () => request('/api/templates/auto/sweep', { method: 'POST' }),
  setAutoTemplate: (id, enabled) =>
    request('/api/templates/auto', { method: 'POST', body: JSON.stringify({ id, enabled }) }),
  promoteAutoTemplate: (id) =>
    request(`/api/templates/auto/${encodeURIComponent(id)}/promote`, { method: 'POST' }),
  // purgeSource=true：连源头示例一起删（RAGFlow 问答集 + 问题索引 + 本地示例表 + 历史），
  // 否则只删模板、下次重挖又从源头长回来
  deleteAutoTemplate: (id, purgeSource = true) =>
    request(
      `/api/templates/auto/${encodeURIComponent(id)}?purge_source=${purgeSource ? 1 : 0}`,
      { method: 'DELETE' },
    ),
  // 固定看板：卡片来自"答对了"固化的示例题 + 系统无参模板（重放不生成 SQL）
  dashboardCards: () => request('/api/dashboard/cards'),
  // 真删一张看板卡片（RAGFlow 两个集 + 本地示例表 + 历史）；"移出"是 setDashboardCard(on_board:false)
  deleteDashboardCard: (docName) =>
    request(`/api/dashboard/cards/${encodeURIComponent(docName)}`, { method: 'DELETE' }),
  runDashboardCard: (docName) =>
    request('/api/dashboard/run', { method: 'POST', body: JSON.stringify({ doc_name: docName }) }),
  setDashboardCard: (payload) =>
    request('/api/dashboard/card-state', { method: 'POST', body: JSON.stringify(payload) }),
  dashboardSyncStatus: () => request('/api/dashboard/sync-status'),
  syncDashboardExamples: () =>
    request('/api/dashboard/sync-examples', { method: 'POST', body: JSON.stringify({}) }),
  // 查询收藏夹（用户自己维护的高频问法）
  favorites: () => request('/api/favorites'),
  addFavorite: (question) =>
    request('/api/favorites', { method: 'POST', body: JSON.stringify({ question }) }),
  removeFavorite: (question) =>
    request('/api/favorites', { method: 'DELETE', body: JSON.stringify({ question }) }),
  // 答对/答错反馈（攒 bad case）
  reportFeedback: (payload) =>
    request('/api/feedback/report', { method: 'POST', body: JSON.stringify(payload) }),
  updateChatSettings: (payload) =>
    request('/api/settings/chat', { method: 'PUT', body: JSON.stringify(payload) }),
  health: () => request('/api/health'),
  knowledgeStatus: () => request('/api/knowledge/status'),
  syncKnowledge: () => request('/api/knowledge/sync', { method: 'POST' }),
  rechunkKnowledge: () => request('/api/knowledge/rechunk', { method: 'POST' }),
  retrievals: (limit = 20) => request(`/api/knowledge/retrievals?limit=${limit}`),
  schema: (kind = 'mysql', includeRows = false) =>
    request(`/api/schema?kind=${encodeURIComponent(kind)}&include_rows=${includeRows ? 'true' : 'false'}`),
  // kind/generator 都留空就用后端 .env 的默认；generator 可选 legacy / official（A/B 对比）
  syncMdl: (kind = '', generator = '') => {
    const params = new URLSearchParams()
    if (kind) params.set('kind', kind)
    if (generator) params.set('generator', generator)
    const query = params.toString()
    return request(`/api/schema/mdl/sync${query ? `?${query}` : ''}`, { method: 'POST' })
  },
  mdlGenerators: () => request('/api/schema/mdl/generators'),
  storage: (path = 'buckets') => request(`/api/storage?path=${encodeURIComponent(path)}`),
  storageReferences: () => request('/api/storage/references'),
  examplePairs: (limit = 50) => request(`/api/knowledge/example-pairs?limit=${limit}`),
  purgeExamplePairs: (payload) =>
    request('/api/knowledge/example-pairs/purge', { method: 'POST', body: JSON.stringify(payload) }),
  models: () => request('/api/models'),
  saveModel: (payload) => request('/api/models', { method: 'POST', body: JSON.stringify(payload) }),
  testModel: (payload) => request('/api/models/test', { method: 'POST', body: JSON.stringify(payload) }),
  deleteModel: (id) => request(`/api/models/${id}`, { method: 'DELETE' }),
}
