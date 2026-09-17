// 极简登录状态管理：不用 Pinia，一个 reactive 对象 + localStorage 足够
import { computed, reactive } from 'vue'

const TOKEN_KEY = 'rr_token'
const EMAIL_KEY = 'rr_email'
const ROLE_KEY = 'rr_role'

const state = reactive({
  token: localStorage.getItem(TOKEN_KEY) || '',
  email: localStorage.getItem(EMAIL_KEY) || '',
  role: localStorage.getItem(ROLE_KEY) || '',
})

export function setAuth(token, email, role) {
  state.token = token
  state.email = email
  state.role = role
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(EMAIL_KEY, email)
  localStorage.setItem(ROLE_KEY, role)
}

export function clearAuth() {
  state.token = ''
  state.email = ''
  state.role = ''
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(EMAIL_KEY)
  localStorage.removeItem(ROLE_KEY)
}

export function authHeaders() {
  return state.token ? { Authorization: `Bearer ${state.token}` } : {}
}

export const isLoggedIn = computed(() => !!state.token)
export const authState = state
