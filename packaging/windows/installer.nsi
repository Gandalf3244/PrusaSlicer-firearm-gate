; PrusaSlicer (firearm gate) - Windows installer.
;
;   makensis -DSTAGE=<dir> -DVERSION=2.9.6 -DOUTFILE=PrusaSlicer-FirearmGate-2.9.6-win64.exe installer.nsi
;
; STAGE holds exactly what gets installed:
;   prusa-slicer.exe, prusa-slicer-console.exe, prusa-gcodeviewer.exe, *.dll   (the Windows build)
;   resources\                                                                (PrusaSlicer resources)
;   resources\firearm-check\python\, resources\firearm-check\app\             (make_checker_runtime.sh)
; Nothing else is needed on the target machine: the checker's Python runtime is private
; to the install and PrusaSlicer finds it next to its resources.

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
!define REGKEY    "Software\Microsoft\Windows\CurrentVersion\Uninstall\PrusaSlicer-FirearmGate"

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

!define MUI_ABORTWARNING
!define MUI_ICON   "${STAGE}\resources\icons\PrusaSlicer.ico"
!define MUI_UNICON "${STAGE}\resources\icons\PrusaSlicer.ico"
!define MUI_WELCOMEPAGE_TITLE "${APPNAME}"
!define MUI_WELCOMEPAGE_TEXT "This installs PrusaSlicer ${VERSION} with the firearm-part gate.$\r$\n$\r$\nEvery model is checked against a reference library of printed-gun parts before it is sliced; a recognised part is refused and the evidence is shown.$\r$\n$\r$\nThe checker and its Python runtime are included - nothing else has to be installed."
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

Section "PrusaSlicer with firearm gate" SEC_MAIN
  SectionIn RO
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
SectionEnd
