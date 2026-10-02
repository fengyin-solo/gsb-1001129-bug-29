<template>
  <section class="page" data-module="lighting">
    <header class="page-head">
      <div>
        <h2>路灯照明管理 · 回路棋盘</h2>
        <p class="page-desc">
          同一回路命中不同照度阈值时由「回路棋盘」统一裁决：调度员先核对测光证据，再合并故障包，
          最后下发车辆；裁决结论回写灯具台账、维修清单与路径汇总，冲突以照明主管复核为准。
        </p>
      </div>
    </header>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="message" class="notice" :class="{ 'notice-error': !messageOk }">{{ message }}</p>

    <!-- 灯具台账 -->
    <div v-if="activeTab === 'ledger'">
      <form class="filter-bar" @submit.prevent="reloadLedger">
        <label class="filter-item">
          <span>灯具编号</span>
          <input v-model="ledgerQuery.keyword" placeholder="按灯具编号检索" />
        </label>
        <label class="filter-item">
          <span>设施状态</span>
          <select v-model="ledgerQuery.status">
            <option value="">全部</option>
            <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in ledgerColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in ledgerRows" :key="String(row.id)">
            <td v-for="column in ledgerColumns" :key="column" :class="{ root: column === '根因' && row[column] }">
              {{ row[column] ?? '—' }}
            </td>
          </tr>
          <tr v-if="!ledgerRows.length">
            <td :colspan="ledgerColumns.length" class="empty-state">暂无路灯照明数据</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot"><span>共 {{ ledgerTotal }} 条灯具台账</span></footer>
    </div>

    <!-- 回路棋盘 -->
    <div v-if="activeTab === 'board'">
      <article v-for="board in boards" :key="board.回路编号" class="board-card">
        <header class="board-head">
          <div>
            <h3>{{ board.回路名称 }}（{{ board.回路编号 }}）</h3>
            <p class="page-desc">{{ board.所属路段 }} · {{ board.道路等级 }} · 当前版本 {{ board.当前版本 }}</p>
          </div>
          <span class="stage-pill">{{ board.阶段 }}</span>
        </header>

        <ol class="stepper">
          <li
            v-for="(stage, index) in boardStageOrder"
            :key="stage"
            class="step"
            :class="{ done: index + 1 < board.阶段次序, current: index + 1 === board.阶段次序 }"
          >
            <span class="step-dot">{{ index + 1 }}</span>
            <span class="step-label">{{ stage }}</span>
          </li>
        </ol>

        <div class="rule-line">
          <span>道路等级阈值判定：<b>{{ board.道路规则级别 ?? '待测光' }}</b></span>
          <span>现场安全结论判定：<b>{{ board.现场安全级别 ?? '待测光' }}</b></span>
          <span v-if="board.复核级别">主管复核：<b class="level-mark">{{ board.复核级别 }}</b></span>
          <span v-if="board.规则冲突" class="conflict-flag">规则冲突 · 以主管复核为准</span>
        </div>

        <!-- 阶段一：测光核验 -->
        <div v-if="board.阶段 === '测光核验'" class="action-panel">
          <h4>① 测光证据核验（调度员核对测光数据）</h4>
          <table class="data-table inner">
            <thead>
              <tr><th>杆号</th><th>灯具编号</th><th>台账现象</th><th>实测照度(lx)</th><th>现场安全结论</th></tr>
            </thead>
            <tbody>
              <tr v-for="lamp in darkLamps(board)" :key="lamp.杆号">
                <td>{{ lamp.杆号 }}</td>
                <td>{{ lamp.灯具编号 }}</td>
                <td>{{ lamp.现象 }}</td>
                <td><input v-model.number="meteringDraft[board.回路编号][lamp.杆号].lux" type="number" placeholder="照度" /></td>
                <td>
                  <select v-model="meteringDraft[board.回路编号][lamp.杆号].safety">
                    <option value="正常">正常</option>
                    <option value="高风险">高风险</option>
                  </select>
                </td>
              </tr>
            </tbody>
          </table>
          <div class="panel-actions">
            <button class="btn ghost" type="button" @click="fillSample(board)">填入样例读数</button>
            <button class="btn primary" type="button" @click="submitMetering(board)">提交测光核验</button>
          </div>
        </div>

        <!-- 阶段二：主管复核 -->
        <div v-else-if="board.阶段 === '主管复核'" class="action-panel">
          <h4>② 照明主管复核</h4>
          <div v-if="board.规则冲突" class="conflict-box">
            道路等级阈值给出「{{ board.道路规则级别 }}」，现场安全结论给出「{{ board.现场安全级别 }}」，
            两者冲突，最终抢修级别以主管复核为准。
          </div>
          <p v-else class="page-desc">两路规则一致，仍需主管复核确认后才能下发车辆。</p>
          <label class="filter-item">
            <span>复核抢修级别</span>
            <select v-model="reviewDraft[board.回路编号]">
              <option v-for="level in levelOrder" :key="level" :value="level">{{ level }}</option>
            </select>
          </label>
          <div class="panel-actions">
            <button class="btn ghost" type="button" @click="recompute(board)">按回路版本重算故障包</button>
            <button class="btn primary" type="button" @click="submitReview(board)">提交主管复核</button>
          </div>
        </div>

        <!-- 阶段三：故障包下发 -->
        <div v-else-if="board.阶段 === '已复核'" class="action-panel">
          <h4>③ 合并故障包并下发车辆</h4>
          <p class="page-desc">
            故障级别 <b class="level-mark">{{ board.故障包?.裁决级别 }}</b> ·
            故障灯 {{ board.故障包?.故障灯数 }} 盏 · {{ board.故障包?.抢修时限 }}
          </p>
          <label class="filter-item">
            <span>承接车辆</span>
            <select v-model.number="dispatchDraft[board.回路编号]">
              <option v-for="vehicle in vehicles" :key="vehicle.id" :value="vehicle.id">
                {{ vehicle.车辆编号 }} · {{ vehicle.车牌号 }} · {{ vehicle.驾驶员 }}
              </option>
            </select>
          </label>
          <div class="panel-actions">
            <button class="btn ghost" type="button" @click="recompute(board)">按回路版本重算故障包</button>
            <button class="btn primary" type="button" @click="submitDispatch(board)">下发车辆</button>
          </div>
        </div>

        <!-- 执行中 -->
        <div v-else-if="board.阶段 === '执行中'" class="action-panel">
          <h4>④ 车辆执行中：{{ board.车辆编号 }}</h4>
          <p class="page-desc">抢修完成后归档，灭灯记录将冻结当时阈值，台账回写已修复。</p>
          <div class="panel-actions">
            <button class="btn primary" type="button" @click="submitArchive(board)">执行归档</button>
          </div>
        </div>

        <!-- 已归档 -->
        <div v-else class="action-panel">
          <h4>⑤ 已归档 · {{ board.最近级别 ?? '—' }}</h4>
          <p class="page-desc">历史灭灯记录与阈值快照已保留，可开启下一版本重新测光裁决。</p>
          <div class="panel-actions">
            <button class="btn primary" type="button" @click="submitNewVersion(board)">开启新版本</button>
          </div>
        </div>

        <details class="package-box" v-if="board.故障包">
          <summary>故障包 {{ board.故障包.包指纹 }} · 杆号 {{ board.故障包.杆号清单.join('、') }}</summary>
          <p class="root">{{ board.故障包.根因 }}</p>
          <p class="page-desc" v-if="board.规则冲突">冲突处置：{{ board.故障包.冲突处置 }}</p>
        </details>
      </article>
    </div>

    <!-- 维修清单 -->
    <div v-if="activeTab === 'repairs'">
      <table class="data-table">
        <thead>
          <tr><th>维修单号</th><th>回路</th><th>版本</th><th>抢修级别</th><th>故障灯数</th><th>杆号清单</th><th>根因</th><th>时限</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in repairs" :key="String(row.id)">
            <td>{{ row.维修单号 }}</td><td>{{ row.回路编号 }}</td><td>v{{ row.版本号 }}</td>
            <td>{{ row.抢修级别 }}</td><td>{{ row.故障灯数 }}</td>
            <td>{{ row.杆号清单?.join('、') }}</td><td class="root">{{ row.根因 }}</td>
            <td>{{ row.抢修时限 }}</td><td>{{ row.状态 }}</td>
          </tr>
          <tr v-if="!repairs.length"><td colspan="9" class="empty-state">暂无维修清单，完成测光核验后自动生成</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 车辆路径 -->
    <div v-if="activeTab === 'routes'">
      <table class="data-table">
        <thead>
          <tr><th>路径单号</th><th>回路</th><th>版本</th><th>级别</th><th>车辆</th><th>驾驶员</th><th>停靠点顺序</th><th>状态</th><th>下发时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in routes" :key="String(row.id)">
            <td>{{ row.路径单号 }}</td><td>{{ row.回路编号 }}</td><td>v{{ row.版本号 }}</td>
            <td>{{ row.抢修级别 }}</td><td>{{ row.车辆编号 }} {{ row.车牌号 }}</td><td>{{ row.驾驶员 }}</td>
            <td>{{ row.停靠点?.join(' → ') }}</td><td>{{ row.状态 }}</td><td>{{ row.下发时间 }}</td>
          </tr>
          <tr v-if="!routes.length"><td colspan="9" class="empty-state">暂无路径待办，故障包下发车辆后在此汇总</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 历史灭灯 -->
    <div v-if="activeTab === 'history'">
      <table class="data-table">
        <thead>
          <tr><th>灭灯记录</th><th>回路</th><th>版本</th><th>道路等级</th><th>级别</th><th>杆号清单</th><th>当时阈值快照</th><th>复核人</th><th>归档时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in history" :key="String(row.id)">
            <td>{{ row.灭灯记录编号 }}</td><td>{{ row.回路编号 }}</td><td>v{{ row.版本号 }}</td>
            <td>{{ row.道路等级 }}</td><td>{{ row.抢修级别 }}</td><td>{{ row.杆号清单?.join('、') }}</td>
            <td class="threshold-cell">
              一级≥{{ row.当时阈值快照?.一级灭灯阈值 }}盏 · 二级≥{{ row.当时阈值快照?.二级灭灯阈值 }}盏 ·
              照度≥{{ row.当时阈值快照?.最低维持照度_lx }}lx（{{ row.当时阈值快照?.规则版本 }}）
            </td>
            <td>{{ row.复核人 }}</td><td>{{ row.归档时间 }}</td>
          </tr>
          <tr v-if="!history.length"><td colspan="9" class="empty-state">暂无归档灭灯记录</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/lighting'
