// @ts-nocheck -- executed by Bun's test runner; Bun types are not a production dependency.
import { describe, expect, test } from 'bun:test'
import { projectWorkbenchUploadResponse } from './workbenchMode'

describe('workbench upload projection', () => {
  test('keeps only opaque file metadata and drops injected provider locators', () => {
    const projected = projectWorkbenchUploadResponse({
      WorkbenchFileId: `wf_${'a'.repeat(32)}`,
      Name: 'report.txt',
      Type: 'text/plain',
      Size: 12,
      Url: 'https://private-provider.example/signed',
      CosUrl: 'cos://private-bucket/object',
      ETag: 'provider-secret',
    })

    expect(projected).toEqual({
      WorkbenchFileId: `wf_${'a'.repeat(32)}`,
      Name: 'report.txt',
      Type: 'text/plain',
      Size: 12,
    })
    expect(projected).not.toHaveProperty('Url')
    expect(projected).not.toHaveProperty('CosUrl')
  })

  test('rejects legacy or incomplete workbench upload responses', () => {
    expect(() => projectWorkbenchUploadResponse({ Url: 'https://provider.example' }))
      .toThrow('Workbench upload response is invalid')
    expect(() => projectWorkbenchUploadResponse({
      WorkbenchFileId: `wf_${'a'.repeat(32)}`,
      Name: 'report.txt',
      Type: 'text/plain',
      Size: -1,
    })).toThrow('Workbench upload response is invalid')
  })
})
