import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default {
  root: 'src',
  publicDir: '../public',   // resolve relative to project root, not `src`
  build: {
    outDir: '../dist',
    emptyOutDir: true
  }
}
