<template>
  <div>
    <div v-if="loading" class="hint">加载中…</div>
    <div v-else-if="error" class="hint err">{{ error }}</div>

    <div v-else-if="report">
      <div class="card head">
        <div class="head-top">
          <div>
            <h1 class="name">{{ report.meta.name }}</h1>
            <div class="sub">
              <span class="code">{{ report.meta.code }}</span>
              <span>{{ report.meta.report_type }}</span>
              <span>报告期 {{ report.meta.period }}</span>
              <span>披露日 {{ report.meta.disclosure_date }}</span>
              <span v-if="report.company?.industry">行业：{{ report.company.industry }}</span>
            </div>
            <a v-if="report.meta.source_url" :href="report.meta.source_url" target="_blank"
               rel="noopener" class="src">查看巨潮原文 →</a>
          </div>

          <!-- ---------- 操作区 ---------- -->
          <div class="actions">
            <template v-if="!editing">
              <button class="btn" :class="{ muted: !canEdit }" @click="onEditClick">修改</button>
              <button class="btn danger" :class="{ muted: !canDelete }" @click="onDeleteClick">删除</button>
            </template>
            <template v-else>
              <button class="btn primary" :disabled="saving" @click="save">
                {{ saving ? '保存中…' : '保存' }}
              </button>
              <button class="btn" :disabled="saving" @click="cancelEdit">取消</button>
            </template>
          </div>
        </div>

        <p v-if="editing" class="edit-note">
          编辑模式：结论与依据已变为文本框，评级已变为下拉选择框。来源链接、披露日期等取自原始公告，不可修改。
        </p>
        <p v-if="report.meta.last_edited_at && !editing" class="edited-note">
          本研报于 {{ fmt(report.meta.last_edited_at) }} 由 {{ report.meta.last_edited_by }} 人工修订过。
        </p>
      </div>

      <div class="dims">
        <section v-for="(d, di) in report.dims" :key="d.order" class="card dim">
          <div class="dim-head">
            <span class="dim-name">{{ d.dim_name }}</span>

            <!-- 评级：只读徽标 / 下拉选择 -->
            <span v-if="!editing" class="rating"
                  :style="{ background: ratingColor(d.rating) }">{{ d.rating }}</span>
            <select v-else v-model="d.rating" class="rating-select"
                    :style="{ background: ratingColor(d.rating) }">
              <option v-for="opt in RATINGS" :key="opt" :value="opt">{{ opt }}</option>
            </select>
          </div>

          <!-- 结论：文本 / 文本框 -->
          <p v-if="!editing" class="conclusion">{{ d.conclusion }}</p>
          <textarea v-else v-model="d.conclusion" class="ta conclusion-ta" rows="3"
                    placeholder="该维度的结论"></textarea>

          <!-- 依据：列表 / 文本框组 -->
          <ul v-if="!editing" class="evidence">
            <li v-for="(e, i) in d.evidence" :key="i">
              <b>{{ e.label }}</b>：{{ e.text }}
            </li>
          </ul>
          <div v-else class="evidence-edit">
            <div v-for="(e, i) in d.evidence" :key="i" class="ev-row">
              <input v-model="e.label" class="ta ev-label" placeholder="要点" />
              <textarea v-model="e.text" class="ta ev-text" rows="2" placeholder="依据内容"></textarea>
              <button class="mini danger" title="删除这一条" @click="removeEvidence(di, i)">×</button>
            </div>
            <button class="mini add" @click="addEvidence(di)">+ 添加一条依据</button>
          </div>
        </section>
      </div>
    </div>

    <!-- ---------- 删除确认框 ---------- -->
    <div v-if="confirmOpen" class="mask" @click.self="confirmOpen = false">
      <div class="dialog">
        <h3 class="dialog-title">确认删除这份研报？</h3>
        <p class="dialog-body">
          即将删除 <b>{{ report?.meta.name }}（{{ report?.meta.code }}）</b>
          的 <b>{{ report?.meta.period }}</b> 研报。
        </p>
        <p class="dialog-warn">
          ⚠️ 删除后公开页面将立即不可见。数据会留档（软删除），但需要管理员才能恢复。
        </p>
        <div class="dialog-actions">
          <button class="btn" @click="confirmOpen = false">取消</button>
          <button class="btn danger solid" :disabled="deleting" @click="doDelete">
            {{ deleting ? '删除中…' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ---------- 轻提示 ---------- -->
    <div v-if="toast.text" class="toast" :class="toast.type" @click="toast.text = ''">
      <span>{{ toast.text }}</span>
      <router-link v-if="toast.link" :to="toast.link.to" class="toast-link"
                   @click="toast.text = ''">{{ toast.link.label }}</router-link>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import { authHeaders, isLoggedIn } from '../stores/auth'

const route = useRoute()
const router = useRouter()

const report = ref(null)
const loading = ref(true)
const error = ref('')
const editing = ref(false)
const saving = ref(false)
const confirmOpen = ref(false)
const deleting = ref(false)

// 当前登录用户的权限
const perms = ref({ edit: false, delete: false })
const loggedIn = ref(isLoggedIn.value)

const toast = reactive({ text: '', type: 'ok', link: null })

const RATINGS = ['强', '较强', '中等偏强', '中性', '结构性分化', '偏负面']

const canEdit = computed(() => loggedIn.value && perms.value.edit)
const canDelete = computed(() => loggedIn.value && perms.value.delete)

function showToast(text, type = 'ok', link = null) {
  toast.text = text
  toast.type = type
  toast.link = link
  if (!link) setTimeout(() => { toast.text = '' }, 4000)
}

function ratingColor(rating) {
  const map = {
    '强': '#eaf3de',
    '较强': '#e6f1fb',
    '中等偏强': '#f1efe8',
    '中性': '#f1efe8',
    '结构性分化': '#faEeda',
    '偏负面': '#fcebeb',
  }
  return map[rating] || '#f1efe8'
}

function fmt(iso) {
  if (!iso) return ''
  return String(iso).replace('T', ' ').slice(0, 16)
}

// ---------- 加载 ----------
async function loadReport() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await axios.get(
      `/api/reports/${route.params.code}/${route.params.period}`)
    report.value = data
  } catch (e) {
    error.value = '研报不存在或未公开'
  } finally {
    loading.value = false
  }
}

