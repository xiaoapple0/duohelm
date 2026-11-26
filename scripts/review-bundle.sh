#!/bin/sh
# 生成 reviewer 子代理的审查材料（.claude/agents/reviewer.md）：只有命令的原始输出，没有结论。
# 用法：在要审查的分支的工作区里（任意子目录都可以）运行
#   sh scripts/review-bundle.sh > <会话临时目录>/review-bundle.txt
# 只运行只读命令：不 fetch、不改工作区和引用。
# - 命令成功时只输出它的标准输出（标准错误里的提示、警告丢弃，不混进结果）
# - 命令失败时写“报错（退出码 N）：”加标准错误；依赖它的小节写“未运行：原因”
# 每一节都会输出，不会留空也不会整节缺失（为空即正常的小节，标题里写明了）。
set -u

section() { printf '\n===== %s =====\n' "$1"; }
skipped() { echo "未运行：$1"; }

errfile=$(mktemp)
trap 'rm -f "$errfile"' EXIT

# 运行命令：成功时只输出标准输出；失败时输出“报错（退出码 N）：”加标准错误
run() {
  out=$("$@" 2>"$errfile")
  code=$?
  if [ "$code" -eq 0 ]; then
    [ -n "$out" ] && printf '%s\n' "$out"
    return 0
  fi
  printf '报错（退出码 %s）：%s\n' "$code" "$(cat "$errfile")"
  [ -n "$out" ] && printf '%s\n' "$out"
  return 1
}

cd "$(git rev-parse --show-toplevel)" || exit 1

section "工作区（仓库根目录）"
pwd -W 2>/dev/null || pwd  # Windows 的 Git Bash 下输出 D:/... 形式，Read 工具能直接用
section "当前分支"
branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
if [ -n "$branch" ]; then
  echo "$branch"
else
  echo "报错：分离 HEAD，没有分支名（HEAD 是 $(git rev-parse --short HEAD)）"
fi

section "远程 main（git ls-remote origin refs/heads/main）"
remote_main=""
if out=$(git ls-remote origin refs/heads/main 2>"$errfile") && [ -n "$out" ]; then
  remote_main=$(printf '%s\n' "$out" | head -n 1 | cut -f1)
  echo "$remote_main"
else
  echo "报错：$(cat "$errfile")${out:+$out}（没有取到远程 main 的提交）"
fi

section "merge-base（git merge-base HEAD <远程 main>）"
base=""
if [ -z "$remote_main" ]; then
  skipped "远程 main 没有取到"
elif out=$(git merge-base HEAD "$remote_main" 2>"$errfile"); then
  base=$out
  echo "$base"
else
  echo "报错：$(cat "$errfile")"
  echo "（本地可能没有远程 main 的这个提交，需要先 git fetch 再重新生成审查材料）"
fi

section "分支落后的提交（git log --oneline <merge-base>..<远程 main>；为空表示不落后）"
if [ -n "$base" ]; then run git log --oneline "$base..$remote_main"; else skipped "merge-base 失败"; fi
section "本分支的提交（git log --stat <merge-base>..HEAD）"
if [ -n "$base" ]; then run git log --stat "$base..HEAD"; else skipped "merge-base 失败"; fi
section "改动统计（git diff <merge-base>...HEAD --stat）"
if [ -n "$base" ]; then run git diff "$base...HEAD" --stat; else skipped "merge-base 失败"; fi

# 改动的文件（不含删除）。core.quotePath=false：中文文件名不转义，后面才能按名字取大小
files=""
files_ok=0
if [ -n "$base" ] && files=$(git -c core.quotePath=false diff --name-only --diff-filter=d "$base...HEAD" 2>"$errfile"); then
  files_ok=1
fi

section "改动文件的大小（HEAD 里提交的版本，字节；已删除的文件不列）"
if [ -z "$base" ]; then
  skipped "merge-base 失败"
elif [ "$files_ok" -ne 1 ]; then
  echo "报错：列出改动文件失败：$(cat "$errfile")"
elif [ -z "$files" ]; then
  echo "（没有改动或新增的文件）"
else
  printf '%s\n' "$files" | while IFS= read -r f; do
    if size=$(git cat-file -s "HEAD:$f" 2>"$errfile"); then
      printf '%s\t%s\n' "$size" "$f"
    else
      printf '报错：%s\t%s\n' "$(cat "$errfile")" "$f"
    fi
  done
fi

section "被跟踪却应被忽略的文件（git ls-files -i -c --exclude-standard；为空表示没有）"
run git ls-files -i -c --exclude-standard

section "PR 状态（gh pr view <分支> --json number,state,mergedAt）"
if [ -z "$branch" ]; then
  skipped "分离 HEAD，没有分支名"
elif out=$(gh pr view "$branch" --json number,state,mergedAt 2>"$errfile"); then
  echo "$out"
elif grep -qi "no pull requests found" "$errfile"; then
  echo "没有 PR：这个分支还没有开 PR（正常，第一次开 PR 之前就是这样）"
else
  echo "报错：$(cat "$errfile")"
fi

section "必需检查（gh api repos/xiaoapple0/duohelm/rules/branches/main；只在改动了 .github/ 时运行）"
if [ -z "$base" ]; then
  skipped "merge-base 失败"
elif [ "$files_ok" -ne 1 ]; then
  echo "报错：列出改动文件失败，无法判断是否改动了 .github/"
elif all=$(git -c core.quotePath=false diff --name-only "$base...HEAD" 2>"$errfile") \
  && printf '%s\n' "$all" | grep -q '^\.github/'; then
  run gh api repos/xiaoapple0/duohelm/rules/branches/main
else
  skipped "diff 没有改动 .github/"
fi

section "完整 diff（git diff <merge-base>...HEAD）"
if [ -n "$base" ]; then run git diff "$base...HEAD"; else skipped "merge-base 失败"; fi
