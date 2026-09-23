; PrusaSlicer (firearm gate) - Windows installer.
;
;   makensis -DSTAGE=<absolute dir> -DVERSION=2.9.6 -DOUTFILE=<absolute path>.exe installer.nsi
;   (makensis resolves relative paths against this script's directory, so pass absolute ones)
;
; STAGE holds exactly what gets installed:
;   prusa-slicer.exe, prusa-slicer-console.exe, prusa-gcodeviewer.exe, *.dll   (the Windows build)
;   resources\                                                                (PrusaSlicer resources)
;   resources\firearm-check\python\, resources\firearm-check\app\             (make_checker_runtime.sh)
; Nothing else is needed on the target machine: the checker's Python runtime is private
; to the install and PrusaSlicer finds it next to its resources.
;
; A stock PrusaSlicer next to this one would slice anything, so the installer removes it
; first (every "PrusaSlicer" entry in Add/Remove Programs, plus leftover copies in the
; standard folders) and refuses to install while one remains.

!ifndef STAGE
  !error "pass -DSTAGE=<staging dir>"
!endif
!ifndef VERSION
  !define VERSION "2.9.6"
!endif
!ifndef OUTFILE
  !define OUTFILE "PrusaSlicer-FirearmGate-${VERSION}-win64.exe"
!endif

!define APPNAME   "PrusaSlicer (firearm gate)"
!define UNINST    "Software\Microsoft\Windows\CurrentVersion\Uninstall"
!define REGKEY    "${UNINST}\PrusaSlicer-FirearmGate"

Unicode true
Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\PrusaSlicer-FirearmGate"
InstallDirRegKey HKLM "${REGKEY}" "InstallLocation"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
BrandingText "${APPNAME} ${VERSION}"

!include "MUI2.nsh"
!include "x64.nsh"
!include "LogicLib.nsh"
!include "StrFunc.nsh"
${StrStr}

!define MUI_ABORTWARNING
!define MUI_ICON   "${STAGE}\resources\icons\PrusaSlicer.ico"
!define MUI_UNICON "${STAGE}\resources\icons\PrusaSlicer.ico"
!define MUI_WELCOMEPAGE_TITLE "${APPNAME}"
!define MUI_WELCOMEPAGE_TEXT "This installs PrusaSlicer ${VERSION} with the firearm-part gate.$\r$\n$\r$\nEvery model is checked against a reference library of printed-gun parts before it is sliced; a recognised part is refused and the evidence is shown.$\r$\n$\r$\nAn installed stock PrusaSlicer is uninstalled first, since it would slice without the check.$\r$\n$\r$\nThe checker and its Python runtime are included - nothing else has to be installed."
!define MUI_FINISHPAGE_RUN "$INSTDIR\prusa-slicer.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APPNAME}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "This is a 64-bit application."
    Abort
  ${EndIf}
FunctionEnd

; ---- stock PrusaSlicer removal -------------------------------------------------------

; Uninstalls the Add/Remove Programs entry $R1 (display name $R2) under SHCTX in the
; current registry view; aborts the installation if the entry is still there afterwards.
Function UninstallStockEntry
  ReadRegStr $R4 SHCTX "${UNINST}\$R1" "QuietUninstallString"
  ReadRegStr $R5 SHCTX "${UNINST}\$R1" "UninstallString"
  ReadRegStr $R6 SHCTX "${UNINST}\$R1" "InstallLocation"
  ${StrStr} $R7 $R5 "msiexec"
  ${StrStr} $R8 $R5 "unins0"
  ${If} $R4 != ""
    StrCpy $R9 $R4
  ${ElseIf} $R7 != ""
    StrCpy $R9 'msiexec.exe /x $R1 /qn /norestart'      ; MSI: the key name is the product code
  ${ElseIf} $R8 != ""
    StrCpy $R9 '$R5 /VERYSILENT /SUPPRESSMSGBOXES /NORESTART'   ; Inno Setup
  ${ElseIf} $R6 != ""
    StrCpy $R9 '$R5 /S _?=$R6'                          ; NSIS; _?= makes ExecWait really wait
  ${Else}
    StrCpy $R9 '$R5 /S'
  ${EndIf}

  DetailPrint "Removing $R2: $R9"
  ExecWait $R9 $0
  DetailPrint "  exit code $0"
  StrCpy $R8 30
  Call WaitEntryGone

  ${If} $R4 != ""
  ${AndIfNot} ${Silent}
    ; the silent switch was not understood - let the user click through the stock uninstaller
    ExecWait $R5 $0
    StrCpy $R8 600
    Call WaitEntryGone
  ${EndIf}
  ${If} $R4 != ""
    MessageBox MB_OK|MB_ICONSTOP "$R2 could not be removed.$\r$\n$\r$\nClose PrusaSlicer, uninstall it in Settings > Apps, and run this installer again." /SD IDOK
    SetErrorLevel 3
    Abort
  ${EndIf}
  ${If} $R6 != ""
    StrCpy $R3 $R6
    Call DeleteStockBinaries
  ${EndIf}
FunctionEnd

; Waits up to $R8 seconds for the Uninstall entry $R1 to disappear (uninstallers that
; relaunch themselves from %TEMP% return before they are done). $R4 = "" once it is gone.
Function WaitEntryGone
  ${Do}
    ReadRegStr $R4 SHCTX "${UNINST}\$R1" "UninstallString"
    ${If} $R4 == ""
    ${OrIf} $R8 <= 0
      ${Break}
    ${EndIf}
    Sleep 1000
    IntOp $R8 $R8 - 1
  ${Loop}
FunctionEnd

; Walks the Uninstall keys under SHCTX in the current registry view.
Function RemoveStockInView
  StrCpy $R0 0
  loop:
    EnumRegKey $R1 SHCTX "${UNINST}" $R0
    StrCmp $R1 "" done
    IntOp $R0 $R0 + 1
    StrCmp $R1 "PrusaSlicer-FirearmGate" loop
    ReadRegStr $R2 SHCTX "${UNINST}\$R1" "DisplayName"
    ${StrStr} $R3 $R2 "PrusaSlicer"
    StrCmp $R3 "" loop
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "$R2 is installed on this computer. It slices without the firearm check, so it will be uninstalled now.$\r$\n$\r$\nCancel stops this installation." /SD IDOK IDOK +3
      SetErrorLevel 2
      Abort
    Call UninstallStockEntry
    StrCpy $R0 0            ; the key list changed, start over
    Goto loop
  done:
FunctionEnd

; Deletes the PrusaSlicer executables in directory $R3 (a stock install or a portable copy).
Function DeleteStockBinaries
  ${If} $R3 == $INSTDIR
    Return
  ${EndIf}
  ${IfNot} ${FileExists} "$R3\prusa-slicer.exe"
  ${AndIfNot} ${FileExists} "$R3\prusa-slicer-console.exe"
    Return
  ${EndIf}
  DetailPrint "Removing leftover PrusaSlicer in $R3"
  Delete "$R3\prusa-slicer.exe"
  Delete "$R3\prusa-slicer-console.exe"
  Delete "$R3\PrusaSlicer.dll"
  ${If} ${FileExists} "$R3\prusa-slicer.exe"
  ${OrIf} ${FileExists} "$R3\prusa-slicer-console.exe"
    MessageBox MB_OK|MB_ICONSTOP "The stock PrusaSlicer in $R3 could not be removed (is it running?).$\r$\n$\r$\nClose it and run this installer again." /SD IDOK
    SetErrorLevel 3
    Abort
  ${EndIf}
FunctionEnd

Function RemoveStockPrusaSlicer
  SetShellVarContext all
  SetRegView 64
  Call RemoveStockInView
  SetRegView 32
  Call RemoveStockInView
  SetShellVarContext current
  SetRegView 64
  Call RemoveStockInView
  SetRegView 32
  Call RemoveStockInView
  SetRegView default

  ; copies that are not registered: the old "Drivers & Apps" bundle, unpacked zips
  StrCpy $R3 "$PROGRAMFILES64\Prusa3D\PrusaSlicer"
  Call DeleteStockBinaries
  StrCpy $R3 "$PROGRAMFILES32\Prusa3D\PrusaSlicer"
  Call DeleteStockBinaries
  StrCpy $R3 "$PROGRAMFILES64\PrusaSlicer"
  Call DeleteStockBinaries
  StrCpy $R3 "$LOCALAPPDATA\Programs\PrusaSlicer"
  Call DeleteStockBinaries
FunctionEnd

; ---------------------------------------------------------------------------------------

Section "PrusaSlicer with firearm gate" SEC_MAIN
  SectionIn RO
  Call RemoveStockPrusaSlicer

  SetOutPath "$INSTDIR"
  ; a previous install is replaced wholesale, so removed files do not linger
  RMDir /r "$INSTDIR\resources"
  File /r "${STAGE}\*.*"

  ; PrusaSlicer needs the Visual C++ 2015-2022 runtime; the redistributable is bundled
  ; and installed quietly (it is a no-op when already present)
  IfFileExists "$INSTDIR\vc_redist.x64.exe" 0 +3
    ExecWait '"$INSTDIR\vc_redist.x64.exe" /install /quiet /norestart'
    Delete "$INSTDIR\vc_redist.x64.exe"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr   HKLM "${REGKEY}" "DisplayName"     "${APPNAME}"
  WriteRegStr   HKLM "${REGKEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr   HKLM "${REGKEY}" "DisplayIcon"     "$INSTDIR\prusa-slicer.exe"
  WriteRegStr   HKLM "${REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKLM "${REGKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr   HKLM "${REGKEY}" "Publisher"       "PrusaSlicer fork - firearm gate"
  WriteRegDWORD HKLM "${REGKEY}" "NoModify" 1
  WriteRegDWORD HKLM "${REGKEY}" "NoRepair" 1

  ; point PrusaSlicer's file types (ProgIDs it registers itself) at this build, so .3mf/.stl
  ; files that opened in the removed stock PrusaSlicer open here
  WriteRegStr HKCU "Software\Classes\Prusa.Slicer.1\Shell\Open\Command" "" '"$INSTDIR\prusa-slicer.exe" "%1"'
  WriteRegStr HKLM "Software\Classes\Prusa.Slicer.1\Shell\Open\Command" "" '"$INSTDIR\prusa-slicer.exe" "%1"'
  WriteRegStr HKCU "Software\Classes\PrusaSlicer.GCodeViewer.1\Shell\Open\Command" "" '"$INSTDIR\prusa-gcodeviewer.exe" "%1"'
  WriteRegStr HKLM "Software\Classes\PrusaSlicer.GCodeViewer.1\Shell\Open\Command" "" '"$INSTDIR\prusa-gcodeviewer.exe" "%1"'

  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\prusa-slicer.exe"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Desktop shortcut" SEC_DESKTOP
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\prusa-slicer.exe"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKLM "${REGKEY}"
  DeleteRegKey HKLM "Software\Classes\Prusa.Slicer.1"
  DeleteRegKey HKLM "Software\Classes\PrusaSlicer.GCodeViewer.1"
SectionEnd
