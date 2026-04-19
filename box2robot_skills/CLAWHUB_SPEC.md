# ClawHub Skill Specification Reference

> Source: https://github.com/openclaw/clawhub / https://clawhub.ai

---

## 1. Folder Structure

A skill is a folder containing:

| File | Required | Description |
|------|----------|-------------|
| `SKILL.md` or `skill.md` | **Required** | Markdown with YAML frontmatter — the core skill definition |
| Text-based supporting files | Optional | Helper scripts, configs, etc. |
| `.clawhubignore` / `.clawdhubignore` | Optional | Exclude files from publishing (like .gitignore) |

**CLI-generated metadata (auto-created, don't manually edit):**
- `.clawhub/origin.json` — local install metadata
- `.clawhub/lock.json` — workdir state

---

## 2. YAML Frontmatter (SKILL.md)

```yaml
---
name: my-skill
description: Brief summary for UI/search
version: 1.0.0
homepage: https://docs.example.com
emoji: "\U0001F527"
metadata:
  openclaw:
    requires:
      env: [MY_API_KEY, MY_SECRET]     # Environment variables (ALL must be set)
      bins: [curl, jq]                  # CLI binaries (ALL must exist)
      anyBins: [ffmpeg, avconv]         # CLI binaries (at least ONE must exist)
      config: [~/.config/app.json]      # Config file paths read by skill
    primaryEnv: MY_API_KEY              # Main credential variable
    always: false                       # Always-active skill (no install needed)
    skillKey: my-shortcut               # Override invocation key
    emoji: "\U0001F527"                 # Display emoji
    homepage: https://docs.example.com  # Documentation URL
    os: ["macos", "linux"]              # OS restrictions
    install:                            # Dependency install specifications
      - kind: brew
        formula: jq
        bins: [jq]
      - kind: node
        package: typescript
        bins: [tsc]
      - kind: go
        package: github.com/user/tool
        bins: [tool]
      - kind: uv
        package: aiohttp
        bins: []
    nix:                                # Nix plugin configuration
      plugin: my-nix-plugin
      systems: ["x86_64-linux", "aarch64-darwin"]
    config: {}                          # Clawdbot config specification
---
```

---

## 3. Complete Field Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Skill name (used as display name) |
| `description` | string | Yes | Brief summary for search/UI |
| `version` | string | Yes | Semantic version (e.g., `1.0.0`) |
| `homepage` | string | No | Documentation URL |
| `emoji` | string | No | Display emoji |
| `metadata.openclaw.requires.env` | string[] | No | Environment variables skill expects (ALL must be set) |
| `metadata.openclaw.requires.bins` | string[] | No | CLI tools required (ALL must exist on PATH) |
| `metadata.openclaw.requires.anyBins` | string[] | No | CLI tools required (at least ONE must exist) |
| `metadata.openclaw.requires.config` | string[] | No | Config file paths the skill reads |
| `metadata.openclaw.primaryEnv` | string | No | Main credential variable (shown prominently in UI) |
| `metadata.openclaw.always` | boolean | No | Always-active (no explicit install needed) |
| `metadata.openclaw.skillKey` | string | No | Override default invocation key |
| `metadata.openclaw.os` | string[] | No | OS restrictions (e.g., `["macos"]`) |
| `metadata.openclaw.install` | array | No | Dependency install specifications |
| `metadata.openclaw.nix` | object | No | Nix plugin configuration |
| `metadata.openclaw.config` | object | No | Clawdbot config specification |

---

## 4. Install Specification Kinds

| Kind | Fields | Description |
|------|--------|-------------|
| `brew` | `formula`, `bins` | Homebrew formula |
| `node` | `package`, `bins` | npm package (global) |
| `go` | `package`, `bins` | Go module |
| `uv` | `package`, `bins` | Python package via uv/pip |

Each entry:
```yaml
- kind: <brew|node|go|uv>
  formula: <name>     # brew only
  package: <name>     # node/go/uv
  bins: [<binary>]    # binaries provided by this package
```

---

## 5. Technical Constraints

### Allowed File Types
- Content types: `text/*`, JSON, YAML, TOML, JS, TS, Markdown, SVG
- Full list defined in `packages/schema/src/textFiles.ts`
- Binary files (images, compiled code) are NOT allowed

### Size Limits
- **Total bundle**: 50MB maximum
- **Embedding**: SKILL.md + ~40 non-Markdown files (best-effort vector indexing)

### Slug Requirements
- Derived from folder name
- Must be lowercase, URL-safe
- Pattern: `^[a-z0-9][a-z0-9-]*$`

### Licensing
- All ClawHub skills use **MIT-0** (no attribution required, commercial use permitted)
- Users cannot override license terms

---

## 6. Publishing

### Versioning
- Each publish creates a new semantic version
- Tags (like `latest`) point to specific versions
- `changelog` is **required** for each version

### Upload Flow
1. Request upload session from ClawHub
2. Upload files to Convex storage URLs
3. Submit metadata + changelog
4. Server validates:
   - File sizes within limits
   - Content types are text-based
   - SKILL.md is parseable (valid YAML frontmatter)
   - Version is unique (no duplicates)
   - GitHub account age >= 14 days

### Validation Checks
| Check | Requirement |
|-------|-------------|
| SKILL.md exists | Must be present in root |
| YAML frontmatter valid | Must parse without errors |
| `name` field | Required, non-empty |
| `description` field | Required, non-empty |
| `version` field | Required, valid semver |
| File types | Only text-based files allowed |
| Total size | <= 50MB |
| Account age | GitHub account >= 14 days old |

---

## 7. Data Objects (Server-side)

### Skill Object
| Field | Type | Description |
|-------|------|-------------|
| `slug` | string | Unique identifier (from folder name) |
| `displayName` | string | Display name |
| `ownerUserId` | string | Publisher's user ID |
| `summary` | string | From SKILL.md |
| `versions` | map | version string -> SkillVersion ID |
| `tags` | map | tag name (e.g., "latest") -> version |
| `badges` | array | Trust/quality badges |
| `moderationStatus` | string | Review status |
| `stats` | object | Stars, installs, etc. |

### SkillVersion Object
| Field | Type | Description |
|-------|------|-------------|
| `skillId` | string | Parent skill ID |
| `version` | string | Semantic version |
| `changelog` | string | Required change description |
| `files` | array | File metadata (path, size, storageId, sha256) |
| `parsedMeta` | object | Parsed from SKILL.md frontmatter |
| `embeddingId` | string | Vector search embedding ID |

---

## 8. Search & Discovery

- **Vector search**: Covers SKILL.md + other text files + metadata
- **Filters**: tags, owner, moderation status, stars, update time
- **Ranking**: Combination of relevance, stars, recency

---

## 9. Content Policy (Prohibited)

| Category | Examples |
|----------|---------|
| Bypass & unauthorized access | Auth bypass, CAPTCHA bypass, account takeover |
| Platform abuse | Stealth accounts, fake engagement, spam automation |
| Fraud | Fake certificates, deceptive payment flows |
| Privacy violation | Mass contact scraping, harassment, doxxing |
| Non-consensual impersonation | Deepfakes, cloning influencers |
| Explicit content | NSFW generation |
| Covert execution | Obfuscated install commands, hidden key requirements |

**Enforcement**: Hide, remove, or hard-delete skills; revoke tokens; ban repeat offenders.

---

## 10. Review Criteria (What Reviewers Check)

| Area | What's Checked |
|------|----------------|
| Purpose & capability | Name/description matches actual code behavior |
| Instruction scope | SKILL.md instructions match declared capabilities; sensitive ops flagged |
| Install mechanism | No unexpected remote downloads or archive extractions |
| Credentials | All used env vars/tokens declared in metadata; primaryEnv set correctly |
| Persistence & privilege | `always` flag appropriate; autonomous invocation risk assessed |
| Physical/privacy risk | Camera, microphone, device control, factory reset flagged for review |
| Shell/exec risk | Any OS shell or arbitrary command execution capability flagged |

### Common Review Failures
1. **Metadata mismatch**: Code uses env vars not declared in `requires.env`
2. **Missing primaryEnv**: Token/key used but not declared as primary credential
3. **Undeclared capabilities**: "exec" or "shell" commands that could run arbitrary OS commands
4. **Sensitive operations**: Physical device control, camera/mic access without safety notes
5. **Autonomous risk**: Skills controlling physical devices should note supervision requirements
