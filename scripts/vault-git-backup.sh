set -euo pipefail

vault_path=$1
remote=$2
branch=$3

fail() {
  echo "vault-git-backup: ERROR: $*" >&2
  exit 1
}

[ -d "$vault_path" ] || fail "vault path does not exist: $vault_path"
git_common_dir=$(git -C "$vault_path" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
  || fail "not a Git repository: $vault_path"

lock_dir="$git_common_dir/vault-git-backup.lock"
if ! /bin/mkdir "$lock_dir" 2>/dev/null; then
  fail "another backup is running, or a stale lock exists: $lock_dir"
fi

temp_index=""
cleanup() {
  if [ -n "$temp_index" ]; then
    /bin/rm -f "$temp_index"
  fi
  /bin/rm -f "$lock_dir/pid"
  /bin/rmdir "$lock_dir"
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP
printf '%s\n' "$$" > "$lock_dir/pid"

for state_path in MERGE_HEAD REBASE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG rebase-merge rebase-apply sequencer; do
  resolved_path=$(git -C "$vault_path" rev-parse --path-format=absolute --git-path "$state_path")
  if [ -e "$resolved_path" ]; then
    fail "Git operation is in progress ($state_path); finish it manually"
  fi
done

current_branch=$(git -C "$vault_path" symbolic-ref --quiet --short HEAD) \
  || fail "detached HEAD is not safe for automated backup"
[ "$current_branch" = "$branch" ] \
  || fail "expected branch '$branch', found '$current_branch'"

if ! git -C "$vault_path" diff --cached --quiet --exit-code; then
  fail "index contains manually staged changes; commit or unstage them manually"
fi

echo "vault-git-backup: fetching $remote/$branch"
git -C "$vault_path" fetch "$remote" "$branch" \
  || fail "fetch failed; repository content and index were not changed"

local_head=$(git -C "$vault_path" rev-parse HEAD)
remote_head=$(git -C "$vault_path" rev-parse "refs/remotes/$remote/$branch" 2>/dev/null) \
  || fail "remote-tracking ref refs/remotes/$remote/$branch does not exist"
[ "$local_head" = "$remote_head" ] \
  || fail "local HEAD does not match $remote/$branch; reconcile manually"

if [ -z "$(git -C "$vault_path" status --porcelain --untracked-files=all)" ]; then
  echo "vault-git-backup: no changes to back up"
  exit 0
fi

temp_index=$(/usr/bin/mktemp "${TMPDIR:-/tmp}/vault-git-backup-index.XXXXXX")
/bin/rm -f "$temp_index"
GIT_INDEX_FILE="$temp_index" git -C "$vault_path" read-tree HEAD
GIT_INDEX_FILE="$temp_index" git -C "$vault_path" add --all
GIT_INDEX_FILE="$temp_index" git -C "$vault_path" diff --cached --check \
  || fail "snapshot failed Git whitespace checks; the real index was not changed"
expected_tree=$(GIT_INDEX_FILE="$temp_index" git -C "$vault_path" write-tree)

[ "$(git -C "$vault_path" symbolic-ref --quiet --short HEAD)" = "$branch" ] \
  || fail "branch changed while preparing the snapshot; the real index was not changed"
[ "$(git -C "$vault_path" rev-parse HEAD)" = "$local_head" ] \
  || fail "HEAD changed while preparing the snapshot; the real index was not changed"
git -C "$vault_path" diff --cached --quiet --exit-code \
  || fail "index changed while preparing the snapshot; no automated changes were staged"

git -C "$vault_path" add --all
git -C "$vault_path" diff --cached --check \
  || fail "staged snapshot failed Git whitespace checks; inspect the index manually"
[ "$(git -C "$vault_path" write-tree)" = "$expected_tree" ] \
  || fail "worktree or index changed during staging; inspect the index manually"

if git -C "$vault_path" diff --cached --quiet --exit-code; then
  echo "vault-git-backup: changes disappeared before staging; no commit created"
  exit 0
fi

message="second-brain: automated vault snapshot $(/bin/date +%F)"
git -C "$vault_path" commit --no-gpg-sign -m "$message" \
  || fail "commit failed; staged changes were left intact for inspection"
git -C "$vault_path" push "$remote" "HEAD:refs/heads/$branch" \
  || fail "push failed; the local commit was preserved and must be reconciled manually"

echo "vault-git-backup: committed and pushed '$message'"