async function loadPerms() {
  loggedIn.value = isLoggedIn.value
  if (!loggedIn.value) {
    perms.value = { edit: false, delete: false }
    return
  }
  try {
    const { data } = await axios.get('/api/me/profile', { headers: authHeaders() })
    const map = {}
    for (const p of data.permissions || []) map[p.code] = p.granted
    perms.value = { edit: !!map.edit, delete: !!map.delete }
  } catch {
    perms.value = { edit: false, delete: false }
  }
}

// ---------- 修改 ----------
function onEditClick() {
  if (!loggedIn.value) {
    showToast('请先登录后再修改研报', 'err', { to: '/auth', label: '去登录' })
    return
  }
  if (!perms.value.edit) {
    showToast('你没有「修改研报」权限，可在个人中心申请全部权限', 'err',
      { to: '/profile', label: '去申请' })
    return
  }
  // 深拷贝一份用于编辑，取消时可直接丢弃
  report.value.dims = JSON.parse(JSON.stringify(report.value.dims))
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  // 丢弃编辑中的副本，重新拉取以确保与服务器一致
  loadReport()
}

function addEvidence(di) {
  report.value.dims[di].evidence.push({ label: '', text: '' })
}

function removeEvidence(di, i) {
  report.value.dims[di].evidence.splice(i, 1)
}

async function save() {
  saving.value = true
  try {
    const payload = {
      dims: report.value.dims.map((d) => ({
        order: d.order,
        rating: d.rating,
        conclusion: d.conclusion || '',
        evidence: (d.evidence || [])
          .map((e) => ({ label: e.label || '', text: e.text || '' }))
          .filter((e) => e.label || e.text),
      })),
    }
    const { data } = await axios.put(
      `/api/reports/${route.params.code}/${route.params.period}`,
      payload, { headers: authHeaders() })
    editing.value = false
    await loadReport()
    showToast(data.message || '已保存')
  } catch (e) {
    showToast(e.response?.data?.detail || '保存失败', 'err')
  } finally {
    saving.value = false
  }
}

// ---------- 删除 ----------
function onDeleteClick() {
  if (!loggedIn.value) {
    showToast('请先登录后再删除研报', 'err', { to: '/auth', label: '去登录' })
    return
  }
  if (!perms.value.delete) {
    showToast('你没有「删除研报」权限，可在个人中心申请全部权限', 'err',
      { to: '/profile', label: '去申请' })
    return
  }
  confirmOpen.value = true
}

async function doDelete() {
  deleting.value = true
  try {
    const { data } = await axios.delete(
      `/api/reports/${route.params.code}/${route.params.period}`,
      { headers: authHeaders() })
    confirmOpen.value = false
    showToast(data.message || '已删除')
    setTimeout(() => router.push('/'), 1200)
  } catch (e) {
    confirmOpen.value = false
    showToast(e.response?.data?.detail || '删除失败', 'err')
  } finally {
    deleting.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadReport(), loadPerms()])
})
</script>

