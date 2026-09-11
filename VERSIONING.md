# Versioning & Deployment

The bot uses **semantic versioning** (`vX.Y.Z`) and a two-branch workflow so the
VPS can deploy a specific, known-good version instead of "whatever is on main".

## Branches

- **`main`** — the stable, deployable branch. Only tagged releases land here.
- **`develop`** — the integration branch. All feature changes are committed here first.

## Workflow

1. **Feature work** → commit to `develop` (never directly to `main`).
   ```
   git checkout develop
   git pull
   # ... make changes ...
   git add . && git commit -m "Add <feature>"
   git push origin develop
   ```
2. **Test** on the VPS from `develop` (or a staging checkout).
3. **Release** — merge `develop` → `main` and tag it:
   ```
   git checkout main
   git pull
   git merge develop
   git tag v0.2.0
   git push origin main --tags
   ```
4. **Deploy** the VPS from the tag:
   ```
   git fetch --tags
   git checkout v0.2.0
   ```

## Version bump rules

- `v0.1.0` → `v0.1.1` — patch (bug fixes)
- `v0.1.0` → `v0.2.0` — minor (new features)
- `v0.1.0` → `v1.0.0` — major (breaking changes)

## Current version

`v0.2.0`
