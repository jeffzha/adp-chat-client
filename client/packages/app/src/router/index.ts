import {
  createRouter,
  createWebHashHistory,
  type RouteLocationNormalized
} from 'vue-router'
import { isLoggedIn } from '@/service/login'
import { httpService } from '@/service/httpService'
import {
  initializeWorkbench,
  setWorkbenchSessionError,
  workbenchRuntime,
} from '@/workbench/runtime'


const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      // 渠道会话：/:applicationId/channel/:conversationId
      // 渠道（访客）会话不落地本地 chat_conversation 表，权威源在 CAPI DescribeConversationList。
      // 通过 URL 里的 /channel/ 段显式区分：
      //   1) 刷新时前端可直接判定为渠道会话，跳过普通 /chat/messages 首屏拉取
      //   2) 侧栏点击渠道会话时 router.push 到该变体，保持刷新可复原
      // 注意：必须放在通用 home 路由之前，确保 /channel/ 段优先匹配（vue-router 从上到下匹配）。
      path: '/:applicationId/channel/:conversationId',
      name: 'home-channel',
      component: () => import('@/pages/Home.vue'),
    },
    {
      // 定时任务会话：/:applicationId/timertask/:conversationId?triggerId=xxx
      // 定时任务（AppTrigger）触发的会话与渠道会话同源（不在本地 chat_conversation 表，走 CAPI）。
      // 通过 URL 里的 /timertask/ 段显式区分：
      //   1) 刷新时前端判定为定时任务会话，走 DescribeConversationMessageList 拉首屏
      //   2) 右侧默认展开定时任务执行记录面板（sidebar 模式，对齐企微机器人体验）
      //   3) query.triggerId 用于刷新后自动定位到具体触发器详情
      // 必须放在通用 home 路由之前。
      path: '/:applicationId/timertask/:conversationId',
      name: 'home-timertask',
      component: () => import('@/pages/Home.vue'),
    },
    {
      // 统一层级结构：/:applicationId?/:conversationId?
      // 例：/                       -> 未选应用
      //     /appA                   -> 选中应用 appA，无会话
      //     /appA/convX             -> 应用 appA 下的普通会话 convX
      path: '/:applicationId?/:conversationId?',
      name: 'home',
      component: () => import('@/pages/Home.vue'),
    },
    {
      path: '/unavailable',
      name: 'workbench-unavailable',
      component: () => import('@/pages/WorkbenchUnavailable.vue'),
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/pages/Login.vue'),
    },
    {
      path: '/share/:shareId?',
      name: 'share',
      meta:{
        unauthorized: true
      },
      component: () => import('@/pages/Share.vue'),
    },
  ],
})

let entre = false
router.beforeEach(
  async (to: RouteLocationNormalized, _from: RouteLocationNormalized) => {
    await initializeWorkbench()

    if (workbenchRuntime.enabled) {
      if (workbenchRuntime.status === 'error') {
        if (to.name !== 'workbench-unavailable') return { name: 'workbench-unavailable' }
        return
      }
      if (to.name === 'workbench-unavailable' || to.name === 'login') {
        return { name: 'home' }
      }
      if (to.meta.unauthorized) return

      // Workbench URLs never accept an application id from the browser. The
      // trusted application comes exclusively from /application/list.
      if (to.params.applicationId) {
        return { name: 'home' }
      }
      try {
        await httpService.get('/account/info')
      } catch {
        setWorkbenchSessionError('The workbench session is unavailable or has expired.')
        return { name: 'workbench-unavailable' }
      }
      return
    }

    if (to.meta.unauthorized) {
      return
    } else {
      if (entre) {
        // 防止重复进入, axiosInstance.ts中会统一处理AccountUnauthorized错误，并跳转登录页
        console.log(`prevent re-entre`);
        return
      }
      // 配置AUTO_CREATE_ACCOUNT=true时，检查是否需要自动创建账号
      entre = true
      try {
        await httpService.get('/account/info')
      } catch {
      }
      entre = false
      
      if (to.name !== 'login' && !isLoggedIn()) {
        return { name: 'login' }
      } else if (to.name === 'login' && isLoggedIn()) {
        return { name: 'home' }
      } else {
        return
      }
    }
  },
)

export default router
