#!/usr/bin/env node
// Lightweight secret gate for staged files, run by the pre-commit hook. Node builtins only, so it works
// before `npm ci` and has no build-time dependencies.
import { readFileSync, statSync } from 'node:fs'

const PATTERNS = [
  [/-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----/, 'private key'],
  [/AKIA[0-9A-Z]{16}/, 'AWS access key id'],
  [/ghp_[A-Za-z0-9]{36}/, 'GitHub personal token'],
  [/gho_[A-Za-z0-9]{36}/, 'GitHub OAuth token'],
  [/ghs_[A-Za-z0-9]{36}/, 'GitHub App installation token'],
  [/ghu_[A-Za-z0-9]{36}/, 'GitHub user-to-server token'],
  [/ghr_[A-Za-z0-9]{36}/, 'GitHub refresh token'],
  [/github_pat_[A-Za-z0-9_]{60,}/, 'GitHub fine-grained token'],
  [/xox[baprs]-[0-9A-Za-z-]{10,}/, 'Slack token'],
  [
    /https:\/\/hooks\.slack\.com\/(services|workflows|triggers)\/[A-Za-z0-9/_-]{20,}/,
    'Slack webhook URL',
  ],
  [/sk-ant-[A-Za-z0-9_-]{20,}/, 'Anthropic API key'],
  [/sk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{32,}/, 'OpenAI-style API key'],
  // The lookbehind anchors each base64url run to one start, which avoids quadratic backtracking.
  // The bounded prefix still catches a JWT glued to up to 16 base64url characters, such as
  // `%20`, `\n` or `token_` before it.
  [
    /(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{0,16}?eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}/,
    'JWT',
  ],
  [/(client_secret|CLIENT_SECRET)\s*[=:]\s*['"]?[A-Za-z0-9~._-]{20,}/, 'client secret assignment'],
  [/AccountKey=[A-Za-z0-9+/=]{60,}/, 'Azure storage account key'],
]

// Every size: a secret in a large file is as leaked as one in a small file. Binary files are
// skipped below, by content.
const files = process.argv.slice(2).filter((f) => {
  try {
    return statSync(f).isFile()
  } catch {
    return false
  }
})

// Offsets of each line start, computed once per file so every match finds its line in O(log n).
function findLineStarts(text) {
  const starts = [0]
  for (let i = text.indexOf('\n'); i !== -1; i = text.indexOf('\n', i + 1)) starts.push(i + 1)
  return starts
}

// Index of the last line start at or before `offset`.
function lineIndexOf(starts, offset) {
  let lo = 0
  let hi = starts.length - 1
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (starts[mid] <= offset) lo = mid
    else hi = mid - 1
  }
  return lo
}

let failed = false
for (const file of files) {
  if (/(^|\/)check-secrets\.mjs$/.test(file)) continue
  const buf = readFileSync(file)
  if (buf.includes(0)) continue // binary
  const text = buf.toString('utf8')
  const lineStarts = findLineStarts(text)
  for (const [re, label] of PATTERNS) {
    // Every match, not the first: a fixture marked `secret-scan:ignore` early in a file must not hide a
    // real key further down it.
    for (const m of text.matchAll(new RegExp(re.source, 'g'))) {
      const index = lineIndexOf(lineStarts, m.index)
      const line = index + 1
      const end = text.indexOf('\n', lineStarts[index])
      const lineText = text.slice(lineStarts[index], end === -1 ? text.length : end)
      // Deliberate fixtures (tests, docs) opt out per line with this marker.
      if (lineText.includes('secret-scan:ignore')) continue
      console.error(`✖ ${file}:${line}: looks like a ${label}`)
      failed = true
    }
  }
}
if (failed) {
  console.error('\nRefusing to commit. Move the value to .env (git-ignored) or a Secret manifest.')
  process.exit(1)
}
