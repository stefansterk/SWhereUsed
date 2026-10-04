Attribute VB_Name = "SWhereUsed"
' SWhereUsed for SOLIDWORKS: open the where-used page for the selected component.
'
' What it looks up:
'   - a selected component (or a face/edge of it) in an assembly
'   - a selected drawing view in a drawing (the model shown in that view)
'   - otherwise the active part or assembly itself, or the model of the first view of a drawing
'
' Install once: Tools > Macro > New, save as SWhereUsed.swp, delete the empty code, then
' File > Import File... and pick this SWhereUsed.bas. Put it on a toolbar button or a keyboard shortcut.
'
' Optional: fill in APP_FOLDER with the folder that holds "Start SWhereUsed.bat". The macro then
' starts SWhereUsed for you when it is not running yet.
Option Explicit

Private Const APP_URL As String = "http://127.0.0.1:8790"
Private Const APP_FOLDER As String = ""          ' e.g. "C:\Tools\SWhereUsed"

Private Const swDocDRAWING As Long = 3
Private Const swSelCOMPONENTS As Long = 20
Private Const swSelDRAWINGVIEWS As Long = 12

Sub main()
    On Error GoTo Failed
    Dim swApp As SldWorks.SldWorks
    Dim swModel As SldWorks.ModelDoc2
    Dim swSelMgr As SldWorks.SelectionMgr
    Dim swComp As SldWorks.Component2
    Dim swView As SldWorks.View
    Dim filePath As String, configName As String

    Set swApp = Application.SldWorks
    Set swModel = swApp.ActiveDoc
    If swModel Is Nothing Then
        MsgBox "Open a part, assembly or drawing first.", vbInformation, "SWhereUsed"
        Exit Sub
    End If

    Set swSelMgr = swModel.SelectionManager
    If swSelMgr.GetSelectedObjectCount2(-1) > 0 Then
        If swModel.GetType = swDocDRAWING Then
            If swSelMgr.GetSelectedObjectType3(1, -1) = swSelDRAWINGVIEWS Then
                Set swView = swSelMgr.GetSelectedObject6(1, -1)
                filePath = swView.GetReferencedModelName
                configName = swView.ReferencedConfiguration
            End If
        Else
            ' Works for a selected component and for a face, edge or vertex of one
            Set swComp = swSelMgr.GetSelectedObjectsComponent4(1, -1)
            If Not swComp Is Nothing Then
                filePath = swComp.GetPathName
                configName = swComp.ReferencedConfiguration
            End If
        End If
    End If

    If filePath = "" And swModel.GetType = swDocDRAWING Then
        ' Nothing selected in a drawing: the model of the first view (GetFirstView returns the sheet)
        Dim swDraw As SldWorks.DrawingDoc
        Set swDraw = swModel
        Set swView = swDraw.GetFirstView
        If Not swView Is Nothing Then Set swView = swView.GetNextView
        Do While Not swView Is Nothing
            If swView.GetReferencedModelName <> "" Then
                filePath = swView.GetReferencedModelName
                configName = swView.ReferencedConfiguration
                Exit Do
            End If
            Set swView = swView.GetNextView
        Loop
    End If

    If filePath = "" Then filePath = swModel.GetPathName
    If filePath = "" Then
        MsgBox "Save the document first: SWhereUsed needs a file on disk.", vbInformation, "SWhereUsed"
        Exit Sub
    End If

    If Not EnsureRunning() Then Exit Sub

    Dim url As String
    url = APP_URL & "/?path=" & UrlEncode(filePath)
    If configName <> "" Then url = url & "&config=" & UrlEncode(configName)
    CreateObject("Shell.Application").ShellExecute url
    Exit Sub

Failed:
    MsgBox "SWhereUsed macro: " & Err.Description, vbExclamation, "SWhereUsed"
End Sub

' Is the app running? If not, and APP_FOLDER is set, start it and wait for it.
Private Function EnsureRunning() As Boolean
    If IsRunning() Then
        EnsureRunning = True
        Exit Function
    End If
    If APP_FOLDER = "" Then
        MsgBox "SWhereUsed is not running. Start it with 'Start SWhereUsed.bat' and try again." & vbCrLf & vbCrLf & _
               "Tip: set APP_FOLDER at the top of this macro and it starts SWhereUsed for you.", vbInformation, "SWhereUsed"
        Exit Function
    End If
    Dim bat As String
    bat = APP_FOLDER & "\Start SWhereUsed.bat"
    If Dir(bat) = "" Then
        MsgBox "Not found: " & bat, vbExclamation, "SWhereUsed"
        Exit Function
    End If
    CreateObject("Shell.Application").ShellExecute "cmd.exe", "/c """"" & bat & """ --no-browser""", APP_FOLDER, "open", 7
    Dim t As Single
    t = Timer
    Do While Abs(Timer - t) < 60
        If IsRunning() Then
            EnsureRunning = True
            Exit Function
        End If
        DoEvents
    Loop
    MsgBox "SWhereUsed did not start within a minute. Look at its window for the reason.", vbExclamation, "SWhereUsed"
End Function

Private Function IsRunning() As Boolean
    On Error GoTo NotRunning
    Dim http As Object
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.setTimeouts 1000, 1000, 1000, 2000
    http.Open "GET", APP_URL & "/api/health", False
    http.send
    IsRunning = (http.Status = 200)
    Exit Function
NotRunning:
    IsRunning = False
End Function

' Percent-encode as UTF-8 (paths may contain accents and other non-ASCII characters)
Private Function UrlEncode(ByVal s As String) As String
    Dim i As Long, c As Long, lo As Long, ch As String, out As String
    i = 1
    Do While i <= Len(s)
        ch = Mid$(s, i, 1)
        c = AscW(ch) And &HFFFF&
        If (c >= 48 And c <= 57) Or (c >= 65 And c <= 90) Or (c >= 97 And c <= 122) _
           Or ch = "-" Or ch = "_" Or ch = "." Or ch = "~" Then
            out = out & ch
        ElseIf c < &H80& Then
            out = out & Pct(c)
        ElseIf c < &H800& Then
            out = out & Pct(&HC0& Or (c \ &H40&)) & Pct(&H80& Or (c And &H3F&))
        ElseIf c >= &HD800& And c <= &HDBFF& And i < Len(s) Then
            ' Surrogate pair: one code point above U+FFFF, four UTF-8 bytes
            lo = AscW(Mid$(s, i + 1, 1)) And &HFFFF&
            c = &H10000 + (c - &HD800&) * &H400& + (lo - &HDC00&)
            out = out & Pct(&HF0& Or (c \ &H40000)) & Pct(&H80& Or ((c \ &H1000&) And &H3F&)) & _
                        Pct(&H80& Or ((c \ &H40&) And &H3F&)) & Pct(&H80& Or (c And &H3F&))
            i = i + 1
        Else
            out = out & Pct(&HE0& Or (c \ &H1000&)) & Pct(&H80& Or ((c \ &H40&) And &H3F&)) & Pct(&H80& Or (c And &H3F&))
        End If
        i = i + 1
    Loop
    UrlEncode = out
End Function

Private Function Pct(ByVal b As Long) As String
    Pct = "%" & Right$("0" & Hex$(b), 2)
End Function
