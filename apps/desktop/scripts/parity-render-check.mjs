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
  expect(errors).toEqual([])
  const receipt = {
    result: 'passed',
    scope: 'Real Chrome / production React components; fixture state, no backend/model execution',
    checks: [
      'exact hosted member handle',
      'room draft isolation and restoration',
      '2048px image exact byte and dimension preservation',
      'native file picker attachment chip',
      'retry confirmation visible across status update',
      'retry dismissed on room switch'
    ],
    image: { exactBytes: media.exactBytes, width: media.width, height: media.height },
    errors
  }
  await writeFile(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  console.log(JSON.stringify(receipt, null, 2))
} finally {
  await browser.close()
}
