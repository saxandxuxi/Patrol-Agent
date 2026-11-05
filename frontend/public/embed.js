(function () {
  const STYLE = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .root {
      position: fixed; right: 18px; bottom: 18px; z-index: 2147483646;
      width: 58px; height: 58px; font-family: Inter, "Microsoft YaHei", sans-serif;
    }
    .root.is-docking { transition: left .18s ease, top .18s ease; }
    .orb {
      position: relative; display: grid; width: 58px; height: 58px; padding: 0;
      color: #f8fbff; border: 1px solid rgba(208, 228, 255, .55); border-radius: 50%;
      cursor: grab; touch-action: none; user-select: none;
      background:
        radial-gradient(circle at 32% 20%, rgba(104, 163, 255, .9), transparent 36%),
        linear-gradient(145deg, #2169ee 0%, #154ec8 52%, #0b2e7d 100%);
      box-shadow:
        0 12px 30px rgba(13, 52, 141, .38),
        inset 0 1px 0 rgba(255, 255, 255, .28),
        0 0 0 5px rgba(255,255,255,.9);
      place-items: center;
      transition: width .18s ease, height .18s ease, border-radius .18s ease, box-shadow .18s ease;
    }
    .root.is-dragging .orb { cursor: grabbing; }
    .orb::before {
      position: absolute; inset: 5px; content: ""; border: 1px solid rgba(255,255,255,.2);
      border-radius: 50%;
    }
    .orb::after {
      position: absolute; inset: -5px; content: ""; border: 1px solid rgba(70,125,255,.32);
      border-radius: 50%; animation: pulse 2.4s infinite;
    }
    .orb-mark {
      position: relative; z-index: 1; display: flex; align-items: baseline; gap: 1px;
      font-family: Bahnschrift, "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
      line-height: 1; transform: translateY(1px);
    }
    .orb-mark-cn { font-size: 13px; font-weight: 650; letter-spacing: .02em; }
    .orb-mark-e {
      font-size: 22px; font-weight: 700; font-stretch: condensed; font-style: italic;
      letter-spacing: -.08em; text-shadow: 0 1px 5px rgba(0, 18, 72, .35);
    }
    .orb:focus-visible { outline: 3px solid rgba(255, 205, 82, .95); outline-offset: 6px; }
    .orb-dot {
      position: absolute; right: 3px; bottom: 4px; width: 10px; height: 10px;
      border: 2px solid #fff; border-radius: 50%; background: #f3a712;
    }
    .root.docked .orb {
      width: 34px; height: 54px; box-shadow: 0 8px 24px rgba(8, 36, 105, .28);
    }
    .root.dock-left .orb { border-radius: 0 16px 16px 0; }
    .root.dock-right .orb { border-radius: 16px 0 0 16px; }
    .root.docked .orb::before { inset: 4px; border-radius: inherit; }
    .root.docked .orb::after { display: none; }
    .root.docked .orb-mark-cn { display: none; }
    .root.docked .orb-mark-e {
      font-size: 18px; font-stretch: normal; font-style: normal; letter-spacing: -.04em;
    }
    .root.docked .orb-dot { width: 8px; height: 8px; }
    .menu {
      position: fixed; left: 0; top: 0; width: 248px; padding: 8px;
      color: #f7f9ff; border: 1px solid rgba(255,255,255,.14); border-radius: 16px;
      background: linear-gradient(160deg, #17233b, #0d1728);
      box-shadow: 0 18px 48px rgba(0,0,0,.34);
      transform-origin: right bottom; animation: menu-in .16s ease-out;
    }
    .menu[hidden], .panel[hidden], .help-text[hidden] { display: none; }
    .menu-head { display: flex; align-items: center; justify-content: space-between; padding: 6px 8px 8px; }
    .menu-title { font-size: 13px; font-weight: 700; }
    .help-btn {
      display: grid; width: 20px; height: 20px; padding: 0; color: #a9c3ff;
      border: 1px solid #5c78ad; border-radius: 50%; background: transparent;
      cursor: pointer; place-items: center;
    }
    .help-text { padding: 8px 10px; color: #b6c5df; font-size: 11px; line-height: 1.6; }
    .menu button.item {
      display: flex; width: 100%; padding: 10px 11px; align-items: center; gap: 10px;
      color: #f4f7ff; text-align: left; border: 0; border-radius: 10px;
      background: transparent; cursor: pointer;
    }
    .menu button.item:hover { background: rgba(255,255,255,.09); }
    .item-icon { display: grid; width: 24px; height: 24px; color: #bcd0ff; font-size: 14px; place-items: center; }
    .item-copy { display: flex; flex-direction: column; gap: 2px; }
    .item-copy strong { font-size: 13px; }
    .item-copy small { color: #899bb9; font-size: 10px; }
    .divider { height: 1px; margin: 6px 8px; background: rgba(255,255,255,.1); }
    .panel {
      position: fixed; left: 0; top: 0; width: 440px;
      height: min(760px, calc(100vh - 110px)); overflow: hidden;
      border: 1px solid rgba(22,44,88,.16); border-radius: 18px;
      background: #fff; box-shadow: 0 24px 70px rgba(7, 27, 64, .3);
      transition: width .2s ease;
    }
    .panel.wide { width: min(900px, calc(100vw - 86px)); }
    .panel iframe { display: block; width: 100%; height: 100%; border: 0; background: #fff; }
    @keyframes pulse { 0%,100% { transform: scale(.96); opacity: .3; } 50% { transform: scale(1.04); opacity: .7; } }
    @keyframes menu-in { from { transform: scale(.94); opacity: 0; } to { transform: scale(1); opacity: 1; } }
    @media (prefers-reduced-motion: reduce) {
      .orb::after { animation: none; }
      .menu, .panel { animation: none; transition: none; }
    }
    @media (max-width: 600px) {
      .panel, .panel.wide {
        position: fixed; inset: 8px !important; width: auto !important; height: auto !important;
        border-radius: 14px;
      }
    }
  `

  class InspectionAssistant {
    constructor(options) {
      this.options = {
        assistantUrl: '/',
        fullAssistantUrl: '/',
        ...options,
      }
      this.ready = false
      this.pending = []
      this.mount()
    }

    mount() {
      this.host = document.createElement('div')
      this.host.dataset.inspectionAssistant = 'true'
      document.body.appendChild(this.host)
      this.shadow = this.host.attachShadow({ mode: 'open' })
      this.shadow.innerHTML = `
        <style>${STYLE}</style>
        <div class="root">
          <div class="menu" hidden>
            <div class="menu-head">
              <span class="menu-title">巡检助手</span>
              <button class="help-btn" type="button" title="查看说明">?</button>
            </div>
            <div class="help-text" hidden>我只负责数据库查询，可问病害数量、分布、明细、趋势、巡检任务和规范，不会操作巡检平台。</div>
            <button class="item" type="button" data-action="ask"><span class="item-icon">⌕</span><span class="item-copy"><strong>问助手</strong><small>查数据库里的业务问题</small></span></button>
            <button class="item" type="button" data-action="recent"><span class="item-icon">◷</span><span class="item-copy"><strong>最近对话</strong><small>继续之前的查询</small></span></button>
            <div class="divider"></div>
            <button class="item" type="button" data-action="workbench"><span class="item-icon">▦</span><span class="item-copy"><strong>固定看板</strong><small>打开完整助手查看</small></span></button>
            <button class="item" type="button" data-action="presets"><span class="item-icon">⌘</span><span class="item-copy"><strong>快捷查询</strong><small>常用口径直接查</small></span></button>
            <button class="item" type="button" data-action="full"><span class="item-icon">↗</span><span class="item-copy"><strong>打开完整助手</strong><small>进入完整对话与工作台</small></span></button>
          </div>
          <div class="panel" hidden>
            <iframe title="巡检助手" loading="lazy"></iframe>
          </div>
          <button class="orb" type="button" aria-label="打开巡检助手">
            <span class="orb-mark"><span class="orb-mark-cn">小</span><span class="orb-mark-e">e</span></span><span class="orb-dot"></span>
          </button>
        </div>
      `
      this.menu = this.shadow.querySelector('.menu')
      this.panel = this.shadow.querySelector('.panel')
      this.iframe = this.shadow.querySelector('iframe')
      this.helpText = this.shadow.querySelector('.help-text')
      this.root = this.shadow.querySelector('.root')
      this.orb = this.shadow.querySelector('.orb')
      this.dragState = null
      this.ignoreClick = false
      this.dockedSide = ''
      this.positionKey = 'inspection_assistant_position_v1'
      this.restorePosition()
      this.orb.addEventListener('click', (event) => {
        if (this.ignoreClick) {
          event.preventDefault()
          return
        }
        this.toggleOrb()
      })
      this.orb.addEventListener('pointerdown', (event) => this.startDrag(event))
      window.addEventListener('pointermove', (event) => this.moveDrag(event))
      window.addEventListener('pointerup', (event) => this.endDrag(event))
      window.addEventListener('pointercancel', (event) => this.cancelDrag(event))
      this.shadow.querySelector('.help-btn').addEventListener('click', () => {
        this.helpText.hidden = !this.helpText.hidden
      })
      this.shadow.querySelectorAll('[data-action]').forEach((button) => {
        button.addEventListener('click', () => this.action(button.dataset.action))
      })
      window.addEventListener('message', (event) => this.onMessage(event))
      window.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          this.hidePanel()
        }
      })
      window.addEventListener('resize', () => this.onResize())
    }

    iframeUrl() {
      const url = new URL(this.options.assistantUrl, window.location.href)
      url.searchParams.set('mode', 'embed')
      url.searchParams.set('host_origin', window.location.origin)
      return url.toString()
    }

    ensureIframe() {
      if (!this.iframe.src) this.iframe.src = this.iframeUrl()
    }

    viewportSize() {
      return { width: window.innerWidth, height: window.innerHeight }
    }

    clamp(value, min, max) {
      return Math.min(Math.max(value, min), max)
    }

    freePosition(x, y) {
      const { width, height } = this.viewportSize()
      return {
        x: this.clamp(x, 8, Math.max(8, width - 66)),
        y: this.clamp(y, 8, Math.max(8, height - 66)),
      }
    }

    applyPosition(x, y) {
      this.x = x
      this.y = y
      this.root.style.right = 'auto'
      this.root.style.bottom = 'auto'
      this.root.style.left = `${x}px`
      this.root.style.top = `${y}px`
    }

    restorePosition() {
      let saved = null
      try {
        saved = JSON.parse(localStorage.getItem(this.positionKey) || 'null')
      } catch {
        saved = null
      }
      if (saved?.dockedSide === 'left' || saved?.dockedSide === 'right') {
        this.y = Number(saved.y) || window.innerHeight - 76
        this.setDocked(saved.dockedSide, { animate: false, persist: false })
        return
      }
      const position = this.freePosition(
        Number(saved?.x) || window.innerWidth - 76,
        Number(saved?.y) || window.innerHeight - 76,
      )
      this.applyPosition(position.x, position.y)
      this.setOrbLabel(false)
    }

    setOrbLabel(docked) {
      this.orb.setAttribute('aria-label', docked ? '展开巡检助手' : '打开巡检助手')
      this.orb.title = docked ? '展开巡检助手' : '拖动调整位置，点击打开巡检助手'
    }

    savePosition() {
      try {
        localStorage.setItem(
          this.positionKey,
          JSON.stringify({
            x: this.x,
            y: this.y,
            dockedSide: this.dockedSide || '',
          }),
        )
      } catch {
        // 宿主禁用 localStorage 时仍保留当前页面内的位置
      }
    }

    animatePosition(x, y) {
      this.root.classList.add('is-docking')
      this.applyPosition(x, y)
      window.setTimeout(() => this.root.classList.remove('is-docking'), 220)
    }

    setDocked(side, { animate = true, persist = true } = {}) {
      this.hidePanel()
      this.dockedSide = side
      this.root.classList.remove('dock-left', 'dock-right')
      this.root.classList.add('docked', `dock-${side}`)
      const x = side === 'left' ? 0 : window.innerWidth - 34
      const y = this.clamp(this.y, 8, Math.max(8, window.innerHeight - 62))
      if (animate) this.animatePosition(x, y)
      else this.applyPosition(x, y)
      this.setOrbLabel(true)
      if (persist) this.savePosition()
    }

    undock() {
      if (!this.dockedSide) return
      const previousSide = this.dockedSide
      this.dockedSide = ''
      this.root.classList.remove('docked', 'dock-left', 'dock-right')
      this.setOrbLabel(false)
      const position = this.freePosition(
        previousSide === 'left' ? 18 : window.innerWidth - 76,
        this.y,
      )
      this.animatePosition(position.x, position.y)
      this.savePosition()
    }

    startDrag(event) {
      if (event.button !== 0) return
      if (this.dockedSide) this.undock()
      this.dragState = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originX: this.x,
        originY: this.y,
        moved: false,
      }
      try {
        this.orb.setPointerCapture(event.pointerId)
      } catch {
        // 某些宿主 WebView 不支持 pointer capture
      }
    }

    moveDrag(event) {
      const state = this.dragState
      if (!state || state.pointerId !== event.pointerId) return
      const dx = event.clientX - state.startX
      const dy = event.clientY - state.startY
      if (!state.moved && Math.hypot(dx, dy) < 4) return
      if (!state.moved) {
        state.moved = true
        this.hidePanel()
        this.root.classList.add('is-dragging')
      }
      event.preventDefault()
      const position = this.freePosition(state.originX + dx, state.originY + dy)
      this.applyPosition(position.x, position.y)
    }

    endDrag(event) {
      const state = this.dragState
      if (!state || state.pointerId !== event.pointerId) return
      this.dragState = null
      this.root.classList.remove('is-dragging')
      try {
        this.orb.releasePointerCapture(event.pointerId)
      } catch {
        // pointer capture 已释放
      }
      if (!state.moved) return
      this.ignoreClick = true
      window.setTimeout(() => {
        this.ignoreClick = false
      }, 0)
      const { width } = this.viewportSize()
      if (this.x <= 72) {
        this.setDocked('left')
      } else if (this.x + 58 >= width - 72) {
        this.setDocked('right')
      } else {
        this.savePosition()
      }
    }

    cancelDrag(event) {
      if (this.dragState?.pointerId !== event.pointerId) return
      this.dragState = null
      this.root.classList.remove('is-dragging')
    }

    onResize() {
      if (this.dockedSide) {
        this.setDocked(this.dockedSide, { animate: false, persist: false })
      } else {
        const position = this.freePosition(this.x, this.y)
        this.applyPosition(position.x, position.y)
        this.savePosition()
      }
      if (!this.menu.hidden) this.positionMenu()
      if (!this.panel.hidden) this.positionPanel()
    }

    positionMenu() {
      if (this.menu.hidden) return
      const { width, height } = this.viewportSize()
      const orb = this.orb.getBoundingClientRect()
      const menu = this.menu.getBoundingClientRect()
      const gap = 12
      const margin = 12
      let left = orb.right + gap
      if (left + menu.width > width - margin) left = orb.left - menu.width - gap
      left = this.clamp(left, margin, Math.max(margin, width - menu.width - margin))
      let top = orb.top - menu.height - gap
      if (top < margin) top = orb.bottom + gap
      top = this.clamp(top, margin, Math.max(margin, height - menu.height - margin))
      this.menu.style.left = `${left}px`
      this.menu.style.top = `${top}px`
    }

    positionPanel() {
      if (this.panel.hidden) return
      const { width, height } = this.viewportSize()
      const orb = this.orb.getBoundingClientRect()
      const panel = this.panel.getBoundingClientRect()
      const gap = 14
      const margin = 12
      let left = orb.right + gap
      if (left + panel.width > width - margin) left = orb.left - panel.width - gap
      left = this.clamp(left, margin, Math.max(margin, width - panel.width - margin))
      let top = orb.bottom - panel.height
      if (top < margin) top = margin
      top = this.clamp(top, margin, Math.max(margin, height - panel.height - margin))
      this.panel.style.left = `${left}px`
      this.panel.style.top = `${top}px`
    }

    showMenu() {
      this.menu.hidden = false
      requestAnimationFrame(() => this.positionMenu())
    }

    toggleOrb() {
      if (this.dockedSide) {
        this.undock()
        this.showMenu()
        return
      }
      if (!this.panel.hidden) {
        this.hidePanel()
        return
      }
      if (this.menu.hidden) this.showMenu()
      else this.menu.hidden = true
    }

    hidePanel() {
      this.menu.hidden = true
      this.panel.hidden = true
      this.panel.classList.remove('wide')
      if (this.iframe.src) {
        this.iframe.contentWindow.postMessage(
          { type: 'assistant:reset-result' },
          new URL(this.iframeUrl()).origin,
        )
      }
    }

    openPanel(action) {
      if (this.dockedSide) this.undock()
      this.menu.hidden = true
      this.panel.hidden = false
      this.ensureIframe()
      requestAnimationFrame(() => this.positionPanel())
      if (action) this.sendWhenReady(action)
    }

    action(name) {
      if (name === 'ask') {
        this.openPanel({ type: 'assistant:focus' })
      } else if (name === 'recent') {
        this.openPanel({ type: 'assistant:open-history' })
      } else if (name === 'workbench' || name === 'presets') {
        const url = new URL(this.options.fullAssistantUrl, window.location.href)
        url.searchParams.set('view', name)
        window.open(url.toString(), '_blank', 'noopener')
      } else if (name === 'full') {
        window.open(new URL(this.options.fullAssistantUrl, window.location.href).toString(), '_blank', 'noopener')
      }
    }

    sendWhenReady(payload) {
      if (this.ready) {
        this.iframe.contentWindow.postMessage(payload, new URL(this.iframeUrl()).origin)
      } else {
        this.pending.push(payload)
      }
    }

    onMessage(event) {
      if (event.source !== this.iframe.contentWindow) return
      const data = event.data || {}
      if (data.type === 'assistant:ready') {
        this.ready = true
        for (const item of this.pending.splice(0)) {
          this.iframe.contentWindow.postMessage(item, event.origin)
        }
      } else if (data.type === 'assistant:result-open') {
        this.panel.classList.add('wide')
        requestAnimationFrame(() => this.positionPanel())
      } else if (data.type === 'assistant:result-close') {
        this.panel.classList.remove('wide')
        requestAnimationFrame(() => this.positionPanel())
      } else if (data.type === 'assistant:close') {
        this.hidePanel()
      }
    }
  }

  window.InspectionAssistant = {
    init(options) {
      if (window.__inspectionAssistant) return window.__inspectionAssistant
      window.__inspectionAssistant = new InspectionAssistant(options || {})
      return window.__inspectionAssistant
    },
  }
})()