const statuses = ['正常', '不亮', '闪烁', '已修复']
const tabs = [
  { key: 'ledger', label: '灯具台账' },
  { key: 'board', label: '回路棋盘' },
  { key: 'repairs', label: '维修清单' },
  { key: 'routes', label: '车辆路径' },
  { key: 'history', label: '历史灭灯' },
] as const

const activeTab = ref<string>('board')
const message = ref('')
const messageOk = ref(true)

const boardStageOrder = ['测光核验', '主管复核', '已复核', '执行中', '已归档']
const levelOrder = ['一级抢修', '二级抢修', '三级抢修']

const boards = ref<Row[]>([])
const repairs = ref<Row[]>([])
const routes = ref<Row[]>([])
const history = ref<Row[]>([])
const vehicles = ref<Row[]>([])

const meteringDraft = reactive<Record<string, Record<string, { lux: number | null; safety: string }>>>({})
const reviewDraft = reactive<Record<string, string>>({})
const dispatchDraft = reactive<Record<string, number>>({})

const ledgerRows = ref<Row[]>([])
const ledgerTotal = ref(0)
const ledgerQuery = reactive({ keyword: '', status: '' })
const ledgerColumns = [
  '灯具编号', '灯具类型', '功率', '所属路段', '杆号', '设施状态',
  '裁决回路', '裁决版本', '裁决级别', '根因',
]

