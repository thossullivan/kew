const CREDENTIAL_RULES = [
  {
    rule: 'credential',
    pattern: /\b(?:sk-proj-|sk-ant-api03-|sk-)[A-Za-z0-9_-]{20,}\b/g
  },
  {
    rule: 'credential',
    pattern: /\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b/g
  },
  {
    rule: 'credential',
    pattern: /\b(?:npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,})\b/g
  },
  {
    rule: 'credential',
    pattern: /\bAKIA[0-9A-Z]{16}\b/g
  },
  {
    rule: 'credential',
    pattern: /\bxox[a-zA-Z]-[A-Za-z0-9-]{10,}\b/g
  },
  {
    rule: 'credential',
    pattern: /-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY(?: BLOCK)?-----/g
  },
  {
    rule: 'credential',
    pattern:
      /\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b\s*[:=]\s*["']?[^\s"'<>]{8,}/gi
  },
  {
    rule: 'credential',
    pattern: /\bAuthorization\s*:\s*Bearer\s+[^\s"'<>]{8,}/gi
  },
  {
    rule: 'credential',
    pattern: /https?:\/\/[^\s/@:"'<>]+:[^\s/@"'<>]+@[^\s"'<>]+/gi
  }
];

const RULES = [
  {
    rule: 'absolute local path',
    pattern: /\/(?:Users|home)\/[^\s"'<>]+/g
  },
  ...CREDENTIAL_RULES,
  {
    rule: 'private remote',
    pattern: /\b(?:ssh|git):\/\/[^\s"'<>]+|\b(?:git|hg|svn)@[A-Za-z0-9.-]+:[^\s"'<>]+/gi
  },
  {
    rule: 'private remote',
    pattern:
      /https?:\/\/(?:localhost|127\.0\.0\.1|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|[^/\s"'<>]+\.(?:internal|local))(?=[:/\s"'<>]|$)/gi
  },
  {
    rule: 'external script',
    pattern: /<script\b[^>]*\bsrc\s*=\s*["']?(?:https?:)?\/\//gi
  }
];

function lineNumber(text, index) {
  return text.slice(0, index).split('\n').length;
}

function percentDecoded(text) {
  let decoded = text;
  for (let pass = 0; pass < 2; pass += 1) {
    const next = decoded.replace(/%([0-9a-f]{2})/gi, (_match, hex) =>
      String.fromCharCode(Number.parseInt(hex, 16))
    );
    if (next === decoded) break;
    decoded = next;
  }
  return decoded;
}

function base64Decoded(text) {
  const decoded = [];
  const candidates =
    text.match(/(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{16,}={0,2}(?![A-Za-z0-9+/_=-])/g) ?? [];
  for (const candidate of candidates) {
    try {
      decoded.push(Buffer.from(candidate, 'base64').toString('utf8'));
    } catch {
      // Invalid candidates cannot contain encoded credentials.
    }
  }
  return decoded;
}

function addFindings(findings, seen, rules, text, lineAt) {
  for (const { rule, pattern } of rules) {
    for (const match of text.matchAll(pattern)) {
      const line = lineAt(match.index);
      const key = `${rule}:${line}`;
      if (!seen.has(key)) {
        findings.push({ rule, line });
        seen.add(key);
      }
    }
  }
}

/** Return unsafe content findings for an evidence pack intended for public deployment. */
export function publicPackFindings(text) {
  const findings = [];
  const seen = new Set();
  addFindings(findings, seen, RULES, text, (index) => lineNumber(text, index));

  text.split('\n').forEach((line, index) => {
    const decodedLine = percentDecoded(line);
    if (decodedLine !== line) {
      addFindings(findings, seen, CREDENTIAL_RULES, decodedLine, () => index + 1);
    }
    for (const decoded of [...base64Decoded(line), ...base64Decoded(decodedLine)]) {
      addFindings(findings, seen, CREDENTIAL_RULES, decoded, () => index + 1);
    }
  });

  return findings;
}
