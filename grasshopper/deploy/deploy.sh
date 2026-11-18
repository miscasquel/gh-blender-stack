#!/usr/bin/env bash
# Deploy Stageloom.gha into the Grasshopper Libraries folder (plan §10).
#
#     grasshopper/deploy/deploy.sh [--dry-run]
#
# The Windows user under $WINEPREFIX/drive_c/users is discovered, not assumed:
# STAGELOOM_WINE_USER overrides it, Public is skipped, and zero or multiple
# real users fail with the candidates listed. --dry-run prints the resolved
# user and destination without creating or copying anything.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${WINEPREFIX:-$HOME/.local/share/wineprefixes/rhino8}"
USERS="$PREFIX/drive_c/users"
GHA="$(cd "$HERE/.." && pwd)/build/out/Stageloom.gha"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      cat <<'EOF'
usage: grasshopper/deploy/deploy.sh [--dry-run]

  --dry-run  print the resolved wine user and destination; copy nothing
EOF
      exit 0
      ;;
    *)
      echo "deploy.sh: unknown argument: $arg" >&2
      echo "usage: grasshopper/deploy/deploy.sh [--dry-run]" >&2
      exit 2
      ;;
  esac
done

resolve_wine_user() {
  local user entry name
  local users=()

  if [ -n "${STAGELOOM_WINE_USER:-}" ]; then
    user="$STAGELOOM_WINE_USER"
    case "$user" in
      */*|*\\*|Public|public)
        echo "deploy.sh: STAGELOOM_WINE_USER=$user is not a Windows user profile name" >&2
        return 1
        ;;
    esac
    if [ ! -d "$USERS/$user" ]; then
      echo "deploy.sh: STAGELOOM_WINE_USER=$user does not exist under $USERS" >&2
      return 1
    fi
    printf '%s\n' "$user"
    return 0
  fi

  if [ ! -d "$USERS" ]; then
    echo "deploy.sh: no wine users directory at $USERS" >&2
    echo "deploy.sh: set WINEPREFIX to the Rhino 8 prefix" >&2
    return 1
  fi

  for entry in "$USERS"/*; do
    [ -d "$entry" ] || continue
    name="$(basename "$entry")"
    case "$name" in
      Public|public) continue ;;
    esac
    users+=("$name")
  done

  if [ "${#users[@]}" -eq 0 ]; then
    echo "deploy.sh: no Windows user under $USERS" >&2
    echo "deploy.sh: set STAGELOOM_WINE_USER or check WINEPREFIX" >&2
    return 1
  fi
  if [ "${#users[@]}" -gt 1 ]; then
    echo "deploy.sh: multiple Windows users under $USERS:" >&2
    printf '  %s\n' "${users[@]}" >&2
    echo "deploy.sh: set STAGELOOM_WINE_USER to the Rhino 8 user" >&2
    return 1
  fi

  printf '%s\n' "${users[0]}"
}

WINE_USER="$(resolve_wine_user)" || exit 1
DEST="$USERS/$WINE_USER/AppData/Roaming/Grasshopper/Libraries/Stageloom"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "deploy.sh: prefix $PREFIX"
  echo "deploy.sh: user $WINE_USER"
  echo "deploy.sh: destination $DEST/Stageloom.gha"
  echo "deploy.sh: dry run; no files copied"
  exit 0
fi

[ -f "$GHA" ] || { echo "no $GHA; run grasshopper/build/build-gha.sh first" >&2; exit 1; }
mkdir -p "$DEST"
cp "$GHA" "$DEST/Stageloom.gha"
ls -la "$DEST"
echo "deployed to $DEST/Stageloom.gha"
echo "note: Grasshopper only scans Libraries on load, so start (or restart) Rhino"
