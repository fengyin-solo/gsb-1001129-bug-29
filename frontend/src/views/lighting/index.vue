<template>
  <section class="page" data-module="lighting">
    <header class="page-head">
      <div>
        <h2>路灯照明管理 · 回路棋盘</h2>
        <p class="page-desc">
          照度级别裁决收成版本化状态机：测光核验 → 主管复核 → 故障包合并 → 车辆下发 → 执行中 → 已闭环，
          跳级由服务端拒绝；杆号清单、抢修日历、车辆路线共用同一份裁决结论。
        </p>
      </div>
    </header>

    <nav class="tabs">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="errorMessage" class="error-banner">{{ errorMessage }}</p>

    <!-- ============ 回路棋盘 ============ -->
    <template v-if="activeTab === 'board'">
      <div class="stat-row">
        <article class="stat-card"><span class="stat-label">回路总数</span>
          <strong class="stat-value">{{ boards.length }}</strong></article>
        <article class="stat-card"><span class="stat-label">执行中版本</span>
          <strong class="stat-value">{{ runningCount }}</strong></article>
        <article class="stat-card"><span class="stat-label">待主管裁决冲突</span>
          <strong class="stat-value">{{ conflictCount }}</strong></article>
      </div>

      <form class="filter-bar" @submit.prevent="loadBoards">
        <label class="filter-item">
          <span>回路编号 / 名称</span>
          <input v-model="boardFilter.keyword" placeholder="如 C-101" />
        </label>
        <label class="filter-item">
          <span>阶段</span>
          <select v-model="boardFilter.stage">
            <option value="">全部阶段</option>
            <option v-for="stage in stages" :key="stage" :value="stage">{{ stage }}</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn primary" type="button" @click="openNewVersion">开启新版本</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>回路编号</th><th>回路名称</th><th>版本</th><th>阶段</th>
            <th>测光读数(lx)</th><th>各规则判定</th><th>统一裁决级别</th><th>冲突</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in boards" :key="`${row.回路编号}#${row.版本}`">
            <td>{{ row.回路编号 }}</td>
            <td>{{ row.回路名称 }}</td>
            <td>v{{ row.版本 }}</td>
            <td><span class="stage-pill">{{ row.阶段 }}</span></td>
            <td>{{ row.测光读数 ?? '—' }}</td>
            <td>
              <span v-for="(level, source) in sourceVerdicts(row)" :key="source" class="mini-level">
                {{ source }}:{{ level }}
              </span>
            </td>
            <td>
              <strong :class="levelClass(row.裁决级别)">{{ row.裁决级别 || '待测光' }}</strong>
            </td>
            <td>
              <span v-if="row.存在冲突" class="conflict-flag" :title="row.冲突规则.join('；')">
                {{ row.冲突规则.length }} 处待裁定
              </span>
              <span v-else class="muted">—</span>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="loadDetail(row.回路编号)">棋盘详情</button>
            </td>
          </tr>
          <tr v-if="!boards.length">
            <td colspan="9" class="empty-state">暂无回路棋盘数据，可先开启新版本</td>
          </tr>
        </tbody>
      </table>

      <!-- 棋盘详情抽屉：杆号清单 / 台账 / 维修 / 日历 / 路线同屏 -->
      <div v-if="detail" class="drawer">
        <div class="drawer-head">
          <strong>{{ detail.回路编号 }} · {{ detail.回路名称 }}（v{{ detail.版本 }}）</strong>
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
        </div>

        <ol class="stepper">
          <li v-for="(stage, idx) in stages" :key="stage"
              :class="{ done: stageIndex(detail.阶段) > idx, current: detail.阶段 === stage }">
            {{ idx + 1 }}. {{ stage }}
          </li>
        </ol>

        <div class="verdict-box">
          <p><b>测光读数：</b>{{ detail.测光读数 ?? '未核验' }} lx
            ｜<b>道路等级：</b>{{ detail.道路等级 }}
            ｜<b>现场安全：</b>{{ detail.现场安全结论 || '—' }}</p>
          <p>
            <b>各规则判定：</b>
            <span v-for="(level, source) in detail.各源判定" :key="source" class="mini-level">
              {{ source }}:{{ level }}
            </span>
          </p>
          <p><b>阈值版本（冻结）：</b>
            <span v-for="(value, source) in detail.阈值版本" :key="source" class="mini-level">
              {{ source }}≥{{ value }}lx
            </span>
          </p>
          <p v-if="detail.冲突规则?.length" class="conflict-text">
            <b>规则冲突（以照明主管复核为准）：</b><br />
            <span v-for="(item, i) in detail.冲突规则" :key="i">· {{ item }}<br /></span>
          </p>
          <p><b>最终抢修级别：</b>
            <strong :class="levelClass(detail.最终抢修级别)">
              {{ detail.最终抢修级别 || (detail.冲突规则?.length ? '待主管裁定' : '—') }}
            </strong>
            <span v-if="detail.主管复核人" class="muted">
              （{{ detail.主管复核人 }} {{ detail.主管复核时间 }}）
            </span>
          </p>
        </div>

        <!-- 阶段一：测光核验 -->
        <form v-if="detail.阶段 === '测光核验'" class="action-box" @submit.prevent="submitVerify">
          <h4>① 调度员核对测光证据</h4>
          <label>测光读数(lx)<input v-model.number="form.verify.reading" type="number" step="0.1" /></label>
          <label>证据核对人<input v-model="form.verify.checker" placeholder="调度员姓名" /></label>
          <label>现场安全结论<input v-model="form.verify.safety" :placeholder="detail.现场安全结论 || '如：学校路口，夜间行人密集'" /></label>
          <label>杆号清单阈值(lx)<input v-model.number="form.verify.thresholds.杆号清单" type="number" step="0.1" /></label>
          <label>抢修日历阈值(lx)<input v-model.number="form.verify.thresholds.抢修日历" type="number" step="0.1" /></label>
          <label>车辆路线阈值(lx)<input v-model.number="form.verify.thresholds.车辆路线" type="number" step="0.1" /></label>
          <button class="btn primary" type="submit">核验通过，提交主管复核</button>
        </form>

        <!-- 阶段二：主管复核 -->
        <form v-else-if="detail.阶段 === '主管复核'" class="action-box" @submit.prevent="submitReview">
          <h4>② 照明主管复核（冲突以本结论为准）</h4>
          <label>主管裁决级别
            <select v-model="form.review.level">
              <option value="" disabled>请选择</option>
              <option v-for="level in levels" :key="level" :value="level">{{ level }}</option>
            </select>
          </label>
          <label>复核人<input v-model="form.review.reviewer" placeholder="照明主管姓名" /></label>
          <label class="wide">复核意见<input v-model="form.review.opinion" placeholder="规则冲突时说明裁决依据" /></label>
          <button class="btn primary" type="submit">主管敲章，进入故障包合并</button>
        </form>

        <!-- 阶段三：故障包合并 -->
        <div v-else-if="detail.阶段 === '故障包合并'" class="action-box">
          <h4>③ 合并故障包（按回路版本幂等重算）</h4>
          <p class="muted">将合并本回路全部「不亮/闪烁」杆号，并回写灯具台账、维修清单与抢修日历。</p>
          <button class="btn primary" type="button" @click="submitMerge">合并故障包并驱动重算</button>
        </div>

        <!-- 阶段四：车辆下发 -->
        <form v-else-if="detail.阶段 === '车辆下发'" class="action-box" @submit.prevent="submitDispatch">
          <h4>④ 下发车辆（同版本并发下发只允许一次）</h4>
          <p class="muted">待发杆号：{{ detail.故障包?.杆号清单?.join('、') }}</p>
          <label>车辆编号（留空自动指派）<input v-model="form.dispatch.vehicle" placeholder="如 VEH-0001" /></label>
          <button class="btn primary" type="submit">下发车辆</button>
          <button class="btn" type="button" @click="submitMerge">重新幂等重算故障包</button>
        </form>

        <!-- 执行中：回执闭环 -->
        <div v-else-if="detail.阶段 === '执行中'" class="action-box">
          <h4>⑤ 车辆执行中</h4>
          <p class="muted">路线已落库，维修回执后闭环版本，台账/清单/路线统一归档。</p>
          <button class="btn primary" type="button" @click="submitComplete">回执闭环</button>
        </div>

        <div v-else class="action-box">
          <h4>本版本已闭环</h4>
          <p class="muted">闭环时间：{{ detail.闭环时间 }}。历史灭灯记录与当时阈值已保留，可开启新版本。</p>
        </div>

        <div class="drawer-grids">
          <div>
            <h4>杆号清单（统一级别：{{ detail.最终抢修级别 || '待裁决' }}）</h4>
            <table class="data-table compact">
              <thead><tr><th>杆号</th><th>灯具编号</th><th>状态</th><th>不亮原因</th><th>裁决级别</th></tr></thead>
              <tbody>
                <tr v-for="lamp in detail.灯具台账" :key="lamp.id">
                  <td>{{ lamp.杆号 }}</td><td>{{ lamp.灯具编号 }}</td>
                  <td>{{ lamp.status }}</td><td>{{ lamp.不亮原因 || '—' }}</td>
                  <td>{{ lamp.抢修级别 || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div>
            <h4>维修清单</h4>
            <table class="data-table compact">
              <thead><tr><th>故障包</th><th>杆号</th><th>级别</th><th>计划日期</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="item in detail.维修清单" :key="item.id">
                  <td>{{ item.故障包键 }}</td>
                  <td>{{ item.杆号清单?.join('、') }}</td>
                  <td :class="levelClass(item.抢修级别)">{{ item.抢修级别 }}</td>
                  <td>{{ item.计划日期 }}</td><td>{{ item.维修状态 }}</td>
                </tr>
                <tr v-if="!detail.维修清单.length"><td colspan="5" class="empty-state">尚未合并故障包</td></tr>
              </tbody>
            </table>
            <h4 style="margin-top:12px">抢修日历</h4>
            <table class="data-table compact">
              <thead><tr><th>计划日期</th><th>级别</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="item in detail.抢修日历" :key="item.id">
                  <td>{{ item.计划日期 }}</td>
                  <td :class="levelClass(item.抢修级别)">{{ item.抢修级别 }}</td>
                  <td>{{ item.抢修日历状态 }}</td>
                </tr>
                <tr v-if="!detail.抢修日历.length"><td colspan="3" class="empty-state">尚未排期</td></tr>
              </tbody>
            </table>
            <h4 style="margin-top:12px">车辆路线（路径汇总）</h4>
            <table class="data-table compact">
              <tbody>
                <tr v-if="detail.车辆路线">
                  <td>{{ detail.车辆路线.车辆编号 }}（{{ detail.车辆路线.车辆类型 }}）→
                    {{ detail.车辆路线.停靠点?.join(' → ') }}｜{{ detail.车辆路线.路径状态 }}
                    ｜{{ detail.车辆路线.下发时间 }}</td>
                </tr>
                <tr v-else><td class="empty-state">尚未下发车辆</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>

    <!-- ============ 灯具台账 ============ -->
    <template v-else-if="activeTab === 'lamps'">
      <div class="stat-row">
        <article v-for="item in lampStats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span><strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>
      <form class="filter-bar" @submit.prevent="loadLamps">
        <label class="filter-item"><span>灯具编号/杆号</span>
          <input v-model="lampFilter.keyword" placeholder="按编号或杆号检索" /></label>
        <label class="filter-item"><span>状态</span>
          <select v-model="lampFilter.status">
            <option value="">全部</option>
            <option v-for="s in lampStatuses" :key="s" :value="s">{{ s }}</option>
          </select></label>
        <label class="filter-item"><span>回路编号</span>
          <input v-model="lampFilter.circuit" placeholder="如 C-101" /></label>
        <button class="btn" type="submit">查询</button>
        <button class="btn" type="button" @click="exportLamps">导出台账</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in lampColumns" :key="column">{{ column }}</th>
            <th>抢修级别</th><th>作业状态</th><th>历史灭灯（保留当时阈值）</th><th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in lamps" :key="String(row.id)">
            <td v-for="column in lampColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td :class="levelClass(row.抢修级别)">{{ row.抢修级别 || '—' }}</td>
            <td>{{ row.作业状态 || '—' }}</td>
            <td>
              <details>
                <summary>{{ row.灭灯记录?.length || 0 }} 条</summary>
                <ul class="history-list">
                  <li v-for="(rec, i) in row.灭灯记录" :key="i">
                    {{ rec.日期 }}｜v{{ rec.故障包版本 ?? '—' }}｜读数 {{ rec.测光读数 ?? '—' }}lx
                    ｜{{ rec.当时抢修级别 || '未定级' }}<br />
                    <span class="muted">当时阈值：
                      {{ Object.entries(rec.当时照度阈值 ?? {}).map(([k, v]) => `${k}≥${v}lx`).join('，') }}
                    </span>
                  </li>
                </ul>
              </details>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="runLampAction('登记故障', row)">登记故障</button>
              <button class="link" type="button" @click="runLampAction('派发修复', row)">派发修复</button>
              <button class="link" type="button" @click="runLampAction('确认修复', row)">确认修复</button>
            </td>
          </tr>
          <tr v-if="!lamps.length">
            <td :colspan="lampColumns.length + 4" class="empty-state">暂无路灯照明数据</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot"><span>共 {{ lampTotal }} 条灯具记录</span></footer>
    </template>

    <!-- ============ 维修清单 ============ -->
    <template v-else-if="activeTab === 'repairs'">
      <form class="filter-bar" @submit.prevent="loadRepairs">
        <label class="filter-item"><span>回路编号</span><input v-model="summaryFilter.circuit" /></label>
        <label class="filter-item"><span>抢修级别</span>
          <select v-model="summaryFilter.level">
            <option value="">全部</option>
            <option v-for="level in levels" :key="level" :value="level">{{ level }}</option>
          </select></label>
        <button class="btn" type="submit">查询</button>
      </form>
      <table class="data-table">
        <thead><tr><th>故障包</th><th>回路</th><th>版本</th><th>杆号清单</th><th>级别</th>
          <th>所属路段</th><th>不亮原因</th><th>计划日期</th><th>状态</th><th>更新时间</th></tr></thead>
        <tbody>
          <tr v-for="item in repairs" :key="item.id">
            <td>{{ item.故障包键 }}</td><td>{{ item.回路编号 }}</td><td>v{{ item.版本 }}</td>
            <td>{{ item.杆号清单?.join('、') }}</td>
            <td :class="levelClass(item.抢修级别)">{{ item.抢修级别 }}</td>
            <td>{{ item.所属路段 }}</td><td>{{ item.不亮原因 }}</td>
            <td>{{ item.计划日期 }}</td><td>{{ item.维修状态 }}</td><td>{{ item.更新时间 }}</td>
          </tr>
          <tr v-if="!repairs.length"><td colspan="10" class="empty-state">暂无维修清单</td></tr>
        </tbody>
      </table>
    </template>

    <!-- ============ 抢修日历 ============ -->
    <template v-else-if="activeTab === 'calendar'">
      <form class="filter-bar" @submit.prevent="loadCalendar">
        <label class="filter-item"><span>回路编号</span><input v-model="summaryFilter.circuit" /></label>
        <button class="btn" type="submit">查询</button>
      </form>
      <table class="data-table">
        <thead><tr><th>故障包</th><th>回路</th><th>版本</th><th>计划日期</th><th>级别</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="item in calendarRows" :key="item.id">
            <td>{{ item.故障包键 }}</td><td>{{ item.回路编号 }}</td><td>v{{ item.版本 }}</td>
            <td>{{ item.计划日期 }}</td>
            <td :class="levelClass(item.抢修级别)">{{ item.抢修级别 }}</td>
            <td>{{ item.抢修日历状态 }}</td>
          </tr>
          <tr v-if="!calendarRows.length"><td colspan="6" class="empty-state">暂无抢修日历排期</td></tr>
        </tbody>
      </table>
    </template>

    <!-- ============ 车辆路径 ============ -->
    <template v-else>
      <form class="filter-bar" @submit.prevent="loadRoutes">
        <label class="filter-item"><span>回路编号</span><input v-model="summaryFilter.circuit" /></label>
        <button class="btn" type="submit">查询</button>
      </form>
      <table class="data-table">
        <thead><tr><th>故障包</th><th>回路</th><th>版本</th><th>级别</th><th>车辆</th>
          <th>停靠点路线</th><th>状态</th><th>下发时间</th></tr></thead>
        <tbody>
          <tr v-for="item in routes" :key="item.id">
            <td>{{ item.故障包键 }}</td><td>{{ item.回路编号 }}</td><td>v{{ item.版本 }}</td>
            <td :class="levelClass(item.抢修级别)">{{ item.抢修级别 }}</td>
            <td>{{ item.车辆编号 }}（{{ item.车辆类型 }}）</td>
            <td>{{ item.停靠点?.join(' → ') }}</td>
            <td>{{ item.路径状态 }}</td><td>{{ item.下发时间 }}</td>
          </tr>
          <tr v-if="!routes.length"><td colspan="8" class="empty-state">暂无车辆路径待办</td></tr>
        </tbody>
      </table>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type AnyRow = Record<string, any>

const ENDPOINT = '/api/lighting'
const stages = ['测光核验', '主管复核', '故障包合并', '车辆下发', '执行中', '已闭环']
const levels = ['一级抢修', '二级抢修', '三级抢修']
const lampStatuses = ['正常', '不亮', '闪烁', '已修复']
const lampColumns = ['灯具编号', '灯具类型', '功率', '所属路段', '回路编号', '安装日期', '杆号', '不亮原因', '设施状态']

const tabs = [
  { key: 'board', label: '回路棋盘' },
  { key: 'lamps', label: '灯具台账' },
  { key: 'repairs', label: '维修清单' },
  { key: 'calendar', label: '抢修日历' },
  { key: 'routes', label: '车辆路径' },
] as const
type TabKey = (typeof tabs)[number]['key']
const activeTab = ref<TabKey>('board')

const boards = ref<AnyRow[]>([])
const lamps = ref<AnyRow[]>([])
const lampTotal = ref(0)
const repairs = ref<AnyRow[]>([])
const calendarRows = ref<AnyRow[]>([])
const routes = ref<AnyRow[]>([])
const detail = ref<AnyRow | null>(null)
const errorMessage = ref('')

const boardFilter = reactive({ keyword: '', stage: '' })
const lampFilter = reactive({ keyword: '', status: '', circuit: '' })
const summaryFilter = reactive({ circuit: '', level: '' })

const form = reactive({
  verify: {
    reading: 8.4, checker: '', safety: '',
    thresholds: { 杆号清单: 15, 抢修日历: 20, 车辆路线: 12 },
  },
  review: { level: '', reviewer: '', opinion: '' },
  dispatch: { vehicle: '' },
})

const runningCount = computed(() => boards.value.filter((row) => row.阶段 === '执行中').length)
const conflictCount = computed(
  () => boards.value.filter((row) => row.存在冲突 && row.阶段 !== '已闭环').length,
)
const lampStats = computed(() => [
  { label: '灯具总数', value: lampTotal.value },
  { label: '不亮灯具', value: lamps.value.filter((r) => r.status === '不亮').length },
  { label: '修复中/闪烁', value: lamps.value.filter((r) => r.status === '闪烁').length },
  { label: '已修复', value: lamps.value.filter((r) => r.status === '已修复').length },
])

function stageIndex(stage: string): number {
  return stages.indexOf(stage)
}
function levelClass(level?: string): string {
  if (level === '一级抢修') return 'level-1'
  if (level === '二级抢修') return 'level-2'
  if (level === '三级抢修') return 'level-3'
  return 'muted'
}
function sourceVerdicts(row: AnyRow): AnyRow {
  const result: AnyRow = {}
  for (const source of ['杆号清单', '抢修日历', '车辆路线']) {
    if (row.各源判定?.[source]) result[source] = row.各源判定[source]
  }
  return result
}

async function apiPost(path: string, body: AnyRow): Promise<AnyRow> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || `操作被拒绝（${response.status}）`)
  }
  return payload
}

