// Playwright config for MediFlow AI's e2e suite.
//
// Assumes two things are already running (see package.json scripts /
// .github/workflows/frontend-ci.yml for how CI does this):
//   1. The backend API on http://localhost:8000 (seeded reference data)
//   2. `vite preview` serving the production build on http://localhost:4173
//
// Run locally:
//   cd backend && uvicorn main:app --port 8000 &
//   cd frontend && npm run build && npm run preview &
//   cd frontend && npx playwright test
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',

  use: {
    baseURL: 'http://localhost:4173/MediFlow-AI/',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: { channel: 'chromium' },
  },

  projects: [
    {
      name: 'mobile-iphone-se',
      use: { viewport: { width: 375, height: 667 }, userAgent: devices['iPhone SE'].userAgent, browserName: 'chromium' },
    },
    {
      name: 'mobile-pixel',
      use: { viewport: { width: 393, height: 851 }, browserName: 'chromium' },
    },
    {
      name: 'desktop-chromium',
      use: { viewport: { width: 1440, height: 900 }, browserName: 'chromium' },
    },
  ],
});
