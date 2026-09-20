import { expect, test, type Page } from '@playwright/test'

async function seekTo(page: Page, time: number) {
  const slider = page.getByRole('slider', { name: 'Simulation timeline' })
  await slider.evaluate((element, value) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, String(value))
    element.dispatchEvent(new Event('input', { bubbles: true }))
    element.dispatchEvent(new Event('change', { bubbles: true }))
  }, time)
  await expect(slider).toHaveValue(String(time))
}

for (const architecture of ['commerce', 'pipeline']) {
  test(`${architecture}: healthy start, explicit clone lifecycle, cleanup, and rewind`, async ({ page }) => {
    await page.goto('/')
    await page.getByRole('combobox', { name: 'Example architecture' }).selectOption(architecture)
    await expect(page.locator('.map-canvas canvas')).toBeVisible()
    const lifecycle = page.getByRole('region', { name: 'Incident lifecycle' })
    const environments = page.getByRole('combobox', { name: 'Selected environment' }).locator('option')
    await expect(lifecycle).toHaveAttribute('data-phase', 'monitoring')
    await expect(environments).toHaveCount(1)
    await expect(page.getByRole('button', { name: 'Play incident demo', exact: true })).toBeVisible()
    await expect(page.locator('.node-issue-marker')).toHaveCount(0)
    await page.screenshot({ path: `test-results/${architecture}-healthy-start.png`, fullPage: true })
    await seekTo(page, 12)
    await expect(lifecycle).toHaveAttribute('data-phase', 'detected')
    await expect(environments).toHaveCount(1)
    await expect(page.locator('.node-issue-marker').first()).toBeVisible()
    await seekTo(page, 24)
    await expect(lifecycle).toHaveAttribute('data-phase', 'starting')
    await expect(lifecycle.locator('[data-environment="clone-a"]')).toHaveAttribute('data-lifecycle', 'starting')
    await seekTo(page, 25)
    await expect(lifecycle.locator('[data-environment="clone-a"]')).toHaveAttribute('data-lifecycle', 'ready')
    await seekTo(page, 47)
    await expect(environments).toHaveCount(3)
    await expect(lifecycle).toHaveAttribute('data-phase', 'investigating')
    await page.waitForTimeout(1200)
    await page.screenshot({ path: `test-results/${architecture}-investigation.png`, fullPage: true })
    await seekTo(page, 72)
    await expect(lifecycle).toHaveAttribute('data-phase', 'confirming')
    await seekTo(page, 103)
    await expect(lifecycle).toHaveAttribute('data-phase', 'cleanup')
    await expect(lifecycle.locator('[data-environment="clone-a"]')).toHaveAttribute('data-lifecycle', 'destroying')
    await seekTo(page, 105)
    await expect(environments).toHaveCount(3)  // archived clones stay in the workspace for review
    await expect(lifecycle.locator('[data-environment="clone-a"]')).toHaveAttribute('data-lifecycle', 'archived')
    await expect(lifecycle.locator('[data-environment="clone-b"]')).toHaveAttribute('data-lifecycle', 'destroying')
    await seekTo(page, 108)
    await expect(environments).toHaveCount(3)
    await expect(lifecycle).toHaveAttribute('data-phase', 'complete')
    await expect(page.getByText('Clones retained for review; evidence kept', { exact: true })).toBeVisible()
    // the clone that carried the confirmed cause is emphasised; the other is ruled out
    await expect(lifecycle.locator('[data-environment="clone-a"]')).toHaveAttribute('data-outcome', 'confirmed')
    await expect(lifecycle.locator('[data-environment="clone-a"]')).toHaveAttribute('data-winner', 'true')
    await expect(lifecycle.locator('[data-environment="clone-b"]')).toHaveAttribute('data-outcome', 'ruled-out')
    await expect(page.locator('dialog[open]')).toHaveCount(0)  // seeking to the end does not pop the report
    await page.getByRole('button', { name: 'Open the incident report', exact: true }).click()
    await expect(page.locator('dialog[open] #dialog-title')).toHaveText('Self-sustaining overload confirmed')
    await page.keyboard.press('Escape')
    await page.waitForTimeout(1200)
    await page.screenshot({ path: `test-results/${architecture}-cleanup-complete.png`, fullPage: true })
    await page.getByRole('button', { name: 'Review the explanation', exact: true }).click()
    await expect(page.locator('.why-checks')).toHaveCount(2)
    await expect(page.getByText('Clone archived. Its test results are retained above.')).toHaveCount(2)
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Agent workspace', exact: true }).click()
    await seekTo(page, 47)
    await expect(environments).toHaveCount(3)
    await expect(lifecycle.locator('[data-environment="clone-a"]')).not.toHaveAttribute('data-outcome', /.+/)  // no emphasis before the verdict
    await expect(page.getByRole('heading', { name: 'Self-sustaining overload confirmed', exact: true })).toHaveCount(0)
    await page.getByRole('button', { name: 'Restart simulation', exact: true }).click()
    await expect(lifecycle).toHaveAttribute('data-phase', 'monitoring')
    await expect(environments).toHaveCount(1)
  })
}

