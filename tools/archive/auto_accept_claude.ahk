; Auto-press 1 in the bottom-left and bottom-right Warp panes.
;
; Runs every 3 seconds. Clicks into each bottom pane, sends "1",
; then returns focus to wherever you were working.
;
; Layout assumption: Warp is on the right monitor, 2x2 grid.
; Bottom-left pane  = ~25% from left, ~75% from top of Warp window.
; Bottom-right pane = ~75% from left, ~75% from top of Warp window.
;
; Exit: right-click tray icon -> Exit, or press Ctrl+Alt+F12.
;
; Launch:
;   "C:\Program Files\AutoHotkey\v1.1.37.02\AutoHotkeyU64.exe" this_script.ahk

#Persistent
#SingleInstance Force
SetTitleMatchMode, 2

; Kill switch: Ctrl+Alt+F12 exits the script
^!F12::ExitApp

SetTimer, AutoAccept, 3000
return

AutoAccept:
    ; Only act if Warp exists
    IfWinNotExist, ahk_exe Warp.exe
        return

    ; Remember the currently active window so we can restore focus
    WinGet, prevWin, ID, A

    ; Get Warp window position and size
    WinGet, warpID, ID, ahk_exe Warp.exe
    WinGetPos, wx, wy, ww, wh, ahk_id %warpID%

    ; Calculate pane center coordinates (relative to screen)
    ; Bottom-left pane: 25% across, 75% down
    blX := wx + (ww * 0.25)
    blY := wy + (wh * 0.75)
    ; Bottom-right pane: 75% across, 75% down
    brX := wx + (ww * 0.75)
    brY := wy + (wh * 0.75)

    ; --- Bottom-left pane ---
    ; Click into the pane to focus it (don't activate window yet to
    ; avoid visible flicker — use ControlClick if possible, else
    ; briefly activate)
    WinActivate, ahk_id %warpID%
    WinWaitActive, ahk_id %warpID%,, 1
    if ErrorLevel
        return
    Sleep, 50
    Click, %blX%, %blY%
    Sleep, 100
    Send, 1
    Sleep, 100

    ; --- Bottom-right pane ---
    Click, %brX%, %brY%
    Sleep, 100
    Send, 1
    Sleep, 100

    ; Restore focus to where the user was
    if (prevWin and prevWin != warpID)
    {
        WinActivate, ahk_id %prevWin%
    }
return
