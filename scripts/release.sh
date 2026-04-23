#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Usage ──────────────────────────────────────────────────────────────────
if [ $# -lt 1 ]; then
    echo "Usage: $0 <VERSION>"
    echo "Example: $0 0.1.0"
    exit 1
fi

VERSION=$1

# ── Validate version format ───────────────────────────────────────────────
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "Error: version '$VERSION' must be in X.Y.Z format."
    exit 1
fi

# ── Pre-flight checks ────────────────────────────────────────────────────
cd "$PROJECT_DIR"

BRANCH=$(git branch --show-current)
if [ "$BRANCH" != "main" ]; then
    echo "Warning: you are on branch '$BRANCH', not 'main'."
    read -p "Continue anyway? [y/N] " choice
    [ "$choice" = "y" ] || [ "$choice" = "Y" ] || exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Error: working tree is not clean. Commit or stash changes first."
    exit 1
fi

if git rev-parse "$VERSION" >/dev/null 2>&1; then
    echo "Error: tag '$VERSION' already exists."
    exit 1
fi

echo "=== Releasing rancher-keycloak-operator $VERSION ==="
echo ""

# ── Step 1: Bump versions ────────────────────────────────────────────────
echo "[1/4] Bumping versions to $VERSION..."

# pyproject.toml
sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
echo "  Updated pyproject.toml"

# Helm Chart.yaml
CHART_FILE="helm/rancher-keycloak-operator/Chart.yaml"
sed -i '' "s/^version: .*/version: $VERSION/" "$CHART_FILE"
sed -i '' "s/^appVersion: .*/appVersion: \"$VERSION\"/" "$CHART_FILE"
echo "  Updated $CHART_FILE"

# Helm values.yaml (default image tag)
VALUES_FILE="helm/rancher-keycloak-operator/values.yaml"
sed -i '' "s/^  tag: \".*\"/  tag: \"$VERSION\"/" "$VALUES_FILE"
echo "  Updated $VALUES_FILE"
echo ""

# ── Step 2: Regenerate lockfile ──────────────────────────────────────────
echo "[2/4] Regenerating uv.lock..."
uv lock 2>/dev/null || true
echo ""

# ── Step 3: Commit ────────────────────────────────────────────────────────
echo "[3/4] Committing release..."
git add pyproject.toml "$CHART_FILE" "$VALUES_FILE"
git add uv.lock 2>/dev/null || true
git commit -m "Release $VERSION"
echo ""

# ── Step 4: Tag ───────────────────────────────────────────────────────────
echo "[4/4] Tagging $VERSION..."
git tag "$VERSION"
echo ""

echo "=== Release $VERSION prepared ==="
echo ""
echo "Review the commit and tag, then push with:"
echo "  git push origin main --tags"