test('clone motion is seek-safe, paused, and archived clones stay with the winner emphasised', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
  await seekTo(page, 24)
  const cloneA = page.locator('.environment-label[data-environment="clone-a"]')
  const cloneB = page.locator('.environment-label[data-environment="clone-b"]')
  await expect(cloneA).toHaveAttribute('data-presence', '0.000')
  await seekTo(page, 25)
  await expect(cloneA).toHaveAttribute('data-presence', '1.000')
  // clone A carries the cause the verdict confirms: it never fades while being removed
  await seekTo(page, 103)
  await expect(cloneA).toHaveAttribute('data-presence', '1.000')
  await expect(cloneA).toHaveAttribute('data-outcome', 'confirmed')
  await expect(cloneA).toHaveAttribute('data-emphasised', 'true')
  await seekTo(page, 105)
  await expect(cloneA).toHaveAttribute('data-lifecycle', 'archived')
  await expect(cloneB).toHaveAttribute('data-level', '2')
  // clone B was ruled out: it fades to the archived presence and stays
  await seekTo(page, 106)
  await expect(cloneB).toHaveAttribute('data-presence', /^0\.84/)
  await page.waitForTimeout(650)
  await expect(cloneB).toHaveAttribute('data-presence', /^0\.84/)
  await page.getByRole('button', { name: 'Full motion', exact: true }).click()
  await expect(cloneB).toHaveAttribute('data-presence', '1.000')
  await page.getByRole('button', { name: 'Reduced motion', exact: true }).click()
  await seekTo(page, 108)
  await expect(cloneB).toHaveAttribute('data-presence', '0.400')
  await expect(cloneB).toHaveAttribute('data-outcome', 'ruled-out')
  await expect(cloneA).toHaveAttribute('data-presence', '1.000')
  await expect(page.getByRole('button', { name: 'Replay incident demo', exact: true })).toBeVisible()
})

test('the incident report opens by itself when playback reaches the end', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
  await seekTo(page, 100)
  await page.getByRole('button', { name: 'Resume demo', exact: true }).click()
  await expect(page.locator('dialog[open] #dialog-title')).toHaveText('Self-sustaining overload confirmed', { timeout: 15000 })
  await expect(page.locator('dialog[open] .report-winner')).toContainText('Clone A')
  await expect(page.locator('dialog[open] .report-dialog-facts')).toContainText('Clone A, then production')
  await page.getByRole('button', { name: 'Stay in the workspace', exact: true }).click()
  await expect(page.locator('dialog[open]')).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Incident lifecycle' })).toHaveAttribute('data-phase', 'complete')
})

test('demo playback advances from healthy to clone startup and freezes when paused', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
  await page.getByRole('button', { name: 'Play incident demo', exact: true }).click()
  await page.getByRole('button', { name: 'Playback speed 1 times' }).click()
  await page.getByRole('button', { name: 'Playback speed 2 times' }).click()
  const lifecycle = page.getByRole('region', { name: 'Incident lifecycle' })
  await expect(lifecycle).toHaveAttribute('data-phase', 'detected', { timeout: 8000 })
  await expect(lifecycle).toHaveAttribute('data-phase', 'starting', { timeout: 8000 })
  await page.getByRole('button', { name: 'Pause demo', exact: true }).click()
  const cursor = await page.getByRole('slider', { name: 'Simulation timeline' }).inputValue()
  await page.waitForTimeout(500)
  expect(await page.getByRole('slider', { name: 'Simulation timeline' }).inputValue()).toBe(cursor)
  await expect(lifecycle).toHaveAttribute('data-phase', 'starting')
  const canvas = page.locator('.map-canvas canvas')
  const paused = await canvas.screenshot()
  await page.waitForTimeout(650)
  expect(await canvas.screenshot()).toEqual(paused)
})

