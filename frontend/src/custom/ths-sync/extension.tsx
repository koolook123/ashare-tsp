import { lazy } from 'react'
import { Database } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

const ThsSync = lazy(() => import('./Page').then(m => ({ default: m.ThsSync })))

const extension: FrontendExtension = {
  id: 'koolook.ths-sync',
  apiVersion: 1,
  routes: [{ id: 'ths-sync', path: '/ths-sync', component: ThsSync }],
  navigation: [{ id: 'ths-sync', routeId: 'ths-sync', label: '同花顺同步', icon: Database }],
}

export default extension