function notify(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

async function callApi(path: string, init?: RequestInit): Promise<any> {
  const response = await request(path, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail ?? '操作未生效，请稍后重试')
  }
  return payload
}

function darkLamps(board: Row): Row[] {
  return (board.灯具 ?? []).filter((lamp: Row) => ['不亮', '闪烁'].includes(lamp.现象))
}

function ensureDraft(board: Row) {
  if (!meteringDraft[board.回路编号]) {
    meteringDraft[board.回路编号] = {}
  }
  for (const lamp of darkLamps(board)) {
    if (!meteringDraft[board.回路编号][lamp.杆号]) {
      meteringDraft[board.回路编号][lamp.杆号] = { lux: null, safety: '正常' }
    }
  }
}

function fillSample(board: Row) {
  ensureDraft(board)
  const draft = meteringDraft[board.回路编号]
  darkLamps(board).forEach((lamp, index) => {
    draft[lamp.杆号].lux = index === 0 ? 12 : 30
    draft[lamp.杆号].safety = index === 0 ? '高风险' : '正常'
  })
}

async function loadBoards() {
  const payload = await callApi(`${ENDPOINT}/circuits/board`)
  boards.value = payload.items ?? []
  for (const board of boards.value) {
    ensureDraft(board)
    if (!reviewDraft[board.回路编号]) {
      reviewDraft[board.回路编号] = board.故障包?.建议级别 ?? '二级抢修'
    }
    if (!dispatchDraft[board.回路编号]) {
      dispatchDraft[board.回路编号] = vehicles.value[0]?.id
    }
  }
}

