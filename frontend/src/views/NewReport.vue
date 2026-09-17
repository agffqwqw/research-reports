<template>
  <div>
    <h1 class="page-title">新增研报</h1>

    <!-- ---------- 未登录 ---------- -->
    <div v-if="!loggedIn" class="card empty">
      <p>提交生成需要先登录账号。</p>
      <router-link to="/auth" class="btn primary">去登录 / 注册</router-link>
    </div>

    <template v-else>
      <!-- ---------- 提交表单 ---------- -->
      <section class="card block">
        <h2 class="block-title">提交生成任务</h2>
        <div class="gen-row">
          <input
            v-model="query"
            type="text"
            placeholder="输入 A 股股票代码或公司名称，如 300308 / 中际旭创"
            @keyup.enter="submit"
          />
          <button class="btn primary" :disabled="busy" @click="submit">
            {{ busy ? '提交中…' : '提交生成' }}
          </button>
        </div>
        <p class="tip">
          生成由本机在后台完成，通常需要 <b>2–3 分钟</b>。
          提交后可在下方任务列表查看进度，<b>完成后会出现「查看研报」链接</b>。
        </p>
        <p class="tip muted">
          若该公司最新一期报告已生成过，会直接复用，不消耗生成次数。
        </p>
        <p v-if="msg" class="msg" :class="msgType">{{ msg }}</p>
      </section>

      <!-- ---------- 任务列表 ---------- -->
      <section class="card block">
        <h2 class="block-title">
          我的任务
          <span v-if="runningCount" class="poll-badge">
            有 {{ runningCount }} 个进行中 · 自动刷新中
          </span>
        </h2>

        <div v-if="loadingTasks" class="hint">加载中…</div>
        <div v-else-if="!tasks.length" class="hint">还没有提交过任务</div>

        <table v-else class="tasks">
          <thead>
            <tr>
              <th>任务</th>
              <th>公司</th>
              <th>状态</th>
              <th>提交 / 完成</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in tasks" :key="t.id">
              <td class="mono">#{{ t.id }}</td>
              <td>
                <b>{{ t.company_name || '—' }}</b>
                <span class="code">{{ t.company_code }}</span>
              </td>
              <td>
                <span class="status" :class="t.status">{{ statusLabel(t.status) }}</span>
              </td>
              <td class="time">
                {{ fmt(t.created_at) }}
                <template v-if="t.finished_at"><br>{{ fmt(t.finished_at) }}</template>
              </td>
              <td>
                <router-link
                  v-if="t.has_report"
                  :to="`/report/${t.company_code}/${t.period}`"
                  class="link"
                >查看研报 →</router-link>
                <span v-else-if="t.status === 'failed'" class="err" :title="t.error_msg || ''">
                  {{ shortErr(t.error_msg) }}
                </span>
                <span v-else-if="t.status === 'processing'" class="muted">生成中…</span>
                <span v-else class="muted">排队中</span>
              </td>
            </tr>
          </tbody>
        </table>

        <p v-if="tasks.length" class="tip muted refresh-note">
          页面每 10 秒自动刷新一次（仅在有待处理任务时）。
          <button class="link-btn" @click="loadTasks">立即刷新</button>
        </p>
      </section>
    </template>

    <!-- ---------- 轻提示 ---------- -->
    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import axios from 'axios'
import { authHeaders, isLoggedIn } from '../stores/auth'

const loggedIn = ref(isLoggedIn.value)

// ---------- 提交 ----------
const query = ref('')
const busy = ref(false)
const msg = ref('')
const msgType = ref('ok')

// ---------- 任务列表 ----------
const tasks = ref([])
const loadingTasks = ref(false)
const toast = ref('')

let timer = null
// 记录上一次看到的成功任务，用于「刚出结果」的提示
let knownSuccess = new Set()

const runningCount = computed(
  () => tasks.value.filter((t) => t.status === 'pending' || t.status === 'processing').length)

const STATUS_LABEL = {
  pending: '待处理',
  processing: '生成中',
  success: '已完成',
  failed: '失败',
}
const statusLabel = (s) => STATUS_LABEL[s] || s || '—'

function fmt(iso) {
  if (!iso) return '—'
  return String(iso).replace('T', ' ').slice(5, 16)  // MM-DD HH:MM
}

function shortErr(e) {
  if (!e) return '失败'
  return e.length > 24 ? e.slice(0, 24) + '…' : e
}

async function loadTasks(silent = false) {
  if (!loggedIn.value) return
  if (!silent) loadingTasks.value = true
  try {
    const { data } = await axios.get('/api/tasks', { headers: authHeaders() })
    const items = data.items || []

    // 判断是否有「新完成」的任务 —— 这正是用户之前不知道出结果的痛点
    const nowSuccess = new Set(items.filter((t) => t.status === 'success').map((t) => t.id))
    if (knownSuccess.size) {
      for (const t of items) {
        if (t.status === 'success' && !knownSuccess.has(t.id)) {
          showToast(`${t.company_name || t.company_code} 的研报已生成，可点「查看研报」`)
          break
        }
      }
    }
    knownSuccess = nowSuccess

    tasks.value = items
    if (runningCount.value === 0) stopPoll()
  } catch {
    tasks.value = []
  } finally {
    loadingTasks.value = false
  }
}