async function apiGet(path: string): Promise<AnyRow> {
  const response = await request(path)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(payload.detail || `读取失败（${response.status}）`)
  }
  return response.json()
}

function fail(error: unknown) {
  errorMessage.value = error instanceof Error ? error.message : '操作失败'
}

async function switchTab(tab: TabKey) {
  activeTab.value = tab
  errorMessage.value = ''
  try {
    if (tab === 'board') await loadBoards()
    if (tab === 'lamps') await loadLamps()
    if (tab === 'repairs') await loadRepairs()
    if (tab === 'calendar') await loadCalendar()
    if (tab === 'routes') await loadRoutes()
  } catch (error) {
    fail(error)
  }
}

async function loadBoards() {
  const params = new URLSearchParams()
  if (boardFilter.keyword) params.set('keyword', boardFilter.keyword)
  if (boardFilter.stage) params.set('stage', boardFilter.stage)
  const payload = await apiGet(`${ENDPOINT}/circuits?${params.toString()}`)
  boards.value = payload.items ?? []
}

async function loadDetail(code: string) {
  errorMessage.value = ''
  try {
    detail.value = await apiGet(`${ENDPOINT}/circuits/${code}`)
    form.verify.reading = detail.value.测光读数 ?? 8.4
    form.verify.safety = detail.value.现场安全结论 ?? ''
    form.verify.checker = detail.value.证据核对人 ?? ''
    form.verify.thresholds = { ...(detail.value.阈值版本 ?? form.verify.thresholds) }
    form.review.level = ''
    form.review.reviewer = detail.value.主管复核人 ?? ''
    form.review.opinion = detail.value.主管复核意见 ?? ''
    form.dispatch.vehicle = ''
  } catch (error) {
    fail(error)
  }
}