<style scoped>
.hint { color: var(--muted); padding: 40px 0; text-align: center; }
.hint.err { color: #a32d2d; }

.head { margin-bottom: 20px; }
.head-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.name { font-size: 24px; margin-bottom: 8px; }
.sub { display: flex; flex-wrap: wrap; gap: 14px; color: var(--muted); font-size: 13px; margin-bottom: 10px; }
.code { color: var(--brand); font-weight: 600; }
.src { color: var(--brand); text-decoration: none; font-size: 13px; }

.actions { display: flex; gap: 8px; flex: none; }
.btn {
  padding: 7px 16px; font-size: 13px; border-radius: 7px; cursor: pointer;
  border: 1px solid var(--line); background: #fff; color: var(--text);
}
.btn:hover:not(:disabled) { border-color: var(--brand); color: var(--brand); }
.btn.primary { background: var(--brand); color: #fff; border-color: var(--brand); }
.btn.primary:hover:not(:disabled) { opacity: .9; color: #fff; }
.btn.danger { color: #a32d2d; border-color: #f0c9c9; }
.btn.danger:hover:not(:disabled) { background: #fcebeb; border-color: #e0a5a5; color: #a32d2d; }
.btn.danger.solid { background: #a32d2d; color: #fff; border-color: #a32d2d; }
.btn.danger.solid:hover:not(:disabled) { background: #8e2626; color: #fff; }
.btn:disabled { opacity: .55; cursor: not-allowed; }
/* 无权限时按钮变淡，但仍可点击（点击会给出提示） */
.btn.muted { color: var(--muted); border-color: var(--line); }

.edit-note {
  font-size: 12px; color: #6b4406; background: #faeeda;
  padding: 8px 12px; border-radius: 7px; margin-top: 12px; line-height: 1.7;
}
.edited-note { font-size: 12px; color: var(--muted); margin-top: 8px; }

.dims { display: flex; flex-direction: column; gap: 16px; }
.dim-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 12px; }
.dim-name { font-size: 17px; font-weight: 600; }
.rating { font-size: 13px; padding: 3px 14px; border-radius: 14px; border: 1px solid var(--line); }
.rating-select {
  font-size: 13px; padding: 4px 10px; border-radius: 14px;
  border: 1px solid var(--brand); outline: none; cursor: pointer; font-family: inherit;
}
.conclusion { font-size: 14px; margin-bottom: 10px; }
.conclusion-ta { width: 100%; margin-bottom: 10px; }

.evidence { list-style: none; }
.evidence li { font-size: 13px; color: #4b5563; padding: 5px 0; border-top: 1px dashed var(--line); }
.evidence li:first-child { border-top: none; }
.evidence b { color: var(--text); }

.ta {
  font-family: inherit; font-size: 13px; line-height: 1.7; color: var(--text);
  padding: 8px 11px; border: 1px solid var(--line); border-radius: 7px;
  outline: none; resize: vertical; width: 100%;
}
.ta:focus { border-color: var(--brand); }
.evidence-edit { display: flex; flex-direction: column; gap: 8px; }
.ev-row { display: flex; gap: 8px; align-items: flex-start; }
.ev-label { width: 110px; flex: none; }
.ev-text { flex: 1; }

.mini {
  border: 1px solid var(--line); background: #fff; border-radius: 6px;
  cursor: pointer; font-size: 12px; padding: 6px 10px; color: var(--muted);
}
.mini.danger { flex: none; color: #a32d2d; width: 30px; padding: 6px 0; }
.mini.danger:hover { background: #fcebeb; }
.mini.add { align-self: flex-start; color: var(--brand); border-color: #c6ddf1; }

/* ---------- 确认框 ---------- */
.mask {
  position: fixed; inset: 0; background: rgba(31, 35, 41, .45);
  display: flex; align-items: center; justify-content: center; z-index: 60;
}
.dialog {
  background: #fff; border-radius: 12px; padding: 24px 26px;
  max-width: 400px; width: calc(100% - 40px);
  box-shadow: 0 12px 40px rgba(0, 0, 0, .18);
}
.dialog-title { font-size: 17px; margin-bottom: 12px; }
.dialog-body { font-size: 14px; color: #4b5563; line-height: 1.75; }
.dialog-warn {
  font-size: 12px; color: #6b4406; background: #faeeda;
  padding: 9px 12px; border-radius: 7px; margin-top: 12px; line-height: 1.7;
}
.dialog-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }

/* ---------- 轻提示 ---------- */
.toast {
  position: fixed; left: 50%; bottom: 32px; transform: translateX(-50%);
  background: #1f2329; color: #fff; font-size: 13px; padding: 10px 16px;
  border-radius: 8px; z-index: 70; display: flex; align-items: center; gap: 12px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, .2); max-width: calc(100% - 40px);
}
.toast.err { background: #8e2626; }
.toast-link { color: #bcd9f5; text-decoration: underline; white-space: nowrap; }
</style>
