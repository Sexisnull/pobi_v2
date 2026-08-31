import React from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.jsx'
import './styles/tokens.css'
import './styles/layout.css'
import './styles/ui.css'
import './styles/pages.css'

// 前端部署在 nginx 根目录（base=/），与 vite.config.js 的 base 保持一致。
createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter basename="/">
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
