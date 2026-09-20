import { expect, test, type Page } from '@playwright/test'

async function openComparison(page: Page) {
  await page.goto('/?compare')
  const guide = page.getByRole('dialog', { name: 'What each workspace tab does' })
  await expect(guide).toBeVisible()
  await guide.getByRole('button', { name: 'Close dialog' }).click()
}

async function loadExample(page: Page) {
  await openComparison(page)
  await expect(page.getByRole('heading', { name: 'Compare responders' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'No comparison recording loaded' })).toBeVisible()
  await page.getByRole('button', { name: 'Load illustrative example' }).click()
  await expect(page.getByRole('combobox', { name: 'Case' })).toBeVisible()
}

async function seek(page: Page, value: number) {
  const slider = page.getByRole('slider', { name: 'Comparison timeline' })
  await slider.evaluate((element, next) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, String(next))
    element.dispatchEvent(new Event('input', { bubbles: true }))
  }, value)
}

test('comparison view opens via ?compare with setup description and no silent synthetic fallback', async ({ page }) => {
  await openComparison(page)
  await expect(page.getByText('Recorded comparison', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'No comparison recording loaded' })).toBeVisible()
  await expect(page.getByText(/cmp-\*\.json/)).toBeVisible()
  await expect(page.getByText(/Development-set comparison, not a held-out benchmark/).first()).toBeVisible()
  await expect(page.getByText('Datadog · Not connected — deferred')).toBeVisible()
  await expect(page.locator('.comparison-banner')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Pause simulation' })).toHaveCount(0)
  await expect(page.locator('.sidebar-case')).toHaveCount(0)
  await expect(page.getByText('Read-only comparison replay')).toBeVisible()
})

test('illustrative example marks synthetic, keeps Elastic not run, hides finals until requested', async ({ page }) => {
  await loadExample(page)
  const banner = page.getByRole('status').filter({ hasText: 'Synthetic playback' })
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('no vendor comparison is implied')
  await expect(page.getByText('SANDBOX TOPOLOGY')).toBeVisible()
  const elasticRow = page.locator('.comparison-summary tbody tr[data-arm="elastic"]')
  await expect(elasticRow).toContainText('Not run')
  await expect(elasticRow).toContainText('Synthetic')
  await expect(page.locator('.comparison-summary table')).not.toContainText('Diagnosis')
  await expect(page.locator('.comparison-summary')).toContainText('stay hidden until the replay ends')
  await expect(page.locator('.comparison-outcome').first()).toContainText('Final results hidden')
  await page.getByLabel('Show final results').check()
  await expect(page.locator('.comparison-summary table')).toContainText('Diagnosis')
  await expect(elasticRow).toContainText('Not collected')
})

test('ground truth stays hidden until reveal and rewinding clears the event detail', async ({ page }) => {
  await loadExample(page)
  await expect(page.getByRole('combobox', { name: 'Case' })).toHaveValue('case-a')
  await expect(page.getByRole('combobox', { name: 'Case' }).locator('option').first()).toHaveText('case-a')
  await expect(page.getByText(/expected:/)).toHaveCount(0)
  await expect(page.getByText('H_meta')).toHaveCount(0)
  await seek(page, 110)
  await page.locator('.comparison-panel').first().getByText('Example alert').click()
  await expect(page.locator('.comparison-event-detail')).toBeVisible()
  await seek(page, 20)
  await expect(page.locator('.comparison-event-detail')).toHaveCount(0)
  await page.getByLabel('Show final results').check()
  await expect(page.getByRole('combobox', { name: 'Case' }).locator('option').first()).toHaveText('Retry storm')
  await expect(page.getByText(/expected: H_meta/)).toBeVisible()
})

test('timeline reveals recorded events and gaps stay unconnected', async ({ page }) => {
  await loadExample(page)
  await seek(page, 80)
  const panelA = page.locator('.comparison-panel').first()
  await expect(panelA).toContainText('Example alert')
  await expect(page.locator('.comparison-chart canvas').first()).toBeVisible()
  await page.getByRole('button', { name: 'Play replay' }).click()
  await page.getByRole('button', { name: 'Playback speed 4 times' }).click()
  await expect(page.getByRole('button', { name: 'Pause replay' })).toBeVisible()
  await page.getByRole('button', { name: 'Pause replay' }).click()
  await page.getByRole('button', { name: 'Reset replay' }).click()
  await expect(page.getByRole('slider', { name: 'Comparison timeline' })).toHaveValue('0')
  await expect(panelA).not.toContainText('Example alert')
})