test('pages explain their jobs and offer meaningful empty-state actions', async ({ page }) => {
  await page.goto('/')
  const navigation = page.getByRole('navigation', { name: 'Main navigation' })
  await navigation.getByRole('button', { name: 'Clone experiments', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'No clone investigation yet' })).toBeVisible()
  await expect(page.locator('.lab-clone-card')).toHaveCount(0)
  await page.getByRole('button', { name: 'Draft a test', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('does not save the plan, create a clone, or run a test')
  await page.getByRole('textbox', { name: 'Hypothesis to test' }).fill('A dependency is slow')
  await page.getByRole('textbox', { name: 'Predicted response' }).fill('Reducing requests lowers latency')
  await page.getByRole('button', { name: 'Validate draft', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('Nothing was saved or executed')
  await page.keyboard.press('Escape')
  await navigation.getByRole('button', { name: 'Incident replay', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Review an investigation.' })).toBeVisible()
  await expect(page.getByText(/no recorded incidents loaded/)).toBeVisible()  // says so rather than implying saved history
  await page.getByRole('button', { name: 'Play from the start', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Incident lifecycle' })).toHaveAttribute('data-phase', 'monitoring')
  await expect(page.getByRole('button', { name: 'Pause demo', exact: true })).toBeVisible()
})

test('renders the real WebGL scene and a clearly marked, interactive prototype', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await seekTo(page, 47)
  await expect(page.getByText('Simulated data', { exact: true })).toBeVisible()
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
  await expect(page.locator('.inspector')).toHaveCount(0)
  await expect(page.locator('.node-label')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Inspect primary-db in Production', exact: true })).toBeVisible()
  await page.waitForTimeout(1800)
  await page.screenshot({ path: 'test-results/workspace-desktop.png', fullPage: true })
  await page.getByRole('button', { name: 'Inspect primary-db in Production', exact: true }).click()
  await expect(page.getByText('ENTITY INSPECTOR', { exact: true })).toBeVisible()
  await expect(page.getByText('Agent activity here', { exact: true })).toBeVisible()
  await expect(page.getByText('Instances: not collected')).toBeVisible()
  await page.getByRole('tab', { name: /Decision trace/ }).click()
  await expect(page.getByText('Scripted example · not a live model call').first()).toBeVisible()
  expect(errors).toEqual([])
})

for (const width of [1512, 900, 600, 390]) {
  test(`sidebar keeps its responsive layout across navigation at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 982 })
    await page.goto('/')
    const navigation = page.getByRole('navigation', { name: 'Main navigation' })
    await navigation.getByRole('button', { name: 'Why this incident?', exact: true }).click()
    const sidebar = page.locator('.sidebar')
    const sidebarWidth = await sidebar.evaluate(element => getComputedStyle(element).width)
    const contentOffset = await page.locator('.main-shell').evaluate(element => getComputedStyle(element).marginLeft)
    for (const name of ['Agent workspace', 'Clone experiments', 'Why this incident?', 'Observability', 'Agent workspace', 'Incident replay']) {
      const button = navigation.getByRole('button', { name, exact: true })
      await button.click()
      await expect(button).toHaveAttribute('aria-current', 'page')
      await expect(sidebar).toHaveCSS('width', sidebarWidth)
      await expect(page.locator('.main-shell')).toHaveCSS('margin-left', contentOffset)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
      if (width > 760) {
        await expect(button.locator('span').first()).toBeVisible()
        await expect(page.locator('.sidebar-case')).toBeVisible()
      } else {
        await expect(button.locator('span').first()).toBeHidden()
      }
    }
  })
}

test('context dropdowns use the workspace theme instead of default controls', async ({ page }) => {
  await page.goto('/')
  for (const name of ['Example architecture', 'Selected environment']) {
    const select = page.getByRole('combobox', { name, exact: true })
    await expect(select).toHaveCSS('appearance', 'none')
    await expect(select).toHaveCSS('border-radius', '6px')
    await expect(select).toHaveCSS('background-color', 'rgb(43, 49, 45)')
    await expect(select).toHaveCSS('color', 'rgb(230, 232, 226)')
    expect(await select.evaluate(element => getComputedStyle(element).backgroundImage)).toContain('data:image/svg+xml')
    await select.focus()
    await expect(select).toHaveCSS('outline-color', 'rgb(183, 195, 150)')
  }
  await page.getByRole('combobox', { name: 'Example architecture', exact: true }).selectOption('pipeline')
  await expect(page.getByRole('combobox', { name: 'Example architecture', exact: true })).toHaveValue('pipeline')
  await seekTo(page, 47)  // the replay opens healthy, so clones only exist once the investigation starts
  await page.getByRole('combobox', { name: 'Selected environment', exact: true }).selectOption('clone-a')
  await expect(page.getByTestId('topology-stage')).toHaveAttribute('data-isolated-layer', 'clone-a')
})

test('seeking backward removes future environments and evidence', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await expect(page.getByRole('combobox', { name: 'Selected environment' }).locator('option')).toHaveCount(3)
  await page.getByRole('button', { name: 'Restart simulation' }).click()
  await expect(page.getByRole('combobox', { name: 'Selected environment' }).locator('option')).toHaveCount(1)
  await expect(page.getByText('WORKING HYPOTHESES', { exact: true })).not.toBeVisible()
  await page.getByRole('button', { name: 'Play simulation' }).click()
  await expect(page.getByRole('button', { name: 'Pause simulation', exact: true }).last()).toBeVisible()
  await page.getByRole('button', { name: 'Pause simulation', exact: true }).last().click()
  const before = await page.getByRole('slider', { name: 'Simulation timeline' }).inputValue()
  await page.waitForTimeout(500)
  expect(await page.getByRole('slider', { name: 'Simulation timeline' }).inputValue()).toBe(before)
})

test('supports a second arbitrary architecture, 3D view, metrics, and safe draft controls', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await page.getByRole('combobox', { name: 'Example architecture' }).selectOption('pipeline')
  await seekTo(page, 47)
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Show flat topology' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Inspect events in Production', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Observability', exact: true }).click()
  await page.getByRole('textbox', { name: 'Filter services' }).fill('admin-api')
  await expect(page.locator('tbody tr')).toHaveCount(1)
  await expect(page.locator('tbody tr')).toContainText('Not collected')
  await page.getByRole('button', { name: 'Draft experiment', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('button', { name: 'Validate draft' }).click()
  await expect(page.getByRole('status')).toContainText('Nothing was saved or executed')
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'Safety & approvals' }).click()
  await expect(page.getByRole('button', { name: /Approval unavailable/ })).toBeDisabled()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).not.toBeVisible()
})

test('keeps mobile content in the viewport and respects reduced motion', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/')
  await seekTo(page, 47)
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Show flat topology' })).toHaveCount(0)
  await page.screenshot({ path: 'test-results/workspace-mobile.png', fullPage: true })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.getByRole('button', { name: 'Draft experiment', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.keyboard.press('Escape')
})

test('phase navigation synchronizes replay and previous event controls', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await page.locator('.phase-disclosure summary').click()
  await page.getByRole('navigation', { name: 'Investigation phases' }).getByRole('button', { name: /Confirming in production/ }).click()
  await expect(page.getByRole('slider', { name: 'Simulation timeline' })).toHaveValue('72')
  await expect(page.getByRole('navigation', { name: 'Investigation phases' }).getByRole('button', { name: /Confirming in production/ })).toHaveAttribute('aria-current', 'step')
  await page.getByRole('button', { name: 'Previous event', exact: true }).click()
  await expect(page.getByRole('slider', { name: 'Simulation timeline' })).toHaveValue('70')
  await page.getByRole('navigation', { name: 'Investigation phases' }).getByRole('button', { name: /Monitoring/ }).click()
  await expect(page.getByRole('combobox', { name: 'Selected environment' }).locator('option')).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Previous event', exact: true })).toBeDisabled()
})

test('spatial investigation and draft dialog meet core accessibility checks', async ({ page }) => {
  const { default: AxeBuilder } = await import('@axe-core/playwright')
  await page.goto('/')
  await seekTo(page, 47)
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Show flat topology' })).toHaveCount(0)
  const workspace = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()
  expect(workspace.violations.map(item => ({ id: item.id, nodes: item.nodes.map(node => node.target) }))).toEqual([])
  await page.getByRole('button', { name: 'Draft experiment', exact: true }).click()
  const dialog = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()
  expect(dialog.violations.map(item => ({ id: item.id, nodes: item.nodes.map(node => node.target) }))).toEqual([])
})

test('follow investigation focuses event environments and can pause', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
  await page.getByRole('button', { name: 'Follow key events', exact: true }).click()
  await page.getByRole('button', { name: 'Resume demo', exact: true }).click()
  await expect(page.getByRole('combobox', { name: 'Selected environment' })).toHaveValue('clone-b')
  await expect(page.getByRole('button', { name: 'Inspect primary-db in Clone B', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Pause demo', exact: true }).click()
  const cursor = await page.getByRole('slider', { name: 'Simulation timeline' }).inputValue()
  await page.waitForTimeout(300)
  expect(await page.getByRole('slider', { name: 'Simulation timeline' }).inputValue()).toBe(cursor)
})

test('node selection reveals scoped activity and closing returns to an uncluttered space', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await expect(page.getByRole('button', { name: 'Inspect primary-db in Production', exact: true })).toBeVisible()
  await expect(page.locator('.inspector')).toHaveCount(0)
  await page.getByRole('button', { name: 'Inspect primary-db in Production', exact: true }).click()
  await expect(page.locator('.selected-entity strong')).toHaveText('primary-db')
  await expect(page.getByRole('tab', { name: /Decision trace/ })).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('.node-agent-activity')).toContainText('Production')
  await page.waitForTimeout(900)
  await page.screenshot({ path: 'test-results/workspace-node-focus.png', fullPage: true })
  await page.getByRole('button', { name: 'Close entity inspector' }).click()
  await expect(page.locator('.inspector')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Open evidence', exact: true })).toBeVisible()
})

for (const viewport of [{ width: 1512, height: 982 }, { width: 480, height: 844 }]) {
  test(`expanded map keeps node evidence visible and clickable at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.goto('/')
    await page.getByRole('button', { name: 'Expand map', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Exit expanded map', exact: true })).toBeVisible()
    await expect(page.locator('.workspace-grid')).toHaveCSS('position', 'fixed')
    for (const node of ['primary-db', 'orders-api']) {
      if (viewport.width < 850) {
        await page.getByRole('button', { name: 'Find an entity', exact: true }).click()
        await page.locator('.entity-picker').getByRole('button', { name: node, exact: true }).click()
      } else {
        await page.getByRole('button', { name: `Inspect ${node} in Production`, exact: true }).click()
      }
      await expect(page.getByRole('button', { name: 'Close evidence', exact: true })).toHaveAttribute('aria-expanded', 'true')
      await expect(page.locator('.selected-entity strong')).toHaveText(node)
      await page.getByRole('tab', { name: 'Evidence', exact: true }).click({ timeout: 4000 })
      await expect(page.getByRole('tab', { name: 'Evidence', exact: true })).toHaveAttribute('aria-selected', 'true')
      const bounds = await page.locator('.inspector').boundingBox()
      expect(bounds).not.toBeNull()
      expect(bounds!.x).toBeGreaterThanOrEqual(0)
      expect(bounds!.y).toBeGreaterThanOrEqual(0)
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width)
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height)
      await page.getByRole('button', { name: 'Close entity inspector', exact: true }).click()
      await expect(page.locator('.inspector')).toHaveCount(0)
      await page.getByRole('button', { name: 'Fit whole system', exact: true }).click()
    }
    await page.getByRole('button', { name: 'Open evidence', exact: true }).click()
    await page.getByRole('tab', { name: /Decision trace/ }).click()
    await page.getByRole('button', { name: 'Close evidence', exact: true }).click()
    await expect(page.locator('.inspector')).toHaveCount(0)
    await page.getByRole('button', { name: 'Open evidence', exact: true }).click()
    await page.getByRole('button', { name: 'Exit expanded map', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Expand map', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Expand map', exact: true }).click()
    await page.getByRole('tab', { name: 'Evidence', exact: true }).click()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('button', { name: 'Expand map', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  })
}

test('layer focus isolates a clone and all layers restores the overview', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await expect(page.getByTestId('topology-stage')).toHaveAttribute('data-isolated-layer', 'all')
  await page.getByRole('button', { name: 'Focus Clone A layer', exact: true }).click()
  await expect(page.getByTestId('topology-stage')).toHaveAttribute('data-isolated-layer', 'clone-a')
  await expect(page.getByRole('button', { name: 'Focus Clone A layer', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('combobox', { name: 'Selected environment' })).toHaveValue('clone-a')
  await page.waitForTimeout(1000)
  await page.screenshot({ path: 'test-results/workspace-layer-focus.png', fullPage: true })
  await page.getByRole('button', { name: 'All layers', exact: true }).click()
  await expect(page.getByTestId('topology-stage')).toHaveAttribute('data-isolated-layer', 'all')
  await page.getByRole('button', { name: 'Focus Clone B layer', exact: true }).click()
  await page.getByRole('button', { name: 'Restart simulation', exact: true }).click()
  await expect(page.getByTestId('topology-stage')).toHaveAttribute('data-isolated-layer', 'all')
})


test('incident markers remain clickable after close camera zoom', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await page.getByRole('button', { name: 'Inspect primary-db in Production', exact: true }).click()
  await page.waitForTimeout(1300)
  const canvas = page.locator('.map-canvas canvas')
  const box = (await canvas.boundingBox())!
  await page.mouse.move(box.x + box.width * 0.4, Math.min(800, box.y + box.height * 0.5))
  await page.mouse.wheel(0, -900)
  await page.waitForTimeout(1200)
  const marker = page.getByRole('button', { name: 'Inspect issue in primary-db in Production', exact: true })
  await expect.poll(() => marker.evaluate(element => {
    const bounds = element.getBoundingClientRect()
    const canvasBounds = element.closest('.map-canvas')!.getBoundingClientRect()
    const hit = document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)
    return bounds.top >= canvasBounds.top && bounds.bottom <= canvasBounds.bottom && !!hit && element.contains(hit)
  })).toBe(true)
  await marker.click()
  await expect(page.getByRole('region', { name: 'Issue details' })).toContainText('Elevated latency and errors')
})

test('expanded map keeps the incident inspector above the scene', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await page.getByRole('button', { name: 'Expand map', exact: true }).click()
  await page.getByRole('button', { name: 'Inspect issue in primary-db in Production', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Issue details' })).toBeVisible()
  await expect.poll(() => page.locator('.inspector').evaluate(element => {
    const bounds = element.getBoundingClientRect()
    const hit = document.elementFromPoint(bounds.x + 30, bounds.y + 30)
    return !!hit && element.contains(hit)
  })).toBe(true)
  await page.getByRole('button', { name: 'Close entity inspector', exact: true }).click()
  await expect(page.locator('.inspector')).toHaveCount(0)
  await page.getByRole('button', { name: 'Exit expanded map', exact: true }).click()
  await expect(page.locator('.topology-panel')).not.toHaveClass(/is-expanded/)
})

test('issue markers reveal the selected system symptoms and measurements', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await page.getByRole('button', { name: 'Inspect issue in primary-db in Production', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Issue details' })).toContainText('Elevated latency and errors')
  await expect(page.getByRole('region', { name: 'Issue details' })).toContainText('18.4%')
  await expect(page.locator('.selected-entity strong')).toHaveText('primary-db')
  await expect(page.getByRole('button', { name: 'Show flat topology' })).toHaveCount(0)
})

test('clone test markers reveal individual recorded assertions', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  const marker = page.getByRole('button', { name: 'Incident reproduced: passed in Clone A', exact: true })
  await expect(marker).toHaveAttribute('title', 'Incident reproduced · passed')
  await marker.click()
  await expect(page.getByRole('region', { name: 'Test result' })).toContainText('Latency, load, and errors reproduce the incident shape.')
  await expect(page.getByRole('region', { name: 'Test result' })).toContainText('Expected')
  await page.getByRole('button', { name: 'Close test inspector' }).click()
  await expect(page.locator('.inspector')).toHaveCount(0)
})

test('explanation page separates causes, tests and evidence from the 3D workspace', async ({ page }) => {
  await page.goto('/')
  await seekTo(page, 47)
  await page.getByRole('button', { name: 'Why this incident?', exact: true }).first().click()
  await expect(page.getByRole('heading', { name: 'To find out which explanation survives a test.' })).toBeVisible()
  await expect(page.locator('.why-hypothesis')).toHaveCount(2)
  await expect(page.locator('.why-chart .uplot')).toBeVisible()
  await page.screenshot({path:'test-results/explanation-page.png',fullPage:true})
  await page.getByRole('button', { name: 'Restart simulation', exact: true }).click()
  await expect(page.locator('.why-hypothesis')).toHaveCount(0)
  await page.getByRole('button', { name: 'Watch the agents in 3D' }).click()
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
})

test('shared dark palette stays readable across pages', async ({ page }) => {
  const { default: AxeBuilder } = await import('@axe-core/playwright')
  await page.goto('/')
  await seekTo(page, 47)
  for (const name of ['Why this incident?', 'Observability', 'Clone experiments', 'Incident replay']) {
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name, exact: true }).click()
    await page.screenshot({path:`test-results/palette-${name.replace(/[^a-z]/gi,'')}.png`,fullPage:true})
    const result = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa']).analyze()
    expect.soft(result.violations.map(v => ({id:v.id, nodes:v.nodes.map(n=>n.target)}))).toEqual([])
  }
})

test('agent marker opens its purpose and current work', async ({page}) => {
  await page.goto('/')
  await seekTo(page, 47)
  await page.getByRole('button', {name:'Inspect Investigator A', exact:true}).click()
  await expect(page.getByRole('region',{name:'Agent activity'})).toContainText('Why this step')
  await expect(page.getByRole('region',{name:'Agent activity'})).toContainText('What comes next')
  await expect(page.locator('.selected-entity strong')).toHaveText('Investigator A')
  await page.screenshot({path:'test-results/agent-inspector.png',fullPage:true})
})

test('loads a real incident from the Product API when requested', async ({ page }) => {
  // The spec runs against the Vite dev server, which proxies /api to `faultline ui` on :8010.
  // Skips cleanly when no API is running so the prototype suite stays self-contained.
  const index = await page.request.get('/api/incidents').catch(() => null)
  test.skip(!index || !index.ok(), 'Product API (faultline ui) not running on :8010')
  const incidents: { id: string }[] = await index!.json()
  test.skip(!incidents.length, 'no incidents in the audit log')
  await page.goto(`/?incident=${encodeURIComponent(incidents[0].id)}`)
  await expect(page.getByText('Live incident', { exact: true })).toBeVisible()
  await expect(page.locator('select[aria-label="Example architecture"]')).toHaveValue(incidents[0].id)
  await expect(page.locator('.map-canvas canvas')).toBeVisible()
  await expect(page.getByText('Live incident · real audit log')).toBeVisible()
  await page.locator('select[aria-label="Example architecture"]').selectOption('commerce')
  await expect(page.getByText('Simulated data', { exact: true })).toBeVisible()
})

for (const [diagnosis, confirmed, label] of [
  ['H_meta', true, 'Retry storm'],
  ['H_db', true, 'Degraded DB'],
  ['H_db', false, ''],
  ['none_of_the_above', false, ''],
] as const) {
  test(`diagnosis rendering follows ${diagnosis} / confirmed=${confirmed} and rewinds`, async ({ page }) => {
    const { scenarios } = await import('../src/scenarios')
    const scenario = structuredClone(scenarios[0])
    scenario.id = 'diagnosis-rendering'
    scenario.live = true
    scenario.complete = true
    scenario.hypotheses = [
      { ...scenario.hypotheses[1], id: 'H_db', title: 'Degraded DB' },
      { ...scenario.hypotheses[0], id: 'H_meta', title: 'Retry storm' },
    ]
    scenario.events = scenario.events.map(event => event.kind === 'verdict' ? { ...event, diagnosis, confirmed, title: `${diagnosis}: ${confirmed ? 'confirmed' : 'not confirmed'}` } : event)
    scenario.events.push({ id: 'initial-verdict', sequence: 60, at: 60, kind: 'verdict', actor: 'math', environmentId: 'production', title: 'Initial probe inconclusive', detail: '', diagnosis: 'none_of_the_above', confirmed: false })
    scenario.events.sort((a, b) => a.at - b.at || a.sequence - b.sequence)
    await page.route('**/api/**', route => route.fulfill({ json: route.request().url().endsWith('/api/incidents') ? [{ id: scenario.id }] : scenario }))
    await page.goto(`/?incident=${scenario.id}`)
    await expect(page.getByText('Live incident', { exact: true })).toBeVisible()
    await page.getByRole('slider', { name: 'Simulation timeline' }).press('End')
    await page.getByRole('button', { name: 'Inspect primary-db in Production', exact: true }).click()
    await page.getByRole('tab', { name: 'Evidence', exact: true }).click()
    const confirmedCards = page.locator('.hypothesis-card').filter({ hasText: 'CONFIRMED IN PRODUCTION' })
    await expect(confirmedCards).toHaveCount(confirmed ? 1 : 0)
    if (confirmed) await expect(confirmedCards).toContainText(label)
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Why this incident?', exact: true }).click()
    await expect(page.locator('.why-intro h2')).toHaveText(confirmed ? `Confirmed cause: ${label}.` : 'No cause confirmed.')
    await expect(page.locator('.why-conclusion h2')).toHaveText(confirmed ? `Confirmed cause: ${label}.` : 'No cause confirmed.')
    await expect(page.getByText('Recovery held after retries were restored.', { exact: true })).toHaveCount(0)
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Incident replay', exact: true }).click()
    await expect(page.locator('.report-preview h3')).toHaveText(`${diagnosis}: ${confirmed ? 'confirmed' : 'not confirmed'}`)
    await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Why this incident?', exact: true }).click()
    await page.getByRole('button', { name: 'Restart simulation', exact: true }).click()
    await expect(page.locator('.why-conclusion h2')).toHaveText('The cause is not confirmed yet.')
    await page.getByRole('button', { name: 'Watch the agents in 3D' }).click()
    await page.getByRole('slider', { name: 'Simulation timeline' }).press('ArrowRight')
    await expect(page.locator('h1')).not.toContainText('confirmed')
  })
}

for (const undoStatus of ['active', 'unknown', 'undone', 'expired'] as const) {
  test(`action rendering preserves ${undoStatus} release and replay state`, async ({ page }) => {
    const { scenarios } = await import('../src/scenarios')
    const scenario = structuredClone(scenarios[0])
    scenario.id = 'action-rendering'
    scenario.live = true
    scenario.complete = true
    scenario.duration = 35
    scenario.events = [
      { id: 'apply', sequence: 1, at: 5, kind: 'action', actor: 'adapter', environmentId: 'production', targetId: scenario.targetId, title: 'Apply retry cap', detail: '', action: { id: 'cap', label: 'Retry cap', ttl: 20 } },
      { id: 'undo', sequence: 2, at: 10, kind: 'undo', actor: 'adapter', environmentId: 'production', targetId: scenario.targetId, title: 'Release attempted', detail: '', undoId: 'cap', undoStatus },
    ]
    await page.route('**/api/**', route => route.fulfill({ json: route.request().url().endsWith('/api/incidents') ? [{ id: scenario.id }] : scenario }))
    await page.goto(`/?incident=${scenario.id}`)
    await expect(page.getByText('Live incident', { exact: true })).toBeVisible()
    const slider = page.getByRole('slider', { name: 'Simulation timeline' })
    await slider.press('Home')
    for (let i = 0; i < 12; i++) await slider.press('ArrowRight')
    await page.getByRole('button', { name: 'Inspect primary-db in Production', exact: true }).click()
    const facts = page.locator('.issue-facts')
    if (undoStatus === 'active') await expect(facts).toContainText('Release failed · 13s TTL')
    else if (undoStatus === 'unknown') await expect(facts).toContainText('Awaiting confirmed reversion')
    else await expect(facts).not.toContainText('Retry cap')
    await slider.press('End')
    if (undoStatus === 'active' || undoStatus === 'unknown') await expect(facts).toContainText('Awaiting confirmed reversion')
    else await expect(facts).not.toContainText('Retry cap')
    await slider.press('Home')
    await expect(facts).not.toContainText('Retry cap')
    for (let i = 0; i < 6; i++) await slider.press('ArrowRight')
    await expect(facts).toContainText('19s TTL')
    await expect(facts).not.toContainText('Release failed')
  })
}
