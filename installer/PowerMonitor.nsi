; ============================================================================
;  开机能耗统计 —— Windows 安装程序
;
;  构建： NSIS 3.10 (Unicode) + Modern UI 2，简体中文界面
;  编译： makensis.exe PowerMonitor.nsi
;  产物： ..\dist\开机能耗统计-安装程序-v1.0.0.exe
;
;  设计取舍
;    · per-user 安装（$LOCALAPPDATA\Programs\PowerMonitor）+ RequestExecutionLevel user
;      —— 全程不弹 UAC。程序要写的 HKCU\...\Run 与 HKCU\Control Panel\NotifyIconSettings
;         本来就是用户级设置，不需要管理员权限。
;    · $LOCALAPPDATA\PowerMonitor 是**用户数据目录**（v1.0.6 起配置与账本的固定
;      位置），安装/卸载都不得删除它。老版本把 state.json / config.json 放在
;      安装目录里，安装时把那份接进数据目录，设置与账本不丢。
;      ⚠️ 这个目录以前同时是「旧便携版目录」，收尾步骤里原本有一段 Delete/RMDir
;      清理它 —— 那正是把用户账本删掉的地方（v1.0.6 首次发布时踩过），已移除。
;    · 卸载时一并清掉自启项与托盘「常驻任务栏」登记，不留尾巴。
;
;  注意：本文件必须保存为 UTF-8 with BOM，否则 Unicode 模式下中文会编译成乱码。
; ============================================================================

Unicode true

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

; ------------------------------------------------------------------- 应用信息
!define APP_NAME      "开机能耗统计"
!define APP_EXE       "能耗统计.exe"
!define APP_ID        "PowerMonitor"
!define APP_VERSION   "1.0.13"
!define APP_PUBLISHER "本地构建"

!define UNINST_EXE "卸载 ${APP_NAME}.exe"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"
!define RUN_KEY    "Software\Microsoft\Windows\CurrentVersion\Run"
!define LEGACY_DIR "$LOCALAPPDATA\${APP_ID}"

Name "${APP_NAME}"
Caption "${APP_NAME} ${APP_VERSION} 安装向导"
OutFile "..\dist\${APP_NAME}-安装程序-v${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_ID}"
InstallDirRegKey HKCU "${UNINST_KEY}" "InstallLocation"

RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails nevershow
ShowUninstDetails nevershow
BrandingText "${APP_NAME} ${APP_VERSION}"

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName"     "${APP_NAME}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APP_NAME} 安装向导"
VIAddVersionKey /LANG=2052 "FileVersion"     "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion"  "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "LegalCopyright"  "本机自用工具，可自由修改与分发"

; ------------------------------------------------------------------- 界面
!define MUI_ICON   "..\app.ico"
!define MUI_UNICON "..\app.ico"
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "欢迎安装 ${APP_NAME}"
!define MUI_WELCOMEPAGE_TEXT "这个程序常驻任务栏通知区域，实时统计这台电脑从本次开机到现在的整机能耗与电费。$\r$\n$\r$\n· 零内核驱动、零管理员权限$\r$\n· 内置 30 个省份的居民电价，选中即填，也可以自己改$\r$\n· 图标常驻任务栏，鼠标悬停即见当前功率$\r$\n$\r$\n点「下一步」继续。"

!define MUI_DIRECTORYPAGE_TEXT_TOP "程序将安装到下面的文件夹。这是当前用户的目录，安装过程不需要管理员权限。"
!define MUI_DIRECTORYPAGE_TEXT_DESTINATION "目标文件夹"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "安装完成"
!define MUI_FINISHPAGE_TEXT "${APP_NAME} ${APP_VERSION} 已经装好了。$\r$\n$\r$\n程序会常驻任务栏通知区域（就在 ^ 箭头左侧），左键点图标可打开详情面板。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APP_NAME}"
!define MUI_FINISHPAGE_LINK "打开使用说明"
!define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR\使用说明.txt"
!insertmacro MUI_PAGE_FINISH