test('event selection shows details; arm switching keeps one shared cursor', async ({ page }) => {
  await loadExample(page)
  await seek(page, 110)
  await page.locator('.comparison-panel').first().getByText('Example alert').click()
  await expect(page.locator('.comparison-event-detail')).toContainText('detect')
  await page.getByRole('button', { name: 'Close event detail' }).click()
  await page.locator('.comparison-panel').first().getByText('Example investigation step').click()
  await expect(page.locator('.comparison-event-detail')).toContainText('hypothesis')
  await page.getByRole('combobox', { name: 'Responder A' }).selectOption('clone_probe')
  await expect(page.locator('.comparison-event-detail')).toHaveCount(0)
  const panelA = page.locator('.comparison-panel').first()
  await expect(panelA).toContainText('Faultline · Clone + Probe')
  await expect(panelA).toContainText('Clone environment')
  await expect(page.locator('.comparison-clock')).toHaveCount(1)
})

test('file upload accepts valid recordings, rejects invalid ones, and renders supplied results verbatim', async ({ page }) => {
  await openComparison(page)
  const metrics = {
    diagnosis: 'H_db', correct: true, detection_s: 45, first_correct_s: 95, recovery_s: 100,
    recovery_status: 'recovered', failed_checkouts_estimate: 1234.5, successful_checkouts_estimate: 6789,
    sample_coverage_pct: 97.5, production_actions: 2, clone_actions: 1, rollback_failures: 1, tokens: 4321, cost_usd: 0.42,
  }
  const valid = {
    schema_version: 'faultline-comparison/1', id: 'cmp-upload', title: 'Uploaded recording',
    created_at: '2026-01-01T00:00:00Z',
    protocol: { horizon_s: 120 },
    cases: [{ id: 'c1', label: 'Case one', world: 'degraded', expected: 'H_db' }],
    runs: [
      { id: 'r1', case_id: 'c1', arm: 'observe', source: 'live', status: 'completed', duration_s: 120, samples: [], events: [], metrics },
      { id: 'r2', case_id: 'c1', arm: 'probe', source: 'live', status: 'error', duration_s: 60, samples: [], events: [] },
    ],
  }
  await page.locator('input[type=file]').setInputFiles({ name: 'cmp-upload.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(valid)) })
  await expect(page.getByRole('combobox', { name: 'Case' })).toBeVisible()
  await expect(page.locator('.comparison-panel').first()).toContainText('Live-recorded run')
  const errorRow = page.locator('.comparison-summary tbody tr[data-arm="probe"]')
  await expect(errorRow).toContainText('Error')
  await expect(page.locator('.comparison-summary table')).not.toContainText('H_db')
  await page.getByLabel('Show final results').check()
  const row = page.locator('.comparison-summary tbody tr[data-arm="observe"]')
  await expect(row).toContainText('H_db')
  await expect(row).toContainText('45 s')
  await expect(row).toContainText('95 s')
  await expect(row).toContainText('1,235')
  await expect(row).toContainText('6,789')
  await expect(row).toContainText('97.5%')
  await expect(row).toContainText('4,321')
  await expect(row).toContainText('0.42')
  const outcome = page.locator('.comparison-outcome[data-arm="observe"]')
  await expect(outcome).toContainText('H_db')
  await expect(outcome).toContainText('100 s')
  await expect(outcome).toContainText('1,235')
  await expect(page.locator('.comparison-provenance details')).toBeAttached()
  await expect(page.getByRole('status').filter({ hasText: 'Synthetic playback' })).toHaveCount(0)
  await page.locator('input[type=file]').setInputFiles({ name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{"schema_version":"other"}') })
  await expect(page.getByRole('alert')).toContainText('Not a valid recording')
})

test('comparison layout stays in the viewport at 390px', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await loadExample(page)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.getByLabel('Show final results').check()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('desktop capture of both panels with finals revealed', async ({ page }) => {
  await loadExample(page)
  await seek(page, 110)
  await page.getByLabel('Show final results').check()
  await page.getByRole('combobox', { name: 'Responder B' }).selectOption('clone_probe')
  await expect(page.locator('.comparison-chart canvas').first()).toBeVisible()
  await page.waitForTimeout(600)
  await page.screenshot({ path: 'test-results/comparison-desktop.png', fullPage: true })
})
