<template>
  <div class="layout">
    <header class="topbar">
      <router-link to="/" class="brand">上市公司研报</router-link>
      <span class="tagline">基于巨潮资讯网公开信息 · AI 生成</span>
      <span class="spacer"></span>
      <nav class="topnav">
        <router-link to="/" class="nav-link">列表</router-link>
        <router-link to="/new" class="nav-link">新增研报</router-link>
      </nav>
      <template v-if="isLoggedIn">
        <router-link to="/profile" class="nav-link">个人中心</router-link>
        <span class="who">{{ authState.email }}</span>
        <button class="link" @click="logout">退出</button>
      </template>
      <router-link v-else to="/auth" class="login-link">登录 / 注册</router-link>
    </header>

    <main class="content">
      <router-view />
    </main>

    <footer class="footer">
      <p class="disclaimer">
        免责声明：本站所有研报内容由人工智能基于巨潮资讯网公开信息自动生成，仅供研究参考，不构成任何投资建议。
        本站不对内容的准确性、完整性作出保证，亦不对因使用本站内容而产生的任何损失承担责任。
        关于企业性质（国企 / 央企 / 民营）的说明由 AI 依据公开信息判断，可能失效，请以官方披露为准。
      </p>
    </footer>
  </div>
</template>

<script setup>
import { useRouter } from 'vue-router'
import { authState, clearAuth, isLoggedIn } from './stores/auth'

const router = useRouter()

function logout() {
  clearAuth()
  router.push('/')
}
</script>

<style>
:root {
  --bg: #f7f8fa;
  --card: #ffffff;
  --text: #1f2329;
  --muted: #8a919f;
  --line: #e5e7eb;
  --brand: #185fa5;
  /* 六档评级色（浅底 + 深字） */
  --r-strong: #eaf3de;
  --r-good: #e6f1fb;
  --r-mid-plus: #f1efe8;
  --r-mid: #f1efe8;
  --r-mixed: #faEeda;
  --r-weak: #fcebeb;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  font-size: 14px;
}

.layout { max-width: 960px; margin: 0 auto; padding: 0 16px; }

.topbar {
  display: flex; align-items: baseline; gap: 12px;
  padding: 20px 0; border-bottom: 1px solid var(--line);
}
.brand { font-size: 20px; font-weight: 600; color: var(--brand); text-decoration: none; }
.tagline { color: var(--muted); font-size: 13px; }
.spacer { flex: 1; }
.topnav { display: flex; align-items: baseline; gap: 16px; }
.nav-link { color: var(--text); font-size: 13px; text-decoration: none; }
.nav-link:hover { color: var(--brand); }
.who { color: var(--muted); font-size: 13px; }
.link {
  background: none; border: none; cursor: pointer;
  color: var(--brand); font-size: 13px; padding: 0 0 0 12px;
}
.login-link { color: var(--brand); font-size: 13px; text-decoration: none; }

.content { padding: 24px 0; min-height: 60vh; }

.footer { border-top: 1px solid var(--line); padding: 16px 0 32px; }
.disclaimer { color: var(--muted); font-size: 12px; line-height: 1.7; }

.card {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px 20px;
}
</style>