async function refreshDetail() {
  if (detail.value) {
    await loadDetail(detail.value.回路编号)
    await loadBoards()
  }
}

async function submitVerify() {
  if (!detail.value) return
  try {
    const payload = await apiPost(`${ENDPOINT}/circuits/verify`, {
      values: {
        回路编号: detail.value.回路编号,
        版本: detail.value.版本,
        测光读数: form.verify.reading,
        证据核对人: form.verify.checker,
        现场安全结论: form.verify.safety,
        阈值版本: form.verify.thresholds,
      },
    })
    detail.value = payload.board
    await refreshDetail()
  } catch (error) {
    fail(error)
  }
}

async function submitReview() {
  if (!detail.value) return
  try {
    const payload = await apiPost(`${ENDPOINT}/circuits/review`, {
      values: {
        回路编号: detail.value.回路编号,
        版本: detail.value.版本,
        主管裁决级别: form.review.level,
        主管复核人: form.review.reviewer,
        主管复核意见: form.review.opinion,
      },
    })
    detail.value = payload.board
    await refreshDetail()
  } catch (error) {
    fail(error)
  }
}

async function submitMerge() {
  if (!detail.value) return
  try {
    const payload = await apiPost(`${ENDPOINT}/circuits/merge`, {
      values: { 回路编号: detail.value.回路编号, 版本: detail.value.版本 },
    })
    detail.value = payload.board
    await refreshDetail()
  } catch (error) {
    fail(error)
  }
}