; MUI 的静态文本控件不会自动加高，这段必须短到能放进两行，否则会文字重叠
!define MUI_UNCONFIRMPAGE_TEXT_TOP "将从电脑上移除 ${APP_NAME}。$\r$\n$\r$\n程序会自动退出，安装目录、快捷方式与开机自启设置都会被一并清理。"
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; =================================================================== 安装
Section "${APP_NAME} 主程序" SEC_MAIN
  SectionIn RO
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  SetOverwrite on

  ; 旧实例可能还在托盘里跑（自启项指向旧目录），先请它退场，否则文件被占
  DetailPrint "结束正在运行的实例…"
  nsExec::ExecToLog 'taskkill /F /IM "${APP_EXE}"'
  Pop $0
  Sleep 600

  ; 安装目录里那份 config.json / state.json 是**旧版遗留**（v1.0.6 之前用户数据
  ; 就放在安装目录）。规则很简单：
  ;   · 数据目录还缺 → 把它接过去（升级不丢设置与账本）；
  ;   · 数据目录已经有了 → 用数据目录那份，安装目录这份作废；
  ;   · 两种情况搬完都把安装目录那份删掉 —— 留着一份又小又旧的账本，等哪天
  ;     数据目录出事，自动接手就会挑到它，等于把历史换成一小截。
  ${If} $INSTDIR != "${LEGACY_DIR}"
    ${If} ${FileExists} "$INSTDIR\config.json"
      ${IfNot} ${FileExists} "${LEGACY_DIR}\config.json"
        CreateDirectory "${LEGACY_DIR}"
        CopyFiles /SILENT "$INSTDIR\config.json" "${LEGACY_DIR}\config.json"
        DetailPrint "已接手原有设置：config.json"
      ${EndIf}
      ${If} ${FileExists} "${LEGACY_DIR}\config.json"
        Delete "$INSTDIR\config.json"
      ${EndIf}
    ${EndIf}
    ${If} ${FileExists} "$INSTDIR\state.json"
      ${IfNot} ${FileExists} "${LEGACY_DIR}\state.json"
        CreateDirectory "${LEGACY_DIR}"
        CopyFiles /SILENT "$INSTDIR\state.json" "${LEGACY_DIR}\state.json"
        DetailPrint "已接手原有账本：state.json"
      ${EndIf}
      ${If} ${FileExists} "${LEGACY_DIR}\state.json"
        Delete "$INSTDIR\state.json"
      ${EndIf}
    ${EndIf}
  ${EndIf}

  ; 主程序与说明文件
  File "..\dist\${APP_EXE}"
  File "使用说明.txt"

  ; 卸载程序
  WriteUninstaller "$INSTDIR\${UNINST_EXE}"

  ; 开始菜单
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\${UNINST_EXE}" "" "$INSTDIR\${APP_EXE}" 0

  ; 旧自启项若指向别处（比如上一版便携目录），先清掉；要不要重设由下面的组件决定
  ReadRegStr $0 HKCU "${RUN_KEY}" "${APP_ID}"
  ${If} $0 != ""
    ${If} $0 != `"$INSTDIR\${APP_EXE}"`
      DeleteRegValue HKCU "${RUN_KEY}" "${APP_ID}"
      DetailPrint "已清除指向旧路径的开机自启项"
    ${EndIf}
  ${EndIf}
SectionEnd

Section "创建桌面快捷方式" SEC_DESKTOP
  SetShellVarContext current
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

Section "开机自动启动（登录后即开始统计）" SEC_AUTOSTART
  WriteRegStr HKCU "${RUN_KEY}" "${APP_ID}" `"$INSTDIR\${APP_EXE}"`
SectionEnd

; 隐藏的收尾步骤
Section "-收尾" SEC_INFO
  SetShellVarContext current

  ; ⚠️ 这里**不得**清理 ${LEGACY_DIR}（$LOCALAPPDATA\PowerMonitor）。
  ; 那个目录在 v1.0.6 之后是用户数据目录，装的是用户的能耗账本 state.json
  ; （几度电、几百天的历史）。v1.0.6 首次发布时这里还留着一段「旧便携目录
  ; 清理」，`Delete "${LEGACY_DIR}\state.json"` 把刚攒下的 3605 Wh 账本直接
  ; 删了，用户看到的现象就是「更新一次，记录全没了」—— 与 v1.0.6 想修的
  ; 问题一模一样。卸载同样不删用户数据：升级、重装都不该让历史归零。

  ; 登记到「设置 → 应用 → 已安装的应用」
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayName"          "${APP_NAME}"
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayVersion"       "${APP_VERSION}"
  WriteRegStr   HKCU "${UNINST_KEY}" "Publisher"            "${APP_PUBLISHER}"
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayIcon"          "$INSTDIR\${APP_EXE}"
  WriteRegStr   HKCU "${UNINST_KEY}" "InstallLocation"      "$INSTDIR"
  WriteRegStr   HKCU "${UNINST_KEY}" "UninstallString"      `"$INSTDIR\${UNINST_EXE}"`
  WriteRegStr   HKCU "${UNINST_KEY}" "QuietUninstallString" `"$INSTDIR\${UNINST_EXE}" /S`
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

; ------------------------------------------------------------------- 组件说明
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_MAIN}      "程序文件、开始菜单快捷方式与卸载程序（必需）"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP}   "在桌面放一个快捷方式，方便随时打开详情面板"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_AUTOSTART} "登录 Windows 后自动启动，开机起就开始累计能耗"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; =================================================================== 卸载
Section "Uninstall"
  SetShellVarContext current

  DetailPrint "结束正在运行的实例…"
  nsExec::ExecToLog 'taskkill /F /IM "${APP_EXE}"'
  Pop $0
  Sleep 800

  ; 快捷方式
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk"
  RMDir  "$SMPROGRAMS\${APP_NAME}"

  ; 开机自启项
  DeleteRegValue HKCU "${RUN_KEY}" "${APP_ID}"

  ; 自动置顶的记录 —— 留着的话重装后程序会以为「已经登记过」而不再置顶
  DeleteRegKey HKCU "Software\PowerMonitor"

  ; 这里故意**不**清理 HKCU\Control Panel\NotifyIconSettings 里属于本程序的条目。
  ; 那是 explorer 保存的显示偏好（是否常驻任务栏、图标顺序），不是程序数据。
  ; 而且 explorer 只在「第一次见到某个 exe」时建键，键被删掉后它并不会重建 ——
  ; 于是重装后程序找不到条目可写，图标只能掉回 ^ 里。留着正好让重装无缝衔接。
  ; 若要彻底清除，手动删 HKCU\Control Panel\NotifyIconSettings 下 ExecutablePath
  ; 指向本程序的那几个子键即可。

  ; 「已安装的应用」登记
  DeleteRegKey HKCU "${UNINST_KEY}"

  ; 安装目录（带护栏：绝不递归删到盘根或父目录）
  ${If} $INSTDIR != ""
    ${If} $INSTDIR != "$LOCALAPPDATA"
      ${If} $INSTDIR != "$LOCALAPPDATA\Programs"
        RMDir /r "$INSTDIR"
      ${EndIf}
    ${EndIf}
  ${EndIf}
SectionEnd
