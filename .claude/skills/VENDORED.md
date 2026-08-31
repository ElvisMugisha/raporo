# Vendored third-party skills

These skills are copied ("vendored") from their source repos so they travel with this repo — no per-machine install. Licenses are retained in each folder. To update one: re-copy from upstream, re-apply the local modifications listed here, and update the commit SHA.

| Skill folder | Source repo | Commit | License | Local modifications |
| --- | --- | --- | --- | --- |
| `frontend-design/` | [anthropics/skills](https://github.com/anthropics/skills) `skills/frontend-design` | 3b3fad9 | Apache-2.0 | none |
| `taste/` | [Leonxlnx/taste-skill](https://github.com/Leonxlnx/taste-skill) `skills/taste-skill` (v2) | ccbc156 | MIT | frontmatter `name` → `taste`; description rewritten to scope it to visual identity/design language and defer other lanes |
| `ui-ux-pro-max/` | [nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) `.claude/skills/ui-ux-pro-max` | d279284 | MIT | `${CLAUDE_PLUGIN_ROOT}/.claude/skills/ui-ux-pro-max` → `.claude/skills/ui-ux-pro-max` (project-relative, we vendor instead of plugin-install); description scoped to layout/typography/color/UX-pattern selection |
| `emil-design-eng/`, `animate/`, `review-animations/`, `improve-animations/` | [emilkowalski/skills](https://github.com/emilkowalski/skills) | d23d7f8 | MIT | none |
| `task-observer/` | [rebelytics/one-skill-to-rule-them-all](https://github.com/rebelytics/one-skill-to-rule-them-all) | 510caad | CC BY 4.0 | copied SKILL.md + references/ + scripts/ only (docs/images omitted) |

## Design-skill lanes (why the descriptions were scoped)

Four design skills overlap by nature; each description routes it to one lane so they don't inject contradictory rules into the same task:

- **frontend-design** — aesthetic direction & structure: distinctive, non-templated design intent.
- **taste** — visual identity & consistent design language (anti generic-AI-website).
- **ui-ux-pro-max** — systematic selection: layout, typography, palettes, UX patterns, design systems (database-backed; scripts need Python 3).
- **animate / emil-design-eng / review-animations / improve-animations** — motion, interaction, and final polish.
- **Impeccable** (plugin, not vendored) — on-demand audits via `/impeccable` commands.
