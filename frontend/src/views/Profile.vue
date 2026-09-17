<template>
  <div>
    <div v-if="loading" class="hint">加载中…</div>
    <div v-else-if="error" class="hint err">{{ error }}</div>

    <template v-else-if="profile">
      <h1 class="page-title">个人中心</h1>

      <!-- ---------- 账号信息 ---------- -->
      <section class="card block">
        <h2 class="block-title">账号信息</h2>
        <dl class="kv">
          <div class="kv-row">
            <dt>账号（邮箱）</dt>
            <dd>{{ profile.account.email }}</dd>
          </div>
          <div class="kv-row">
            <dt>角色</dt>
            <dd>{{ profile.account.role === 'admin' ? '管理员' : '普通用户' }}</dd>
          </div>
          <div class="kv-row">
            <dt>注册时间</dt>
            <dd>{{ fmt(profile.account.created_at) }}</dd>
          </div>
          <div class="kv-row">
            <dt>上次登录</dt>
            <dd>{{ fmt(profile.account.last_login_at) || '—' }}</dd>
          </div>
          <div class="kv-row">
            <dt>剩余生成次数</dt>
            <dd>
              <b v-if="profile.quota.unlimited">无限次</b>
              <b v-else>{{ profile.quota.left }} 次</b>
            </dd>
          </div>
          <div class="kv-row">
            <dt>我的生成任务</dt>
            <dd>
              共 {{ profile.task_stats.total }} 个（成功 {{ profile.task_stats.success }} /
              失败 {{ profile.task_stats.failed }} / 进行中 {{ profile.task_stats.running }}）
            </dd>
          </div>
        </dl>
      </section>

      <!-- ---------- 已有权限 ---------- -->
      <section class="card block">
        <h2 class="block-title">
          已有权限
          <span class="count">{{ profile.granted_count }} / {{ profile.total_permissions }}</span>
        </h2>

        <ul class="perm-list">
          <li v-for="p in profile.permissions" :key="p.code" class="perm"
              :class="{ granted: p.granted }">
            <span class="mark">{{ p.granted ? '✓' : '○' }}</span>
            <span class="perm-label">{{ p.label }}</span>
            <span class="perm-state">{{ p.granted ? '已获得' : '未获得' }}</span>
          </li>
        </ul>

        <div v-if="profile.pending_request" class="notice warn-box">
          已有一条待处理的申请（{{ scopeLabel(profile.pending_request.scope) }}），
          提交于 {{ fmt(profile.pending_request.created_at) }}，请等待管理员批准。
        </div>

        <div v-else-if="profile.granted_count >= profile.total_permissions" class="notice ok-box">
          你已拥有全部权限。
        </div>

        <template v-else>
          <button class="btn primary" :disabled="applying" @click="applyAll">
            {{ applying ? '提交中…' : '申请全部权限' }}
          </button>
          <p class="tip">
            申请后会向管理员发送一封邮件，管理员点击邮件里的链接即可批准。
            批准后你将获得：无限次生成、修改研报、删除研报。
          </p>
          <p v-if="applyMsg" class="msg" :class="applyMsgType">{{ applyMsg }}</p>
        </template>
      </section>

      <!-- ---------- 修改密码 ---------- -->
      <section class="card block">
        <h2 class="block-title">修改密码</h2>
        <p class="tip">
          为确认是你本人操作，提交后会向
          <b>{{ profile.account.email }}</b>
          发送一封确认邮件。<b>点击邮件里的链接后</b>新密码才会生效（30 分钟内有效）。
        </p>

        <div class="pwd-form">
          <label>
            新密码
            <input v-model="pwd.new" type="password" placeholder="至少 8 位" autocomplete="new-password" />
          </label>
          <label>
            确认新密码
            <input v-model="pwd.confirm" type="password" placeholder="再输入一次" autocomplete="new-password" />
          </label>
          <button class="btn primary" :disabled="pwdBusy" @click="submitPassword">
            {{ pwdBusy ? '提交中…' : '提交修改密码' }}
          </button>
        </div>
        <p v-if="pwdMsg" class="msg" :class="pwdMsgType">{{ pwdMsg }}</p>
      </section>

      <!-- ---------- 申请记录 ---------- -->
      <section v-if="profile.history && profile.history.length" class="card block">
        <h2 class="block-title">申请记录</h2>
        <table class="hist">
          <thead>
            <tr><th>范围</th><th>状态</th><th>提交时间</th><th>处理时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="h in profile.history" :key="h.id">
              <td>{{ scopeLabel(h.scope) }}</td>
              <td>
                <span class="badge" :class="h.status === 'approved' ? 'ok' : 'warn'">
                  {{ statusLabel(h.status) }}
                </span>
              </td>
              <td>{{ fmt(h.created_at) }}</td>
              <td>{{ fmt(h.decided_at) || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import axios from 'axios'
import { authHeaders, isLoggedIn } from '../stores/auth'

const profile = ref(null)
const loading = ref(true)
const error = ref('')

const applying = ref(false)
const applyMsg = ref('')
const applyMsgType = ref('ok')

const pwd = reactive({ new: '', confirm: '' })
const pwdBusy = ref(false)
const pwdMsg = ref('')
const pwdMsgType = ref('ok')

const SCOPE_LABEL = {
  unlimited: '无限次生成',
  edit: '修改研报',
  delete: '删除研报',
  all: '全部权限',
}
const STATUS_LABEL = { pending: '待处理', approved: '已批准', rejected: '已拒绝' }

const scopeLabel = (s) => SCOPE_LABEL[s] || s || '—'
const statusLabel = (s) => STATUS_LABEL[s] || s || '—'

function fmt(iso) {
  if (!iso) return ''
  // 后端存的是带时区的 ISO，这里只取到分钟，便于阅读
  return String(iso).replace('T', ' ').slice(0, 16)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await axios.get('/api/me/profile', { headers: authHeaders() })
    profile.value = data
  } catch (e) {
    if (e.response?.status === 401) {
      error.value = '请先登录后查看个人中心'
    } else {
      error.value = e.response?.data?.detail || '加载失败'
    }
  } finally {
    loading.value = false
  }
}

async function applyAll() {
  applying.value = true
  applyMsg.value = ''
  try {
    const { data } = await axios.post('/api/permissions/request-all',
      { reason: '' }, { headers: authHeaders() })
    applyMsgType.value = data.ok ? 'ok' : 'err'
    applyMsg.value = data.message || '已提交'
    await load()
  } catch (e) {
    applyMsgType.value = 'err'
    applyMsg.value = e.response?.data?.detail || '提交失败'
  } finally {
    applying.value = false
  }
}

async function submitPassword() {
  pwdMsg.value = ''
  if (!pwd.new || !pwd.confirm) {
    pwdMsgType.value = 'err'
    pwdMsg.value = '请填写两次密码'
    return
  }
  if (pwd.new !== pwd.confirm) {
    pwdMsgType.value = 'err'
    pwdMsg.value = '两次输入的密码不一致'
    return
  }
  if (pwd.new.length < 8) {
    pwdMsgType.value = 'err'
    pwdMsg.value = '密码至少 8 位'
    return
  }
  pwdBusy.value = true
  try {
    const { data } = await axios.post('/api/me/password/request',
      { new_password: pwd.new, confirm_password: pwd.confirm },
      { headers: authHeaders() })
    pwdMsgType.value = data.ok ? 'ok' : 'err'
    pwdMsg.value = data.message || ''
    if (data.ok) {
      pwd.new = ''
      pwd.confirm = ''
    }
  } catch (e) {
    pwdMsgType.value = 'err'
    pwdMsg.value = e.response?.data?.detail || '提交失败'
  } finally {
    pwdBusy.value = false
  }
}

onMounted(() => {
  if (!isLoggedIn.value) {
    loading.value = false
    error.value = '请先登录后查看个人中心'
    return
  }
  load()
})
</script>

<style scoped>
.page-title { font-size: 22px; margin-bottom: 16px; }

.block { margin-bottom: 16px; }
.block-title {
  font-size: 16px; font-weight: 600; margin-bottom: 14px;
  display: flex; align-items: center; gap: 10px;
}
.count {
  font-size: 12px; font-weight: 400; color: var(--brand);
  border: 1px solid var(--brand); border-radius: 10px; padding: 1px 9px;
}

.kv { display: flex; flex-direction: column; gap: 9px; }
.kv-row { display: flex; gap: 12px; font-size: 13px; }
.kv-row dt { width: 110px; flex: none; color: var(--muted); }
.kv-row dd { flex: 1; }

.badge {
  font-size: 11px; padding: 1px 8px; border-radius: 10px;
  border: 1px solid var(--line); margin-left: 6px;
}
.badge.ok { background: #eaf3de; color: #27500a; border-color: #cfe3b4; }
.badge.warn { background: #faeeda; color: #6b4406; border-color: #f0d9ae; }

.perm-list { list-style: none; display: flex; flex-direction: column; gap: 8px; }
.perm {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 12px; border: 1px solid var(--line); border-radius: 8px;
  font-size: 13px; background: #fcfcfd;
}
.perm.granted { background: #f7fbf3; border-color: #dbeac9; }
.perm .mark { width: 16px; color: var(--muted); }
.perm.granted .mark { color: #3b6d11; font-weight: 700; }
.perm-label { flex: 1; }
.perm-state { font-size: 12px; color: var(--muted); }

.btn {
  padding: 9px 18px; font-size: 13px; border-radius: 8px;
  cursor: pointer; border: 1px solid var(--line); background: #fff;
}
.btn.primary { background: var(--brand); color: #fff; border-color: var(--brand); }
.btn:disabled { opacity: .6; cursor: not-allowed; }

.tip { font-size: 12px; color: var(--muted); line-height: 1.75; margin-top: 10px; }
.notice { font-size: 13px; padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; }
.warn-box { background: #faeeda; color: #6b4406; }
.ok-box { background: #eaf3de; color: #27500a; }

.pwd-form { display: flex; flex-direction: column; gap: 12px; max-width: 320px; }
.pwd-form label { font-size: 13px; color: var(--muted); display: flex; flex-direction: column; gap: 5px; }
.pwd-form input {
  padding: 9px 12px; font-size: 14px; border: 1px solid var(--line);
  border-radius: 8px; outline: none; color: var(--text);
}
.pwd-form input:focus { border-color: var(--brand); }
.pwd-form .btn { align-self: flex-start; }

.msg { font-size: 13px; margin-top: 12px; padding: 9px 13px; border-radius: 8px; }
.msg.ok { background: #eaf3de; color: #27500a; }
.msg.err { background: #fcebeb; color: #791f1f; }

.hist { width: 100%; border-collapse: collapse; font-size: 13px; }
.hist th, .hist td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }
.hist th { color: var(--muted); font-weight: 400; font-size: 12px; }

.hint { color: var(--muted); padding: 40px 0; text-align: center; }
.hint.err { color: #a32d2d; }
</style>
