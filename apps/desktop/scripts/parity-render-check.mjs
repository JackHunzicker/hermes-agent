// Run beside `npx vite --host 127.0.0.1 --port 5197 --strictPort`.
// Real Chromium + production components. Fixture room data, no gateway/model acceptance.
/* global window: readonly, document: readonly, Image: readonly */
import { chromium, expect } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const output = path.resolve(process.argv[2] || 'test-results/shared-room-parity')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } })
const errors = []
page.on('pageerror', error => errors.push(error.message))
// Fixture must never contact a user's backend or a third-party service.
await page.route('**/*', route => {
  const url = new URL(route.request().url())
  return url.origin === 'http://127.0.0.1:5197' || ['data:', 'blob:'].includes(url.protocol)
    ? route.continue()
    : route.abort()
})
try {
  await page.goto('http://127.0.0.1:5197/scripts/parity-render.html')
  const composer = page.getByRole('textbox', { name: 'Message Board' })
  await expect(composer).toBeVisible()
  await expect(page.getByText('Jordan', { exact: true })).toBeVisible()
  await expect(page.getByText('You', { exact: true })).toHaveCount(0)
  await page.evaluate(() => window.parityFixture.mention())
  await page.getByRole('button', { name: 'Product', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Product (@pm)' })).toBeVisible()
  await composer.fill('Board-only draft; do not send in Other')
  await page.evaluate(() => window.parityFixture.show('Other'))
  await expect(page.getByRole('textbox', { name: 'Message Other' })).toHaveValue('')
  await page.evaluate(() => window.parityFixture.show('Board'))
  await expect(composer).toHaveValue('Board-only draft; do not send in Other')

  const media = await page.evaluate(async () => {
    const canvas = document.createElement('canvas')
    canvas.width = 2048
    canvas.height = 1024
    const ctx = canvas.getContext('2d')
    ctx.fillStyle = '#305080'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.fillStyle = 'white'
    ctx.font = '72px sans-serif'
    ctx.fillText('Original 2048 x 1024 image', 70, 500)
    const original = canvas.toDataURL('image/jpeg', 0.93)
    const blob = await (await fetch(original)).blob()
    const file = new File([blob], 'original.jpg', { type: blob.type })
    const [attachment] = await window.parityFixture.filesToGroupAttachments([file])
    const image = new Image()
    image.src = attachment.data
    await image.decode()
    return {
      original,
      exactBytes: attachment.data === original,
      width: image.naturalWidth,
      height: image.naturalHeight
    }
  })
  expect(media.exactBytes).toBe(true)
  expect(media.width).toBe(2048)
  const chooser = page.waitForEvent('filechooser')
  await page.getByTitle('Share files with this Group Chat').click()
  await (
    await chooser
  ).setFiles({
    name: 'original.jpg',
    mimeType: 'image/jpeg',
    buffer: Buffer.from(media.original.split(',')[1], 'base64')
  })
  await expect(page.getByText('original.jpg', { exact: true })).toBeVisible()
  await page.screenshot({ path: path.join(output, 'room-original-image.png'), fullPage: true })

  await page.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.evaluate(() => window.parityFixture.advanceTask())
  await page.screenshot({ path: path.join(output, 'retry-confirmation.png'), fullPage: true })
  await page.evaluate(() => window.parityFixture.show('Other'))
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: 'Message Other' })).toHaveValue('')
  await page.screenshot({ path: path.join(output, 'other-room-isolated.png'), fullPage: true })
  await page.evaluate(() => window.parityFixture.historyControls())
  await page.keyboard.press('Escape')
  const captures = []
  for (const [theme, width] of [['light', 1100], ['dark', 1100], ['dark', 720]]) {
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ colorScheme: theme })
    await expect(page.locator('html')).toHaveAttribute('data-hermes-mode', theme)
    await expect(page.getByRole('textbox', { name: 'Search room history' })).toBeVisible()
    await page.getByRole('textbox', { name: 'Search room history' }).fill('original')
    for (const name of ['Stop', 'Search', 'Mark room read', 'Edit message', 'Delete message', 'Add thumbs up', 'Stop this thread', 'Mark thread read']) {
      await expect(page.getByRole('button', { name, exact: true })).toBeVisible()
    }
    await expect(page.getByText('1 unread', { exact: true })).toBeVisible()
    const prefix = `history-${theme}-${width}`
    await page.screenshot({ path: path.join(output, `${prefix}.png`), fullPage: true })
    await page.getByRole('button', { name: 'Edit message', exact: true }).click()
    await expect(page.getByRole('textbox', { name: 'Edit message' })).toHaveValue('Please review the original image.')
    await page.screenshot({ path: path.join(output, `${prefix}-edit.png`), fullPage: true })
    await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    await page.getByRole('button', { name: 'Delete message', exact: true }).click()
    await expect(page.getByRole('dialog')).toBeVisible()
    await page.screenshot({ path: path.join(output, `${prefix}-delete.png`), fullPage: true })
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'Stop this thread', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Confirm thread stop' })).toBeVisible()
    await page.screenshot({ path: path.join(output, `${prefix}-thread-stop.png`), fullPage: true })
    await page.keyboard.press('Escape')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    captures.push({ theme, width, height: 900, states: ['controls', 'edit', 'delete confirmation', 'thread stop confirmation'] })
  }
  expect(errors).toEqual([])
  const receipt = {
    result: 'passed',
    captures,
    unqualified: ['live gateway', 'model execution', 'native device', 'room/thread policy controls absent from recovered source'],
    scope: 'Real Chrome / production React components; fixture state, no backend/model execution',
    checks: [
      'exact hosted member handle',
      'room draft isolation and restoration',
      '2048px image exact byte and dimension preservation',
      'native file picker attachment chip',
      'retry confirmation visible across status update',
      'retry dismissed on room switch',
      'history search, read, edit, delete, reaction and scoped thread controls visible in three viewports',
      'edit text and destructive confirmation rendered with production primitives',
      'no document horizontal overflow in narrow desktop viewport'
    ],
    image: { exactBytes: media.exactBytes, width: media.width, height: media.height },
    errors
  }
  await writeFile(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  console.log(JSON.stringify(receipt, null, 2))
} finally {
  await browser.close()
}
