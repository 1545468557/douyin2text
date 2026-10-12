#!/bin/bash
#
# 把「抖音视频转文字」安装成一个可双击的 macOS App。
#
# 用法：
#   ./install-app.sh              安装到桌面（默认）
#   ./install-app.sh ~/Applications
#   ./install-app.sh .            安装到项目目录里
#
# App 本身很薄：它只是启动项目里的 Python 服务，再开一个原生窗口。
# 所以脚本会把项目路径写进 App 内，并在启动时按「记录路径 → 相对位置 →
# 常见位置」的顺序去找项目。项目被搬动过的话，重跑一次本脚本即可。

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/Desktop}"
APP_NAME="抖音视频转文字"
APP="$DEST/$APP_NAME.app"
ICON="$PROJECT/assets/AppIcon.icns"

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  echo "错误：找不到 $PROJECT/.venv/bin/python" >&2
  echo "请先在项目目录里装好依赖。" >&2
  exit 1
fi

if [ ! -f "$ICON" ]; then
  echo "错误：找不到图标 $ICON" >&2
  exit 1
fi

mkdir -p "$DEST"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ICON" "$APP/Contents/Resources/AppIcon.icns"

# 把项目位置记进 App，启动器优先读它。
printf '%s\n' "$PROJECT" > "$APP/Contents/Resources/project-path"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>抖音视频转文字</string>
  <key>CFBundleDisplayName</key>       <string>抖音视频转文字</string>
  <key>CFBundleExecutable</key>        <string>douyin2text-launcher</string>
  <key>CFBundleIdentifier</key>        <string>local.douyin2text.app</string>
  <key>CFBundleIconFile</key>          <string>AppIcon</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>CFBundleVersion</key>           <string>2</string>
  <key>LSMinimumSystemVersion</key>    <string>11.0</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
  <key>LSApplicationCategoryType</key> <string>public.app-category.productivity</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/douyin2text-launcher" <<'LAUNCH'
#!/bin/bash
# 抖音视频转文字 —— 启动器
#
# 双击本 App：启动本地服务 → 打开原生窗口。关掉窗口即退出，不在后台留进程。
#
# 启动失败会弹窗告知。详细信息在日志里：
#   ~/Library/Logs/douyin2text.log（不可写时退回项目内 .cache/app.log）

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$(cd "$HERE/.." && pwd)"

alert() {
  /usr/bin/osascript -e "display alert \"抖音视频转文字\" message \"$1\" as critical" \
    >/dev/null 2>&1
}

# 找项目目录：先看安装时记下的路径，再试相对位置，最后试常见位置。
# App 可能被放在桌面、应用程序目录，或者项目里面。
resolve_project() {
  local candidates=() recorded relative parent
  recorded="$BUNDLE/Resources/project-path"
  [ -f "$recorded" ] && candidates+=("$(cat "$recorded")")

  # 相对：App 在项目根目录里时 = 项目根；在项目上层时也试一层。
  parent="$(cd "$BUNDLE/.." && pwd)"
  candidates+=("$parent" "$(cd "$parent/.." 2>/dev/null && pwd || true)")

  candidates+=(
    "$HOME/Library/Application Support/douyin2text"
    "$HOME/douyin2text"
  )

  local c
  for c in "${candidates[@]}"; do
    [ -n "$c" ] && [ -x "$c/.venv/bin/python" ] && [ -d "$c/douyin2text" ] && {
      printf '%s\n' "$c"
      return 0
    }
  done
  return 1
}

LOG="$HOME/Library/Logs/douyin2text.log"
if ! mkdir -p "$(dirname "$LOG")" 2>/dev/null || ! : >>"$LOG" 2>/dev/null; then
  LOG="${TMPDIR:-/tmp}/douyin2text.log"
fi

PROJECT="$(resolve_project)" || {
  alert "找不到 douyin2text 项目目录。

App 会按顺序查找：
1. 安装时记下的路径
2. App 所在位置的上层目录
3. ~/Library/Application Support/douyin2text

如果你移动过项目，请在项目目录里重新运行：
    ./install-app.sh"
  exit 1
}

# Finder 启动的进程只有最小 PATH（/usr/bin:/bin:/usr/sbin:/sbin），
# 用户的 shell 配置不会被加载。ffmpeg 这类装在 Homebrew 或 ~/.local/bin 的
# 工具因此会「在终端里能用、双击就找不到」，必须在这里补上。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:$PATH"

# 可编辑安装失效时（例如整个目录被搬动过），仍能靠 PYTHONPATH 找到包。
export PYTHONPATH="$PROJECT${PYTHONPATH:+:$PYTHONPATH}"
cd "$PROJECT" || { alert "无法进入目录：$PROJECT"; exit 1; }

{
  echo "----- $(date '+%Y-%m-%d %H:%M:%S') 启动（项目：$PROJECT）-----"
  "$PROJECT/.venv/bin/python" -m douyin2text.desktop "$@"
  code=$?
  echo "----- 退出码 $code -----"
} >>"$LOG" 2>&1

# 正常关窗口是 0。非 0 说明没起来，把日志里最有用的一行弹给用户，
# 否则 App 会静默失败，用户只会看到「双击没反应」。
if [ "${code:-0}" -ne 0 ]; then
  detail="$(grep -E '^[A-Za-z_.]*(Error|Exception):|^错误：' "$LOG" 2>/dev/null | tail -1)"
  alert "启动失败（退出码 $code）。
${detail:-详见日志：$LOG}"
fi

exit "${code:-0}"
LAUNCH

chmod +x "$APP/Contents/MacOS/douyin2text-launcher"

# 让 Finder 立刻刷新图标和名称，而不是等 Spotlight 慢慢索引。
touch "$APP"

echo "已安装：$APP"
echo "项目位置：$PROJECT"
echo
echo "在 Finder 里双击即可使用。启动日志：~/Library/Logs/douyin2text.log"
