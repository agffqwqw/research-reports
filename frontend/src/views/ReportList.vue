<template>
  <div>
    <!-- 新增研报入口：表单与任务列表已独立到 /new（NewReport.vue） -->
    <div class="new-entry">
      <router-link to="/new" class="new-btn">＋ 新增研报</router-link>
      <span class="new-hint">
        生成需要 2–3 分钟，提交后可在该页查看进度与结果
      </span>
    </div>

    <div class="search">
      <input
        v-model="q"
        type="text"
        placeholder="搜索已有研报：公司名或股票代码"
        @keyup.enter="load(1)"
      />
      <button @click="load(1)">搜索</button>
    </div>

    <div v-if="loading" class="hint">加载中…</div>
    <div v-else-if="items.length === 0" class="hint">暂无研报</div>

    <div v-else class="list">
      <router-link
        v-for="r in items"
        :key="r.id"
        :to="`/report/${r.code}/${r.period}`"
        class="card item"
      >
        <div class="title">
          {{ r.name }}
          <span class="code">{{ r.code }}</span>
          <span class="period">{{ r.period }}</span>
        </div>
        <div class="meta">{{ r.report_type }} · {{ r.disclosure_date }} · {{ r.industry || '行业未标注' }}</div>
        <div class="ratings">
          <span
            v-for="(v, k) in r.ratings"
            :key="k"
            class="pill"
            :style="{ background: ratingColor(v) }"
          >
            {{ k }} {{ v }}
          </span>
        </div>
      </router-link>
    </div>

    <div v-if="pages > 1" class="pager">
      <button :disabled="page <= 1" @click="load(page - 1)">上一页</button>
      <span>{{ page }} / {{ pages }}</span>
      <button :disabled="page >= pages" @click="load(page + 1)">下一页</button>
    </div>

    <!-- 右侧 A-Z 索引：按公司简称拼音首字母筛选 -->
    <aside class="alpha-index" aria-label="按公司首字母筛选">
      <button
        class="alpha-btn alpha-all"
        :class="{ active: !letter }"
        title="显示全部"
        @click="pickLetter('')"
      >全部</button>
      <button
        v-for="l in ALPHABET"
        :key="l"
        class="alpha-btn"
        :class="{ active: letter === l, empty: !letterCount[l] }"
        :disabled="!letterCount[l]"
        :title="letterCount[l] ? l + ' 开头 · ' + letterCount[l] + ' 篇' : l + ' 暂无研报'"
        @click="pickLetter(l)"
      >{{ l }}</button>
    </aside>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import axios from 'axios'

const items = ref([])
const q = ref('')
const page = ref(1)
const pages = ref(0)
const loading = ref(false)

// ---- 右侧 A-Z 索引 ----
const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('')
const letter = ref('')          // 当前选中的首字母，'' 表示全部
const letterCount = ref({})     // { A: 3, B: 1, ... }，只含有研报的字母

async function loadLetters() {
  try {
    const { data } = await axios.get('/api/initials')
    letterCount.value = data.items || {}
  } catch {
    letterCount.value = {}
  }
}

function pickLetter(l) {
  if (l === letter.value) return
  letter.value = l
  load(1)
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

async function load(p) {
  loading.value = true
  try {
    const { data } = await axios.get('/api/reports', {
      params: {
        q: q.value || undefined,
        initial: letter.value || undefined,
        page: p,
        size: 20,
      },
    })
    items.value = data.items
    page.value = data.page
    pages.value = data.pages
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load(1)
  loadLetters()
})
</script>

<style scoped>
/* 新增研报入口（表单与任务列表见 NewReport.vue） */
.new-entry { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; flex-wrap: wrap; }
.new-btn {
  padding: 9px 18px; font-size: 14px; border-radius: 8px;
  background: var(--brand); color: #fff; text-decoration: none;
}
.new-btn:hover { opacity: .9; }
.new-hint { font-size: 12px; color: var(--muted); }

.search { display: flex; gap: 8px; margin-bottom: 20px; }
.search input {
  flex: 1; padding: 10px 14px; font-size: 14px;
  border: 1px solid var(--line); border-radius: 8px; outline: none;
}
.search input:focus { border-color: var(--brand); }
.search button {
  padding: 0 20px; font-size: 14px; cursor: pointer;
  background: var(--brand); color: #fff; border: none; border-radius: 8px;
}

.hint { color: var(--muted); padding: 40px 0; text-align: center; }

.list { display: flex; flex-direction: column; gap: 12px; }
.item { text-decoration: none; color: var(--text); display: block; }
.item:hover { border-color: var(--brand); }
.title { font-size: 16px; font-weight: 600; }
.code { color: var(--muted); font-weight: 400; margin-left: 6px; }
.period { font-size: 12px; color: var(--brand); border: 1px solid var(--brand); border-radius: 4px; padding: 0 6px; margin-left: 8px; }
.meta { color: var(--muted); font-size: 13px; margin: 4px 0 8px; }
.ratings { display: flex; flex-wrap: wrap; gap: 6px; }
.pill { font-size: 12px; padding: 2px 10px; border-radius: 12px; border: 1px solid var(--line); }

.pager { display: flex; align-items: center; justify-content: center; gap: 12px; margin-top: 20px; }
.pager button {
  padding: 6px 16px; cursor: pointer; background: #fff;
  border: 1px solid var(--line); border-radius: 6px;
}
.pager button:disabled { opacity: 0.4; cursor: not-allowed; }

/* ---- 右侧 A-Z 拼音首字母索引 ---- */
.alpha-index {
  position: fixed;
  top: 50%;
  right: 16px;
  transform: translateY(-50%);
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 6px 5px;
  background: rgba(255, 255, 255, 0.94);
  border: 1px solid var(--line);
  border-radius: 14px;
  box-shadow: 0 2px 12px rgba(24, 95, 165, 0.1);
  z-index: 30;
  user-select: none;
  max-height: calc(100vh - 24px);
  overflow-y: auto;
}
.alpha-btn {
  width: 26px; height: 17px; padding: 0;
  font-size: 11px; line-height: 17px; text-align: center;
  font-family: inherit;
  background: none; border: none; border-radius: 6px;
  color: var(--brand); cursor: pointer;
}
.alpha-btn:hover:not(:disabled) { background: #eaf1fa; }
.alpha-btn.active { background: var(--brand); color: #fff; font-weight: 600; }
.alpha-btn.empty { color: #ccd2da; cursor: default; }
.alpha-all {
  height: 20px; line-height: 20px;
  font-size: 10px; margin-bottom: 3px;
  border-bottom: 1px solid var(--line);
  border-radius: 6px 6px 0 0;
}
/* 宽屏时把索引条挪到 960px 内容列右侧的留白里，更贴近内容 */
@media (min-width: 1180px) {
  .alpha-index { right: calc((100vw - 960px) / 2 - 52px); }
}
</style>