async function loadList(tab: string) {
  const map: Record<string, { target: typeof repairs; path: string }> = {
    repairs: { target: repairs, path: '/circuits/repairs' },
    routes: { target: routes, path: '/circuits/routes' },
    history: { target: history, path: '/circuits/outage-history' },
  }
  const config = map[tab]
  if (!config) return
  const payload = await callApi(`${ENDPOINT}${config.path}`)
  config.target.value = payload.items ?? []
}

async function reloadLedger() {
  const query = new URLSearchParams()
  if (ledgerQuery.keyword) query.set('keyword', ledgerQuery.keyword)
  if (ledgerQuery.status) query.set('status', ledgerQuery.status)
  const payload = await callApi(`${ENDPOINT}?${query.toString()}`)
  ledgerRows.value = payload.items ?? []
  ledgerTotal.value = payload.total ?? 0
}

function switchTab(tab: string) {
  activeTab.value = tab
  message.value = ''
  if (tab === 'ledger') void reloadLedger()
  else void loadList(tab)
}

async function boardAction(path: string, body: unknown, successText: string) {
  try {
    await callApi(`${ENDPOINT}/circuits${path}`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    notify(successText)
    await loadBoards()
    await Promise.all([loadList('repairs'), loadList('routes'), loadList('history'), reloadLedger()])
  } catch (error) {
    notify(error instanceof Error ? error.message : '操作失败', false)
  }
}

function submitMetering(board: Row) {
  ensureDraft(board)
  const draft = meteringDraft[board.回路编号]
  const readings = darkLamps(board).map((lamp) => {
    const item = draft[lamp.杆号]
    return { 杆号: lamp.杆号, 照度: item.lux, 现场安全结论: item.safety }
  })
  if (readings.some((item) => item.照度 === null || Number.isNaN(item.照度))) {
    notify('每根灭灯杆号都需要填写实测照度', false)
    return
  }
  void boardAction(
    `/${board.回路编号}/metering`,
    { readings, inspector: '值班测光员', expected_version: versionOf(board) },
    '测光核验已提交，回路推进到主管复核',
  )
}

function submitReview(board: Row) {
  void boardAction(
    `/${board.回路编号}/review`,
    {
      reviewed_level: reviewDraft[board.回路编号],
      reviewer: '照明主管',
      remark: '道路等级与现场安全冲突时以主管复核为准',
      expected_version: versionOf(board),
    },
    '主管复核已确认，结论已回写灯具台账与维修清单',
  )
}

function recompute(board: Row) {
  void boardAction(
    `/${board.回路编号}/recompute`,
    { expected_version: versionOf(board) },
    '故障包已按回路版本重算（证据未变时幂等不重复落库）',
  )
}

function submitDispatch(board: Row) {
  void boardAction(
    `/${board.回路编号}/dispatch`,
    {
      vehicle_id: dispatchDraft[board.回路编号],
      operator: '值班调度',
      expected_version: versionOf(board),
    },
    '故障包已下发，车辆路径待办与阶段在同一事务落库',
  )
}

function submitArchive(board: Row) {
  void boardAction(
    `/${board.回路编号}/archive`,
    { expected_version: versionOf(board) },
    '已归档：灭灯记录保留当时阈值，台账置为已修复',
  )
}

function submitNewVersion(board: Row) {
  void boardAction(`/${board.回路编号}/new-version`, {}, '新版本已开启，请重新测光核验')
}

function versionOf(board: Row): number {
  return Number(String(board.当前版本).replace('v', ''))
}

onMounted(async () => {
  try {
    const vehiclePayload = await callApi('/api/vehicle?size=200')
    vehicles.value = vehiclePayload.items ?? []
    await loadBoards()
    await Promise.all([reloadLedger(), loadList('repairs'), loadList('routes'), loadList('history')])
  } catch (error) {
    notify(error instanceof Error ? error.message : '回路棋盘加载失败', false)
  }
})
</script>

<style scoped>
.tab-bar { display: flex; gap: 8px; margin: 8px 0 12px; }
.tab-btn { border: 1px solid var(--border); background: #fff; border-radius: 6px 6px 0 0; padding: 8px 14px; cursor: pointer; font-size: 13px; }
.tab-btn.active { background: var(--brand); color: #fff; border-color: var(--brand); }
.notice { background: #ecfdf3; border: 1px solid #6ce9a6; color: #027a48; padding: 8px 12px; border-radius: 6px; font-size: 13px; }
.notice-error { background: #fef3f2; border-color: #fda29b; color: #b42318; }
.board-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-bottom: 14px; }
.board-head { display: flex; justify-content: space-between; align-items: flex-start; }
.board-head h3 { margin: 0; font-size: 16px; }
.stage-pill { background: #eff8ff; color: #175cd3; border: 1px solid #b2ddff; border-radius: 999px; padding: 2px 12px; font-size: 12px; }
.stepper { display: flex; gap: 0; margin: 14px 0 10px; padding: 0; list-style: none; }
.step { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 4px; position: relative; font-size: 12px; color: var(--muted); }
.step::before { content: ''; position: absolute; top: 11px; left: -50%; width: 100%; height: 2px; background: var(--border); z-index: 0; }
.step:first-child::before { display: none; }
.step-dot { width: 24px; height: 24px; border-radius: 50%; background: #fff; border: 2px solid var(--border); display: flex; align-items: center; justify-content: center; z-index: 1; }
.step.done { color: #027a48; }
.step.done .step-dot { background: #d1fadf; border-color: #12b76a; }
.step.done::before { background: #12b76a; }
.step.current { color: #175cd3; font-weight: 600; }
.step.current .step-dot { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.rule-line { display: flex; gap: 18px; flex-wrap: wrap; font-size: 13px; padding: 8px 10px; background: #f8fafc; border-radius: 6px; }
.level-mark { color: #b42318; }
.conflict-flag { color: #b42318; font-weight: 600; }
.action-panel { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 12px; }
.action-panel h4 { margin: 0 0 8px; font-size: 14px; }
.panel-actions { display: flex; gap: 10px; margin-top: 10px; }
.conflict-box { background: #fffaeb; border: 1px solid #fedf89; color: #b54708; padding: 8px 10px; border-radius: 6px; font-size: 13px; margin-bottom: 8px; }
.inner { margin: 8px 0; }
.inner input, .inner select, .filter-item select { padding: 4px 6px; }
.package-box { margin-top: 12px; font-size: 13px; }
.root { color: #b42318; }
.threshold-cell { color: var(--muted); font-size: 12px; }
</style>