async function submitDispatch() {
  if (!detail.value) return
  try {
    const payload = await apiPost(`${ENDPOINT}/circuits/dispatch`, {
      values: {
        回路编号: detail.value.回路编号,
        版本: detail.value.版本,
        车辆编号: form.dispatch.vehicle || undefined,
      },
    })
    detail.value = payload.board
    await refreshDetail()
  } catch (error) {
    fail(error)
  }
}

async function submitComplete() {
  if (!detail.value) return
  try {
    const payload = await apiPost(`${ENDPOINT}/circuits/complete`, {
      values: { 回路编号: detail.value.回路编号, 版本: detail.value.版本 },
    })
    detail.value = payload.board
    await refreshDetail()
  } catch (error) {
    fail(error)
  }
}

async function openNewVersion() {
  const code = window.prompt('开启新版本的回路编号（如 C-101；新回路会自动登记）：', 'C-101')
  if (!code) return
  const grade = window.prompt('道路等级（快速路/主干道/次干道/支路）：', '主干道') ?? ''
  const safety = window.prompt('现场安全结论：', '学校路口，夜间行人密集') ?? ''
  try {
    await apiPost(`${ENDPOINT}/circuits/open`, {
      values: { 回路编号: code, 道路等级: grade, 现场安全结论: safety },
    })
    await loadBoards()
    await loadDetail(code.trim())
  } catch (error) {
    fail(error)
  }
}

