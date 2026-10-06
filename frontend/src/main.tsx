import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import './index.css'

const container = document.getElementById('root')
if (!container) {
  // A missing mount point is a build error, not a runtime condition worth handling.
  throw new Error('SOJOURNER: #root mount point is missing from index.html')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)