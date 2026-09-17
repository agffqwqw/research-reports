<template>
  <div class="auth-wrap">
    <div class="card auth-card">
      <div class="tabs">
        <button :class="{ on: mode === 'login' }" @click="mode = 'login'">登录</button>
        <button :class="{ on: mode === 'register' }" @click="mode = 'register'">注册</button>
      </div>

      <!-- 登录 -->
      <template v-if="mode === 'login'">
        <label>邮箱</label>
        <input v-model="form.email" type="email" placeholder="you@example.com" @keyup.enter="doLogin" />

        <label>密码</label>
        <input v-model="form.password" type="password" placeholder="至少 8 位" @keyup.enter="doLogin" />

        <label>验证码</label>
        <div class="captcha-row">
          <input v-model="form.captcha" placeholder="4 位字符" maxlength="4" @keyup.enter="doLogin" />
          <div class="captcha-img" v-html="captchaSvg" @click="loadCaptcha" title="点击换一张"></div>
        </div>

        <button class="primary" :disabled="busy" @click="doLogin">
          {{ busy ? '登录中…' : '登录' }}
        </button>
      </template>

      <!-- 注册 -->
      <template v-else>
        <label>邮箱</label>
        <input v-model="form.email" type="email" placeholder="you@example.com" @keyup.enter="doRegister" />

        <label>密码</label>
        <input v-model="form.password" type="password" placeholder="至少 8 位" @keyup.enter="doRegister" />

        <label>验证码</label>
        <div class="captcha-row">
          <input v-model="form.captcha" placeholder="4 位字符" maxlength="4" @keyup.enter="doRegister" />
          <div class="captcha-img" v-html="captchaSvg" @click="loadCaptcha" title="点击换一张"></div>
        </div>

        <p class="tip">注册后自动登录，并获得 2 次生成机会。</p>

        <button class="primary" :disabled="busy" @click="doRegister">
          {{ busy ? '提交中…' : '注册' }}
        </button>
      </template>

      <p v-if="msg" class="msg" :class="msgType">{{ msg }}</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { setAuth } from '../stores/auth'

const router = useRouter()
const mode = ref('login')
const busy = ref(false)
const msg = ref('')
const msgType = ref('ok')
const captchaSvg = ref('')
const form = ref({ email: '', password: '', captcha: '', captchaToken: '' })

async function loadCaptcha() {
  try {
    const { data } = await axios.get('/api/auth/captcha')
    captchaSvg.value = data.svg
    form.value.captchaToken = data.token
    form.value.captcha = ''
  } catch {
    captchaSvg.value = ''
  }
}

function say(text, type = 'ok') {
  msg.value = text
  msgType.value = type
}

async function doLogin() {
  if (!form.value.email || !form.value.password || !form.value.captcha) {
    return say('请填写邮箱、密码和验证码', 'err')
  }
  busy.value = true
  try {
    const { data } = await axios.post('/api/auth/login', {
      email: form.value.email,
      password: form.value.password,
      captcha_token: form.value.captchaToken,
      captcha: form.value.captcha,
    })
    setAuth(data.access_token, data.email, data.role)
    say('登录成功，正在跳转…')
    setTimeout(() => router.push('/'), 500)
  } catch (e) {
    say(e.response?.data?.detail || '登录失败', 'err')
    loadCaptcha()
  } finally {
    busy.value = false
  }
}

async function doRegister() {
  if (!form.value.email || !form.value.password) return say('请填写邮箱和密码', 'err')
  if (form.value.password.length < 8) return say('密码至少 8 位', 'err')
  if (!form.value.captcha) return say('请填写验证码', 'err')

  busy.value = true
  try {
    const { data } = await axios.post('/api/auth/register', {
      email: form.value.email,
      password: form.value.password,
      captcha_token: form.value.captchaToken,
      captcha: form.value.captcha,
    })

    // 注册即登录：后端直接返回令牌，这里落库并跳转。
    // 早期版本只显示「注册成功」却停在注册表单，用户容易以为注册失败。
    if (data.access_token) {
      setAuth(data.access_token, data.email, data.role)
      say(data.hint || '注册成功，正在跳转…')
      setTimeout(() => router.push('/'), 800)
      return
    }

    // 兜底：万一后端没返回令牌，切到登录页签让用户手动登录
    say(data.hint || '注册成功，请登录', 'ok')
    mode.value = 'login'
    loadCaptcha()
    form.value.captcha = ''
  } catch (e) {
    say(e.response?.data?.detail || '注册失败', 'err')
    loadCaptcha()
  } finally {
    busy.value = false
  }
}

// 切换登录/注册时刷新验证码，避免复用已过期的 token
watch(mode, () => {
  msg.value = ''
  loadCaptcha()
})

onMounted(loadCaptcha)
</script>

<style scoped>
.auth-wrap { display: flex; justify-content: center; padding-top: 24px; }
.auth-card { width: 380px; }
.tabs { display: flex; gap: 8px; margin-bottom: 20px; }
.tabs button {
  flex: 1; padding: 8px 0; cursor: pointer; background: #fff;
  border: 1px solid var(--line); border-radius: 8px; color: var(--muted); font-size: 14px;
}
.tabs button.on { border-color: var(--brand); color: var(--brand); font-weight: 600; }

label { display: block; font-size: 13px; color: var(--muted); margin: 12px 0 6px; }
input {
  width: 100%; padding: 10px 12px; font-size: 14px;
  border: 1px solid var(--line); border-radius: 8px; outline: none;
}
input:focus { border-color: var(--brand); }

.captcha-row { display: flex; gap: 10px; align-items: center; }
.captcha-row input { flex: 1; }
.captcha-img { cursor: pointer; line-height: 0; border-radius: 6px; overflow: hidden; }

.primary {
  width: 100%; margin-top: 20px; padding: 11px 0; cursor: pointer;
  background: var(--brand); color: #fff; border: none; border-radius: 8px; font-size: 15px;
}
.primary:disabled { opacity: 0.6; cursor: not-allowed; }

.tip { font-size: 12px; color: var(--muted); margin-top: 12px; line-height: 1.6; }
.msg { margin-top: 14px; font-size: 13px; padding: 10px 12px; border-radius: 8px; }
.msg.ok { background: #eaf3de; color: #27500a; }
.msg.err { background: #fcebeb; color: #791f1f; }
</style>