async function loadLamps() {
  const params = new URLSearchParams()
  if (lampFilter.keyword) params.set('keyword', lampFilter.keyword)
  if (lampFilter.status) params.set('status', lampFilter.status)
  if (lampFilter.circuit) params.set('circuit', lampFilter.circuit)
  const payload = await apiGet(`${ENDPOINT}?${params.toString()}`)
  lamps.value = payload.items ?? []
  lampTotal.value = payload.total ?? lamps.value.length
}

async function runLampAction(action: string, row: AnyRow) {
  errorMessage.value = ''
  const extra: AnyRow = {}
  if (action === '登记故障') {
    const reason = window.prompt('不亮原因：', row.不亮原因 ?? '')
    if (reason === null) return
    extra.不亮原因 = reason
    const reading = window.prompt('现场测光读数(lx，可留空)：', '')
    if (reading) extra.测光读数 = Number(reading)
  }
  try {
    const payload = await apiPost(`${ENDPOINT}/${row.id}/actions`, { values: { action, ...extra } })
    if (payload.ok === false) throw new Error(payload.message)
    await loadLamps()
  } catch (error) {
    fail(error)
  }
}

function exportLamps() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function loadRepairs() {
  const params = new URLSearchParams()
  if (summaryFilter.circuit) params.set('circuit', summaryFilter.circuit)
  if (summaryFilter.level) params.set('level', summaryFilter.level)
  repairs.value = (await apiGet(`${ENDPOINT}/repairs/all?${params.toString()}`)).items ?? []
}

