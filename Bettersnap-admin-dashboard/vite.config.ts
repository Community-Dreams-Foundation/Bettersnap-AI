import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const productionApiOrigin='https://bettersnap-functions-dagchpg8f0b7fjed.eastus-01.azurewebsites.net'

export default defineConfig({
  plugins:[react()],
  server:{
    proxy:{
      '/api':{
        target:productionApiOrigin,
        changeOrigin:true,
        secure:true,
      },
    },
  },
})
