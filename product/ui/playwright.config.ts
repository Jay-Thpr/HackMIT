import { defineConfig, devices } from '@playwright/test'

const port = process.env.FAULTLINE_UI_PORT ?? '4173'

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    ...devices['Desktop Chrome'],
    channel: 'chrome',
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1512, height: 982 },
    trace: 'retain-on-failure',
    launchOptions: { args: ['--enable-webgl', '--enable-unsafe-swiftshader'] },
  },
  webServer: { command: `npm run dev -- --port ${port} --strictPort`, url: `http://127.0.0.1:${port}`, reuseExistingServer: !process.env.CI },
})