async function loadCalendar() {
  const params = new URLSearchParams()
  if (summaryFilter.circuit) params.set('circuit', summaryFilter.circuit)
  calendarRows.value = (await apiGet(`${ENDPOINT}/calendar/all?${params.toString()}`)).items ?? []
}

async function loadRoutes() {
  const params = new URLSearchParams()
  if (summaryFilter.circuit) params.set('circuit', summaryFilter.circuit)
  routes.value = (await apiGet(`${ENDPOINT}/routes/all?${params.toString()}`)).items ?? []
}

onMounted(loadBoards)
</script>

<style scoped>
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 12px; }
.tab { border: none; background: none; padding: 8px 16px; cursor: pointer; font-size: 14px;
  color: var(--muted); border-bottom: 2px solid transparent; }
.tab.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.error-banner { background: #fef3f2; border: 1px solid #fecdca; color: #b42318;
  border-radius: 6px; padding: 8px 12px; font-size: 13px; }
.stage-pill { background: #eff4ff; color: #1d4ed8; border-radius: 10px; padding: 2px 10px; font-size: 12px; }
.mini-level { display: inline-block; background: #f1f5f9; border-radius: 4px; padding: 1px 6px;
  margin: 1px 3px 1px 0; font-size: 11px; white-space: nowrap; }
.conflict-flag { color: #b45309; font-weight: 600; font-size: 12px; cursor: help; }
.muted { color: var(--muted); }
.level-1 { color: #b42318; font-weight: 700; }
.level-2 { color: #b45309; font-weight: 600; }
.level-3 { color: #15803d; }
.drawer { margin-top: 14px; background: #fff; border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 16px; }
.drawer-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.stepper { display: flex; gap: 6px; list-style: none; padding: 0; margin: 0 0 12px; flex-wrap: wrap; }
.stepper li { flex: 1; min-width: 96px; text-align: center; padding: 6px 4px; font-size: 12px;
  background: #f1f5f9; color: var(--muted); border-radius: 6px; }
.stepper li.done { background: #dcfce7; color: #166534; }
.stepper li.current { background: #1f6feb; color: #fff; font-weight: 600; }
.verdict-box { background: #f8fafc; border: 1px dashed var(--border); border-radius: 6px;
  padding: 8px 12px; font-size: 13px; margin-bottom: 12px; }
.verdict-box p { margin: 4px 0; }
.conflict-text { color: #b45309; }
.action-box { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end;
  border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin-bottom: 12px; }
.action-box h4 { width: 100%; margin: 0; }
.action-box label { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); gap: 3px; }
.action-box label.wide { flex: 1; min-width: 260px; }
.action-box input, .action-box select { min-width: 150px; }
.drawer-grids { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.data-table.compound th, .data-table.compact td { padding: 5px 7px; font-size: 12px; }
.drawer-grids h4 { margin: 0 0 6px; font-size: 13px; }
.history-list { margin: 4px 0 0; padding-left: 16px; font-size: 12px; }
.filter-item select { padding: 4px 6px; }
</style>