function startPoll() {
  if (timer) return
  timer = setInterval(() => loadTasks(true), 10000)
}
function stopPoll() {
  if (timer) { clearInterval(timer); timer = null }
}

function showToast(text) {
  toast.value = text
  setTimeout(() => { toast.value = '' }, 6000)
}

async function submit() {
  const q = query.value.trim()
  if (!q) {
    msgType.value = 'err'
    msg.value = '请输入股票代码或公司名称'
    return
  }
  busy.value = true
  msg.value = ''
  try {
    const { data } = await axios.post('/api/tasks',
      { query: q, quality: 'deep' }, { headers: authHeaders() })
    msgType.value = 'ok'
    if (data.reused) {
      msg.value = `${data.company_name} 已有研报（${data.period}），已直接复用，未消耗生成次数`
    } else {
      msg.value = `任务已提交（编号 ${data.task_id}，${data.company_name}），`
        + `预计 2–3 分钟完成，下方列表会自动刷新`
      // 预先把新任务 id 记入已知集合，避免它完成时把「提交」当成「新完成」而误报
      knownSuccess.add(-data.task_id)
    }
    query.value = ''
    await loadTasks(true)
    startPoll()
  } catch (e) {
    msgType.value = 'err'
    msg.value = e.response?.data?.detail || '提交失败'
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  if (!loggedIn.value) return
  await loadTasks()
  if (runningCount.value > 0) startPoll()
})

onUnmounted(stopPoll)
</script>

<style scoped>
.page-title { font-size: 22px; margin-bottom: 16px; }
.block { margin-bottom: 16px; }
.block-title {
  font-size: 16px; font-weight: 600; margin-bottom: 14px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}
.poll-badge {
  font-size: 11px; font-weight: 400; color: var(--brand);
  background: #e6f1fb; border-radius: 10px; padding: 2px 9px;
}

.gen-row { display: flex; gap: 8px; }
.gen-row input {
  flex: 1; padding: 10px 14px; font-size: 14px;
  border: 1px solid var(--line); border-radius: 8px; outline: none;
}
.gen-row input:focus { border-color: var(--brand); }

.btn {
  padding: 9px 18px; font-size: 13px; border-radius: 8px;
  cursor: pointer; border: 1px solid var(--line); background: #fff;
  text-decoration: none; color: var(--text); display: inline-block;
}
.btn.primary { background: var(--brand); color: #fff; border-color: var(--brand); }
.btn:disabled { opacity: .6; cursor: not-allowed; }

.tip { font-size: 12px; color: var(--muted); line-height: 1.75; margin-top: 10px; }
.tip.muted { color: #a8aeb8; }
.refresh-note { display: flex; align-items: center; gap: 8px; }

.msg { font-size: 13px; margin-top: 12px; padding: 9px 13px; border-radius: 8px; }
.msg.ok { background: #eaf3de; color: #27500a; }
.msg.err { background: #fcebeb; color: #791f1f; }

.tasks { width: 100%; border-collapse: collapse; font-size: 13px; }
.tasks th, .tasks td {
  text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line);
  vertical-align: top;
}
.tasks th { color: var(--muted); font-weight: 400; font-size: 12px; }
.tasks tbody tr:last-child td { border-bottom: none; }
.mono { font-family: Consolas, monospace; color: var(--muted); }
.code { color: var(--muted); margin-left: 6px; font-size: 12px; }
.time { color: var(--muted); font-size: 12px; line-height: 1.6; white-space: nowrap; }
.muted { color: var(--muted); }
.err { color: #a32d2d; font-size: 12px; }
.link { color: var(--brand); text-decoration: none; white-space: nowrap; }
.link:hover { text-decoration: underline; }
.link-btn {
  background: none; border: none; padding: 0; cursor: pointer;
  color: var(--brand); font-size: 12px; text-decoration: underline;
}

.status { font-size: 12px; padding: 2px 9px; border-radius: 10px; white-space: nowrap; }
.status.pending { background: #f1efe8; color: #6b6250; }
.status.processing { background: #e6f1fb; color: #12457a; }
.status.success { background: #eaf3de; color: #27500a; }
.status.failed { background: #fcebeb; color: #791f1f; }

.empty { text-align: center; padding: 32px 20px; }
.empty p { color: var(--muted); margin-bottom: 14px; }
.hint { color: var(--muted); padding: 24px 0; text-align: center; }

.toast {
  position: fixed; left: 50%; bottom: 32px; transform: translateX(-50%);
  background: #1f2329; color: #fff; font-size: 13px; padding: 11px 18px;
  border-radius: 8px; z-index: 70; box-shadow: 0 6px 24px rgba(0,0,0,.2);
  max-width: calc(100% - 40px);
}
</style>
