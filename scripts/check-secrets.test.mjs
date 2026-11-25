import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, test } from 'node:test'

const scanner = fileURLToPath(new URL('./check-secrets.mjs', import.meta.url))
const githubToken = `ghp_${'a'.repeat(36)}`
const openaiKeys = [
  ['legacy', `sk-${'a'.repeat(40)}`],
  ['project', `sk-proj-${'aB1_-'.repeat(10)}`],
  ['service account', `sk-svcacct-${'aB1_-'.repeat(10)}`],
]
let directory

beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), 'check-secrets-test-'))
})

afterEach(() => {
  rmSync(directory, { recursive: true, force: true })
})

function scan(path, content) {
  const file = join(directory, path)
  mkdirSync(dirname(file), { recursive: true })
  writeFileSync(file, content)
  const result = spawnSync(process.execPath, [scanner, file], { encoding: 'utf8' })
  assert.ifError(result.error)
  assert.equal(result.signal, null)
  return result
}

for (const path of ['.env.example', 'app/.env.example']) {
  test(`rejects credentials in ${path}`, () => {
    const result = scan(path, `TOKEN=${githubToken}\n`)
    assert.equal(result.status, 1)
    assert.ok(result.stderr.includes(`${path}:1: looks like a GitHub personal token`))
  })
}

test('accepts example environment files with placeholders', () => {
  const result = scan('app/.env.example', 'OPENAI_API_KEY=your-api-key\nTOKEN=\n')
  assert.equal(result.status, 0)
  assert.equal(result.stderr, '')
})

test('allows an explicitly ignored fixture in an example environment file', () => {
  const result = scan('app/.env.example', `TOKEN=${githubToken} # secret-scan:ignore\n`)
  assert.equal(result.status, 0)
  assert.equal(result.stderr, '')
})

for (const [format, key] of openaiKeys) {
  test(`rejects an OpenAI ${format} key`, () => {
    const result = scan('credentials.txt', `OPENAI_API_KEY=${key}\n`)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /:1: looks like a OpenAI-style API key/)
  })

  test(`allows an explicitly ignored OpenAI ${format} fixture`, () => {
    const result = scan('credentials.txt', `OPENAI_API_KEY=${key} # secret-scan:ignore\n`)
    assert.equal(result.status, 0)
    assert.equal(result.stderr, '')
  })
}

test('an ignored fixture does not hide a later credential', () => {
  const result = scan(
    'app/.env.example',
    `TOKEN=${githubToken} # secret-scan:ignore\nTOKEN=${githubToken}\n`,
  )
  assert.equal(result.status, 1)
  assert.ok(result.stderr.includes('.env.example:2: looks like a GitHub personal token'))
  assert.ok(!result.stderr.includes('.env.example:1:'))
})

for (const [prefix, label] of [
  ['ghu_', 'GitHub user-to-server token'],
  ['ghr_', 'GitHub refresh token'],
]) {
  const token = `${prefix}${'a'.repeat(36)}`

  test(`rejects a ${label}`, () => {
    const result = scan('credentials.txt', `TOKEN=${token}\n`)
    assert.equal(result.status, 1)
    assert.ok(result.stderr.includes(`:1: looks like a ${label}`))
  })

  test(`allows an explicitly ignored ${label} fixture`, () => {
    const result = scan('credentials.txt', `TOKEN=${token} # secret-scan:ignore\n`)
    assert.equal(result.status, 0)
    assert.equal(result.stderr, '')
  })
}

test('reports a match on the last line without a trailing newline', () => {
  const result = scan('credentials.txt', `one\ntwo\nTOKEN=${githubToken}`)
  assert.equal(result.status, 1)
  assert.ok(result.stderr.includes('credentials.txt:3: looks like a GitHub personal token'))
})

test('handles CRLF line endings: numbering by newline and the ignore marker', () => {
  const result = scan(
    'credentials.txt',
    `a\r\nTOKEN=${githubToken} # secret-scan:ignore\r\nb\r\nTOKEN=${githubToken}\r\n`,
  )
  assert.equal(result.status, 1)
  assert.ok(result.stderr.includes('credentials.txt:4: looks like a GitHub personal token'))
  assert.ok(!result.stderr.includes('credentials.txt:2:'))
})

test('scans a file with many matches quickly', () => {
  const fixture = `TOKEN=${githubToken} # secret-scan:ignore\n`
  const count = 30000
  const start = Date.now()
  const result = scan('big.txt', `${fixture.repeat(count)}TOKEN=${githubToken}\n`)
  const elapsed = Date.now() - start
  assert.equal(result.status, 1)
  assert.ok(result.stderr.includes(`big.txt:${count + 1}: looks like a GitHub personal token`))
  assert.ok(!result.stderr.includes('big.txt:1:'))
  assert.equal(result.stderr.split('\n').filter((l) => l.startsWith('✖')).length, 1)
  assert.ok(elapsed < 5000, `scan took ${elapsed} ms, expected under 5000 ms`)
})

test('reports a token at column 0 of a line after an ignored line', () => {
  const result = scan('credentials.txt', `x # secret-scan:ignore\n${githubToken}\n`)
  assert.equal(result.status, 1)
  assert.ok(result.stderr.includes('credentials.txt:2: looks like a GitHub personal token'))
  assert.ok(!result.stderr.includes('credentials.txt:1:'))
})

test('scans a long run of JWT-like prefixes quickly and still reports a later token', () => {
  const start = Date.now()
  const result = scan('jwt-run.txt', `${'eyJ'.repeat(80000)}\n${githubToken}\n`)
  const elapsed = Date.now() - start
  assert.equal(result.status, 1)
  assert.ok(result.stderr.includes('jwt-run.txt:2: looks like a GitHub personal token'))
  assert.ok(elapsed < 5000, `scan took ${elapsed} ms, expected under 5000 ms`)
})

const jwt = `eyJ${'a'.repeat(20)}.eyJ${'b'.repeat(20)}.${'c'.repeat(20)}`
for (const [name, content] of [
  ['after a space', `Authorization: Bearer ${jwt}\n`],
  ['at column 0', `${jwt}\n`],
  ['after =', `TOKEN=${jwt}\n`],
  ['after Bearer%20', `Authorization=Bearer%20${jwt}\n`],
  ['after ?token%3D', `https://example.test/?token%3D${jwt}\n`],
  ['after an escaped newline in JSON', `{"log":"line\\n${jwt}"}\n`],
  ['after token_', `token_${jwt}\n`],
  ['after id-', `id-${jwt}\n`],
]) {
  test(`detects a JWT ${name}`, () => {
    const result = scan('jwt.txt', content)
    assert.equal(result.status, 1)
    assert.ok(result.stderr.includes('jwt.txt:1: looks like a JWT'))
  })
}

test('documented limit: a JWT glued to 17 or more base64url characters is not reported', () => {
  const result = scan('jwt-limit.txt', `${'a'.repeat(17)}${jwt}\n`)
  assert.equal(result.status, 0)
})

test('scans two long JWT-like runs split by a dot quickly and still reports a later token', () => {
  const start = Date.now()
  const result = scan(
    'jwt-dot-run.txt',
    `${'eyJ'.repeat(40000)}.${'eyJ'.repeat(40000)}\n${githubToken}\n`,
  )
  const elapsed = Date.now() - start
  assert.equal(result.status, 1)
  assert.ok(result.stderr.includes('jwt-dot-run.txt:2: looks like a GitHub personal token'))
  assert.ok(elapsed < 5000, `scan took ${elapsed} ms, expected under 5000 ms`)
})
