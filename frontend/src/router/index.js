import { createRouter, createWebHistory } from 'vue-router'
import ReportList from '../views/ReportList.vue'
import ReportDetail from '../views/ReportDetail.vue'
import Auth from '../views/Auth.vue'
import Profile from '../views/Profile.vue'
import NewReport from '../views/NewReport.vue'

const routes = [
  { path: '/', name: 'list', component: ReportList },
  {
    path: '/report/:code/:period',
    name: 'detail',
    component: ReportDetail,
    props: true,
  },
  { path: '/auth', name: 'auth', component: Auth },
  { path: '/profile', name: 'profile', component: Profile },
  // 「新增研报」独立成页：提交表单 + 我的任务列表
  // （放在列表页内嵌时，用户看不到生成进度，会「不知道出结果了」）
  { path: '/new', name: 'new', component: NewReport },
]

export default createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(to, from, savedPosition) {
    if (to.hash) return { el: to.hash, behavior: 'smooth' }
    return savedPosition || { top: 0 }
  },
})
