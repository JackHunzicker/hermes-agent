import { afterEach, expect, it, vi } from 'vitest'

import { filesToGroupAttachments } from './group-attachments'

vi.mock('@hermes/plugin-sdk', () => ({ host: { notify: vi.fn() } }))

afterEach(() => vi.restoreAllMocks())

it('preserves picked image bytes rather than replacing the file with a resized preview', async () => {
  // Drive the old large-image branch without relying on jsdom image decoding.
  const image = document.createElement('img')
  Object.defineProperties(image, {
    width: { value: 2048 },
    height: { value: 1024 },
    src: { set: () => { queueMicrotask(() => image.dispatchEvent(new Event('load'))) } }
  })
  vi.spyOn(globalThis, 'Image').mockImplementation(function () {
    return image
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    drawImage: vi.fn()
  } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/png;base64,Y2hhbmdlZA==')
  const bytes = new Uint8Array([255, 216, 255, 224, 0, 16, 74, 70, 73, 70])
  const file = new File([bytes], 'original.jpg', { type: 'image/jpeg' })

  const [attachment] = await filesToGroupAttachments([file])

  expect(attachment.data).toBe(`data:image/jpeg;base64,${btoa(String.fromCharCode(...bytes))}`)
  expect(attachment.name).toBe(file.name)
})
